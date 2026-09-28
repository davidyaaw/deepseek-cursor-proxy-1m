<#
  start-deepseek-proxy.ps1

  Launcher for the DeepSeek Cursor proxy (1M context fork).
  Starts the proxy (which starts the ngrok tunnel) and prints the public Base URL
  that must be pasted into Cursor -> Settings -> Models -> API Keys.
  The URL is copied to the clipboard automatically.

  Close this window to stop the proxy.
#>

param(
    # Folder of the cloned repository. Defaults to this script's own folder.
    [string]$ProxyDir = $PSScriptRoot,
    # How long to wait for the tunnel URL, in seconds.
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'

if (-not $ProxyDir) { $ProxyDir = (Get-Location).Path }

$LogFile      = Join-Path $ProxyDir '.proxy-stdout.log'
$ErrFile      = Join-Path $ProxyDir '.proxy-stderr.log'
$NgrokApiUrls = @('http://127.0.0.1:4040/api/endpoints', 'http://127.0.0.1:4040/api/tunnels')
$LocalHealth  = 'http://127.0.0.1:9000/v1/models'

# --- Helpers -------------------------------------------------------------

# Reload PATH from the registry so a freshly installed ngrok is visible.
function Update-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

# Resolve the uv executable, falling back to the known per-user install path.
function Get-UvExe {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $fallback = Join-Path $env:APPDATA 'Python\Scripts\uv.exe'
    if (Test-Path $fallback) { return $fallback }
    throw 'uv not found. Install it: https://astral.sh/uv'
}

# Install the Cursor client fix. Asks for administrator once if Cursor is under Program Files.
function Install-CursorFix {
    Write-Host '  Installing Cursor fix so Composer and Grok keep the OpenAI key...' -ForegroundColor Cyan
    $env:PYTHONPATH = Join-Path $ProxyDir 'src'
    & $uv run --no-sync python -m deepseek_cursor_proxy.cursor_byok_patch
    if ($LASTEXITCODE -eq 0) { return }
    & $uv run python -m deepseek_cursor_proxy.cursor_byok_patch
    if ($LASTEXITCODE -ne 0) {
        Write-Host '  Cursor fix was not installed. Composer and Grok need the OpenAI key turned off.' -ForegroundColor Yellow
    }
}

# Tear down leftover ngrok agents so the port 4040 control API is free.
function Reset-Ngrok {
    Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
}

# True when the local proxy already answers on port 9000.
function Test-ProxyUp {
    $code = & curl.exe -s -o NUL -w '%{http_code}' --max-time 4 $LocalHealth 2>$null
    return ($code -eq '200')
}

# Ask the local ngrok agent for its public URL. Returns "<url>/v1" or $null.
function Get-TunnelBaseUrl {
    foreach ($apiUrl in $NgrokApiUrls) {
        try {
            $raw = & curl.exe -s --max-time 4 $apiUrl 2>$null
            if (-not $raw) { continue }
            $json = $raw | ConvertFrom-Json
            $records = $json.endpoints
            if (-not $records) { $records = $json.tunnels }
            $url = $records | ForEach-Object {
                if ($_.url) { $_.url } elseif ($_.public_url) { $_.public_url }
            } | Where-Object { $_ -like 'https://*' } | Select-Object -First 1
            if ($url) { return ($url.TrimEnd('/') + '/v1') }
        } catch { }
    }
    return $null
}

# Read the proxy logs and return the "api_base_url: ..." value once it appears.
function Get-BaseUrlFromLog {
    foreach ($path in @($ErrFile, $LogFile)) {
        if (-not (Test-Path $path)) { continue }
        $match = Select-String -Path $path -Pattern 'api_base_url:\s*(\S+)' -ErrorAction SilentlyContinue |
                 Select-Object -Last 1
        if ($match) { return $match.Matches[0].Groups[1].Value.Trim() }
    }
    return $null
}

# Stop a process and every child it spawned.
function Stop-ProcessTree {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return }
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in @($children)) {
        Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

# PID of the process listening on the local proxy port.
function Get-ProxyListenerPid {
    foreach ($line in @(netstat -ano -p tcp)) {
        if ($line -match ':9000\s+.*LISTENING\s+(\d+)') {
            return [int]$Matches[1]
        }
    }
    return 0
}

# True when a PowerShell launcher is still an ancestor of this process.
function Test-LauncherAlive {
    param([int]$ProcessId)
    $id = $ProcessId
    for ($i = 0; $i -lt 8 -and $id -gt 0; $i++) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
        if (-not $proc) { return $false }
        if ($proc.Name -match '^(powershell|pwsh)(\.exe)?$') { return $true }
        $id = [int]$proc.ParentProcessId
    }
    return $false
}

# Windows console ignores ANSI colors until this mode is on.
function Enable-VirtualTerminal {
    if (-not ('Win32.ConsoleMode' -as [type])) {
        Add-Type -Namespace Win32 -Name ConsoleMode -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern System.IntPtr GetStdHandle(int nStdHandle);
[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool GetConsoleMode(System.IntPtr hConsoleHandle, out uint lpMode);
[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool SetConsoleMode(System.IntPtr hConsoleHandle, uint dwMode);
'@
    }
    $handle = [Win32.ConsoleMode]::GetStdHandle(-11)
    $mode = [uint32]0
    if ([Win32.ConsoleMode]::GetConsoleMode($handle, [ref]$mode)) {
        [void][Win32.ConsoleMode]::SetConsoleMode($handle, ($mode -bor 4))
    }
}

# Print the result banner and copy the Base URL to the clipboard.
function Show-Banner {
    param([string]$Url)

    Set-Clipboard -Value $Url -ErrorAction SilentlyContinue

    Write-Host ''
    Write-Host '  ================================================================' -ForegroundColor Green
    Write-Host '   DeepSeek proxy is READY' -ForegroundColor Green
    Write-Host '  ================================================================' -ForegroundColor Green
    Write-Host ''
    Write-Host '   Cursor Base URL (already copied to clipboard):' -ForegroundColor Gray
    Write-Host ''
    Write-Host "   $Url" -ForegroundColor Yellow
    Write-Host ''
    Write-Host '   Paste into: Settings -> Models -> API Keys -> Override OpenAI Base URL' -ForegroundColor Gray
    Write-Host '   OpenAI API Key: your DeepSeek key' -ForegroundColor Gray
    Write-Host ''
    Write-Host '   Cursor model        DeepSeek' -ForegroundColor Gray
    Write-Host '   GPT-5.6 Sol      -> deepseek-v4-pro' -ForegroundColor White
    Write-Host '   GPT-5.6 Terra    -> deepseek-flash' -ForegroundColor White
    Write-Host '   deepseek-*       -> same name' -ForegroundColor White
    Write-Host '   any other name   -> model in config.yaml' -ForegroundColor White
    Write-Host '   Effort None disables thinking. Other levels set reasoning_effort.' -ForegroundColor Gray
    Write-Host ''
    Write-Host '   Stay on Cursor:  Claude, Gemini, Composer, Grok' -ForegroundColor White
    Write-Host '   This proxy:      Sol, Terra, deepseek-*, any other name' -ForegroundColor White
    Write-Host '   Unknown names are answered by model in config.yaml.' -ForegroundColor Gray
    Write-Host ''
    Write-Host '   ------------------------------------------------------------' -ForegroundColor DarkGray
    Write-Host '   Keep this window open while working in Cursor.' -ForegroundColor DarkGray
    Write-Host '   Commands: / or help, settings, status, clear, quit' -ForegroundColor DarkGray
    Write-Host '   Close it (or press Ctrl+C) to stop the proxy.' -ForegroundColor DarkGray
    Write-Host '   ------------------------------------------------------------' -ForegroundColor DarkGray
    Write-Host ''
}

# --- Main ----------------------------------------------------------------

Update-ProcessPath

if (-not (Test-Path $ProxyDir)) {
    Write-Host "  Proxy folder not found: $ProxyDir" -ForegroundColor Red
    exit 1
}

$uv = Get-UvExe
Install-CursorFix

# Already running? Keep this window open. A dead launcher leaves an orphan we replace.
if (Test-ProxyUp) {
    $listener = Get-ProxyListenerPid
    if ($listener -and -not (Test-LauncherAlive -ProcessId $listener)) {
        Write-Host '  Previous proxy window is gone. Starting it again...' -ForegroundColor Yellow
        Stop-ProcessTree -ProcessId $listener
        Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 400
    } else {
        $url = Get-TunnelBaseUrl
        if (-not $url) { $url = Get-BaseUrlFromLog }
        if ($url) { Show-Banner -Url $url }
        else { Write-Host '  Proxy is already running but no tunnel URL was found.' -ForegroundColor Yellow }
        Write-Host '  This window is not running the proxy. Press Enter to close.' -ForegroundColor DarkGray
        Read-Host | Out-Null
        return
    }
}

$env:PYTHONUNBUFFERED = '1'
$env:UV_NO_PROGRESS    = '1'

Remove-Item $LogFile, $ErrFile -ErrorAction SilentlyContinue

$proxyArgs = 'run deepseek-cursor-proxy'
if ($env:DCP_NGROK_URL) {
    $proxyArgs += ' --ngrok-url "' + ($env:DCP_NGROK_URL -replace '"', '\"') + '"'
}

Write-Host '  Starting DeepSeek proxy + ngrok tunnel...' -ForegroundColor Cyan

# A stale ngrok agent would hold port 4040 and make the new tunnel fail to start.
Reset-Ngrok

# Copy one child stream to a log file and this window.
# A separate runspace is required: OutputDataReceived callbacks crash this host.
# One bad console write must not stop the reader: a full pipe freezes the proxy.
function Start-OutputPump {
    param($Reader, [string]$Path, [string]$Kind)
    $pump = [PowerShell]::Create()
    [void]$pump.AddScript({
        param($Reader, [string]$Path, [string]$Kind)
        $esc = [char]27
        $reset = "$esc[0m"
        while ($true) {
            try { $line = $Reader.ReadLine() } catch { break }
            if ($null -eq $line) { break }
            try { [IO.File]::AppendAllText($Path, $line + [Environment]::NewLine) } catch { }
            if ($line.Length -gt 240) { continue }
            if ($line.Length -gt 0 -and ' {[}"'.IndexOf($line[0]) -ge 0) { continue }
            try {
                if ($line.StartsWith('reasoning_cache:')) { $color = '36' }
                elseif ($Kind -eq 'command') {
                    if ($line -eq 'verbose: on') { $color = '32' }
                    elseif ($line -eq 'verbose: off') { $color = '33' }
                    elseif ($line.StartsWith('unknown')) { $color = '31' }
                    elseif ($line.StartsWith('cleared')) { $color = '33' }
                    else { $color = '96' }
                }
                elseif ($line -match '^(WARNING|ERROR)') { $color = '33' }
                elseif ($line.StartsWith('started model')) { $color = '95' }
                else { $color = '90' }
                [Console]::Out.WriteLine(('{0}[{1}m{2}{3}' -f $esc, $color, $line, $reset))
            } catch { }
        }
    }).AddArgument($Reader).AddArgument($Path).AddArgument($Kind)
    $null = $pump.BeginInvoke()
    return $pump
}

# Own the child stdin so this window can type settings/clear/quit.
# Stdout and stderr are echoed here and saved to the log files.
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $uv
$psi.Arguments = $proxyArgs
$psi.WorkingDirectory = $ProxyDir
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.RedirectStandardInput = $true
$psi.CreateNoWindow = $true
$psi.StandardOutputEncoding = [Text.Encoding]::UTF8
$psi.StandardErrorEncoding = [Text.Encoding]::UTF8
$psi.EnvironmentVariables['PYTHONUNBUFFERED'] = '1'
$psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
$psi.EnvironmentVariables['UV_NO_PROGRESS'] = '1'
$psi.EnvironmentVariables['DCP_CONSOLE'] = '1'

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
try { Enable-VirtualTerminal } catch { }
[void]$proc.Start()
$proc.StandardInput.AutoFlush = $true
$script:stdoutPump = Start-OutputPump -Reader $proc.StandardOutput -Path $LogFile -Kind command
$script:stderrPump = Start-OutputPump -Reader $proc.StandardError -Path $ErrFile -Kind log

try {
    $url       = $null
    $urlShown  = $false
    $deadline  = (Get-Date).AddSeconds($TimeoutSeconds)

    while ((Get-Date) -lt $deadline) {
        $url = Get-BaseUrlFromLog
        if (-not $url) { $url = Get-TunnelBaseUrl }
        if ($url) { Show-Banner -Url $url; $urlShown = $true; break }

        if ($proc.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }

    if (-not $urlShown) {
        Write-Host ''
        Write-Host '  Could not obtain the tunnel URL. Last log lines:' -ForegroundColor Red
        if (Test-Path $ErrFile) { Get-Content $ErrFile -Tail 15 | Write-Host }
        if (Test-Path $LogFile) { Get-Content $LogFile -Tail 15 | Write-Host }
        Write-Host ''
        Write-Host '  Press Enter to close.' -ForegroundColor DarkGray
        Read-Host
        return
    }

    # Stay alive so the proxy keeps running. Lines typed here are commands.
    while (-not $proc.HasExited) {
        $esc = [char]27
        [Console]::Out.Write("$esc[1;32m>$esc[0m ")
        [Console]::Out.Flush()
        $line = [Console]::ReadLine()
        if ($null -eq $line -or $proc.HasExited) { break }
        $proc.StandardInput.WriteLine($line)
        if ($line.Trim() -match '^(?i)/?(quit|exit|stop)$') {
            [void]$proc.WaitForExit(5000)
            break
        }
    }
} finally {
    if ($proc -and $proc.Id) { Stop-ProcessTree -ProcessId $proc.Id }
    Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
