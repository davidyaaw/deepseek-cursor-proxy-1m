"""Keep Composer and Grok on Cursor while the OpenAI key stays enabled.

Cursor attaches that key to every model except Claude and Gemini, then rejects
Composer and Grok. The proxy never sees those requests. This rewrites the
client check so those two families skip the key. Other models still use it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


# Minified provider picker. The model id is the first argument; its name varies by build.
_ROUTING = re.compile(
    rb"function (\w+)\((\w),(\w)\)\{return (\w+)\(\2\)\?\3\.useClaudeKey\?"
    rb"\"anthropic\":void 0:(\w+)\(\2\)\?\3\.useGoogleKey\?\"google\":void 0:"
    rb"\3\.useOpenAIKey\?\"openai\":void 0\}"
)
_ALREADY_PATCHED = b'.startsWith("composer-")||'
_DESKTOP_CHECKSUM = "vs/workbench/workbench.desktop.main.js"
_WORKBENCH_FILES = (
    "out/vs/workbench/workbench.desktop.main.js",
    "out/vs/workbench/workbench.glass.main.js",
)


class CursorByokPatchError(RuntimeError):
    """The installed Cursor bundle does not match the patch this script knows."""


def _hosted_models_skip_openai_key(match: re.Match[bytes]) -> bytes:
    """Rewrite one provider picker so Composer and Grok do not borrow the OpenAI key."""
    name, model, settings, claude_fn, google_fn = match.groups()
    return (
        b"function "
        + name
        + b"("
        + model
        + b","
        + settings
        + b"){return "
        + claude_fn
        + b"("
        + model
        + b")?"
        + settings
        + b'.useClaudeKey?"anthropic":void 0:'
        + google_fn
        + b"("
        + model
        + b")?"
        + settings
        + b'.useGoogleKey?"google":void 0:('
        + model
        + b'.startsWith("composer-")||'
        + model
        + b'.startsWith("grok-")?void 0:'
        + settings
        + b'.useOpenAIKey?"openai":void 0)}'
    )


def patch_workbench(data: bytes) -> bytes:
    """Return the workbench with Composer and Grok excluded from the OpenAI key."""
    matches = list(_ROUTING.finditer(data))
    if len(matches) == 1:
        return _ROUTING.sub(_hosted_models_skip_openai_key, data, count=1)
    if not matches and _ALREADY_PATCHED in data:
        return data
    raise CursorByokPatchError(
        f"expected one BYOK routing check, found {len(matches)}. "
        "This Cursor build is not supported."
    )


def workbench_checksum(data: bytes) -> str:
    """SHA-256 checksum Cursor stores in product.json, without base64 padding."""
    digest = hashlib.sha256(data).digest()
    return base64.b64encode(digest).decode("ascii").rstrip("=")


def update_product_checksum(product_json: str, checksum: str) -> str:
    """Replace the desktop workbench checksum, leaving the rest of the file as-is."""
    marker = f'"{_DESKTOP_CHECKSUM}": "'
    start = product_json.find(marker)
    if start < 0:
        return product_json
    value_at = start + len(marker)
    end = product_json.find('"', value_at)
    if end < 0:
        raise CursorByokPatchError("product.json checksum value is truncated")
    return product_json[:value_at] + checksum + product_json[end:]


def cursor_app_candidates() -> list[Path]:
    """Paths where Cursor's resources/app directory is usually installed."""
    candidates: list[Path] = []
    program_files = os.environ.get("ProgramFiles")
    local_app_data = os.environ.get("LOCALAPPDATA")
    home = Path.home()
    if program_files:
        candidates.append(Path(program_files) / "cursor" / "resources" / "app")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "cursor" / "resources" / "app")
    candidates.extend(
        [
            Path("/Applications/Cursor.app/Contents/Resources/app"),
            home / "Applications/Cursor.app/Contents/Resources/app",
            Path("/usr/share/cursor/resources/app"),
            Path("/usr/lib/cursor/resources/app"),
            Path("/opt/Cursor/resources/app"),
            home / ".local/share/cursor/resources/app",
        ]
    )
    return candidates


def discover_cursor_app_dirs() -> list[Path]:
    """Find every installed Cursor app directory, system copy first."""
    found: list[Path] = []
    seen: set[Path] = set()
    for path in cursor_app_candidates():
        if path in seen:
            continue
        seen.add(path)
        if any((path / relative).is_file() for relative in _WORKBENCH_FILES):
            found.append(path)
    return found


def _backup_path(path: Path) -> Path:
    return Path(str(path) + ".byok-orig")


def _write_bytes(path: Path, data: bytes) -> None:
    temporary = Path(str(path) + ".byok-tmp")
    temporary.write_bytes(data)
    try:
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def apply_patch(app_dir: Path) -> list[str]:
    """Patch both workbench bundles and refresh the desktop integrity checksum."""
    notes: list[str] = []
    desktop_checksum: str | None = None
    for relative in _WORKBENCH_FILES:
        path = app_dir / relative
        if not path.is_file():
            raise CursorByokPatchError(f"missing {path}")
        original = path.read_bytes()
        patched = patch_workbench(original)
        if patched == original:
            notes.append(f"already patched: {path}")
        else:
            _write_bytes(_backup_path(path), original)
            _write_bytes(path, patched)
            notes.append(f"patched: {path}")
        if relative.endswith("workbench.desktop.main.js"):
            desktop_checksum = workbench_checksum(path.read_bytes())

    product_path = app_dir / "product.json"
    if desktop_checksum is None or not product_path.is_file():
        return notes
    product_text = product_path.read_text(encoding="utf-8")
    updated = update_product_checksum(product_text, desktop_checksum)
    if updated != product_text:
        backup = _backup_path(product_path)
        if not backup.exists():
            _write_bytes(backup, product_text.encode("utf-8"))
        _write_bytes(product_path, updated.encode("utf-8"))
        notes.append("updated workbench checksum in product.json")
    return notes


def restore_patch(app_dir: Path) -> list[str]:
    """Put back the workbench files saved before the first patch."""
    notes: list[str] = []
    for relative in _WORKBENCH_FILES:
        path = app_dir / relative
        backup = _backup_path(path)
        if not backup.is_file():
            notes.append(f"no backup: {path.name}")
            continue
        _write_bytes(path, backup.read_bytes())
        notes.append(f"restored: {path.name}")

    product_path = app_dir / "product.json"
    product_backup = _backup_path(product_path)
    if product_backup.is_file():
        _write_bytes(product_path, product_backup.read_bytes())
        notes.append("restored product.json")
    return notes


def _report(lines: list[str], result_path: Path | None) -> None:
    """Print installer notes, or store them for the process that showed the UAC prompt."""
    text = "\n".join(lines)
    if result_path is not None:
        result_path.write_text(text + ("\n" if text else ""), encoding="utf-8")
        return
    if text:
        print(text)


def _summary(notes: list[str], *, restore: bool) -> list[str]:
    """Turn per-file notes into the few lines the launcher should show."""
    changed = [note for note in notes if note.startswith(("patched:", "restored:", "updated "))]
    if changed:
        closing = (
            "Cursor fix removed."
            if restore
            else "Cursor fix installed. Quit Cursor completely and open it again."
        )
        return changed + [closing]
    if notes:
        return ["Cursor fix already installed." if not restore else "Cursor fix was not installed."]
    return ["Cursor install not found. Fix skipped."]


def windows_relaunch_parameters(result_file: Path, forwarded: list[str]) -> str:
    """Build the elevated cmd line that re-runs this installer."""
    source_root = Path(__file__).resolve().parents[1]
    python_args = subprocess.list2cmdline(
        [
            "-m",
            "deepseek_cursor_proxy.cursor_byok_patch",
            "--elevated",
            "--result",
            str(result_file),
            *forwarded,
        ]
    )
    return f'/c set "PYTHONPATH={source_root}"&& "{sys.executable}" {python_args}'


def _run_elevated(parameters: str) -> int:
    """Show one UAC prompt and wait until the elevated installer exits."""
    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        """Win32 SHELLEXECUTEINFOW used to launch the installer as administrator."""

        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040
    info.lpVerb = "runas"
    info.lpFile = "cmd.exe"
    info.lpParameters = parameters
    info.nShow = 0
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:
            raise CursorByokPatchError("Administrator permission was not granted.")
        raise CursorByokPatchError(
            f"Could not request administrator permission (error {error})."
        )
    try:
        kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def _elevate_and_wait(forwarded: list[str]) -> int:
    """Re-run the installer as administrator and print its report."""
    descriptor, result_path = tempfile.mkstemp(prefix="cursor-byok-", suffix=".txt")
    os.close(descriptor)
    result = Path(result_path)
    try:
        code = _run_elevated(windows_relaunch_parameters(result, forwarded))
        if result.is_file():
            print(result.read_text(encoding="utf-8"), end="")
        return code
    finally:
        result.unlink(missing_ok=True)


def _install(app_dirs: list[Path], *, restore: bool) -> list[str]:
    """Patch or restore each Cursor install and return the raw file notes."""
    notes: list[str] = []
    for app_dir in app_dirs:
        notes.extend(restore_patch(app_dir) if restore else apply_patch(app_dir))
    return notes


def main(argv: list[str] | None = None) -> int:
    """Install the Cursor fix, elevating on Windows when Program Files is locked."""
    parser = argparse.ArgumentParser(
        description="Let Composer and Grok run while the OpenAI key stays on."
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Undo the patch using the saved originals.",
    )
    parser.add_argument(
        "--app-dir",
        type=Path,
        help="Cursor resources/app directory. Defaults to every local install.",
    )
    parser.add_argument(
        "--no-elevate",
        action="store_true",
        help="Fail instead of asking for administrator permission.",
    )
    parser.add_argument(
        "--elevated",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--result",
        type=Path,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    forwarded: list[str] = []
    if args.restore:
        forwarded.append("--restore")
    if args.app_dir is not None:
        forwarded.extend(["--app-dir", str(args.app_dir)])
    try:
        app_dirs = [args.app_dir] if args.app_dir else discover_cursor_app_dirs()
        notes = _install(app_dirs, restore=args.restore) if app_dirs else []
    except PermissionError:
        if sys.platform == "win32" and not args.no_elevate and not args.elevated:
            try:
                return _elevate_and_wait(forwarded)
            except CursorByokPatchError as exc:
                print(f"cursor fix failed: {exc}", file=sys.stderr)
                return 1
        print(
            "cursor fix failed: permission denied. Re-run as administrator.",
            file=sys.stderr,
        )
        return 1
    except (OSError, CursorByokPatchError) as exc:
        print(f"cursor fix failed: {exc}", file=sys.stderr)
        return 1
    _report(_summary(notes, restore=args.restore), args.result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
