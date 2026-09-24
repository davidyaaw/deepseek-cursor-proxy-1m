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

# Read the proxy log and return the "api_base_url: ..." value once it appears.
function Get-BaseUrlFromLog {
    if (-not (Test-Path $LogFile)) { return $null }
    $match = Select-String -Path $LogFile -Pattern 'api_base_url:\s*(\S+)' -ErrorAction SilentlyContinue |
             Select-Object -Last 1
    if ($match) { return $match.Matches[0].Groups[1].Value.Trim() }
    return $null
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
    Write-Host '   For 1M context: select GPT-5.6 Sol (or your deepseek-flash)' -ForegroundColor Gray
    Write-Host '   Model names:   GPT-5.6 Sol  |  deepseek-flash' -ForegroundColor Gray
    Write-Host '   Toggle custom API:  Ctrl+Shift+0' -ForegroundColor Gray
    Write-Host ''
    Write-Host '   ------------------------------------------------------------' -ForegroundColor DarkGray
    Write-Host '   Keep this window open while working in Cursor.' -ForegroundColor DarkGray
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

# Already running? Show the current URL instead of starting a second copy.
if (Test-ProxyUp) {
    $url = Get-TunnelBaseUrl
    if (-not $url) { $url = Get-BaseUrlFromLog }
    if ($url) { Show-Banner -Url $url }
    else { Write-Host '  Proxy is already running but no tunnel URL was found.' -ForegroundColor Yellow }
    return
}

$uv = Get-UvExe
$env:PYTHONUNBUFFERED = '1'
$env:UV_NO_PROGRESS    = '1'

Remove-Item $LogFile, $ErrFile -ErrorAction SilentlyContinue

$proxyArgs = @('run', 'deepseek-cursor-proxy')
if ($env:DCP_NGROK_URL) { $proxyArgs += @('--ngrok-url', $env:DCP_NGROK_URL) }

Write-Host '  Starting DeepSeek proxy + ngrok tunnel...' -ForegroundColor Cyan

# A stale ngrok agent would hold port 4040 and make the new tunnel fail to start.
Reset-Ngrok

$proc = Start-Process -FilePath $uv -ArgumentList $proxyArgs `
    -WorkingDirectory $ProxyDir -NoNewWindow -PassThru `
    -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile

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

    # Stay alive so the proxy keeps running; the child dies with this window.
    $proc.WaitForExit()
} finally {
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
