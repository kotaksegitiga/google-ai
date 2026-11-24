"""Remote control for MuMu Android emulator on Windows.

This module starts MuMu, launches an existing Android instance (or, only with
explicit consent, creates a new one), and waits until the ADB serial that
belongs to that instance is online. It only manages the emulator window and
ADB connection -- all Android, Frida, Google Play and payment logic stays in
ultra0.py and run-all.ps1.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_MUMU_DIR = r"C:\Program Files\Netease\MuMuPlayer"
MUMU_MAIN = r"nx_main\MuMuManager.exe"
DEFAULT_PORTS = ("16384", "7555", "16416", "21503", "62001")

MUMU_STARTUP_SECONDS = 180
ADB_POLL_SECONDS = 3
EMAIL_PATTERN = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"


class MuMuError(RuntimeError):
    """Raised when a MuMuManager operation fails or returns unusable output."""


def mumu_manager(mumu_dir: str = DEFAULT_MUMU_DIR) -> Path:
    return Path(mumu_dir) / MUMU_MAIN


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          errors="replace", timeout=timeout)


def _run_checked(args: list[str], timeout: int = 30,
                 what: str = "MuMu command") -> subprocess.CompletedProcess:
    """Run a command and raise MuMuError when it fails or produces no output."""
    try:
        result = _run(args, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MuMuError(f"{what} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise MuMuError(f"{what} failed (exit {result.returncode}): "
                        f"{detail[-1][:160] if detail else 'no output'}")
    return result


def mumu_running(mumu_dir: str = DEFAULT_MUMU_DIR) -> bool:
    """Return True if MuMuNxMain.exe is already running."""
    try:
        result = _run(["tasklist", "/FI", "IMAGENAME eq MuMuNxMain.exe",
                       "/FO", "CSV", "/NH"], timeout=10)
        return "MuMuNxMain.exe" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def launch_mumu(mumu_dir: str = DEFAULT_MUMU_DIR) -> None:
    """Launch the MuMu main application if it is not running."""
    if mumu_running(mumu_dir):
        return
    manager = mumu_manager(mumu_dir)
    if not manager.is_file():
        raise FileNotFoundError(f"MuMuManager not found: {manager}")
    _run_checked([str(manager), "main", "launch"], timeout=30,
                 what="MuMu main launch")


def players(mumu_dir: str = DEFAULT_MUMU_DIR) -> dict[str, dict]:
    """Return parsed MuMuManager info for every player instance.

    Malformed or empty instance metadata is reported instead of guessed.
    """
    result = _run_checked([str(mumu_manager(mumu_dir)), "info", "-v", "all"],
                          timeout=30, what="MuMu instance query")
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError as exc:
        raise MuMuError(f"MuMu instance metadata is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MuMuError("MuMu instance metadata has an unexpected shape")
    return {str(key): value for key, value in data.items()
            if isinstance(value, dict)}


def adb_ports_from_info(mumu_dir: str = DEFAULT_MUMU_DIR) -> tuple[str, ...]:
    """Return ADB ports reported by MuMuManager for started players.

    MuMu 6.x assigns a dynamic ADB port per instance (for example 16416),
    so the fixed legacy ports are only a fallback.
    """
    ports: list[str] = []
    for info in players(mumu_dir).values():
        port = info.get("adb_port")
        if port:
            ports.append(str(port))
    return tuple(ports)


def pick_instance(mumu_dir: str = DEFAULT_MUMU_DIR,
                  requested: int | None = None) -> int:
    """Choose which MuMu instance to use.

    Respects an explicit user selection, falls back to the single existing
    instance, and refuses to guess when several instances exist.
    """
    known = sorted(int(key) for key in players(mumu_dir))
    if requested is not None:
        if requested not in known:
            raise MuMuError(
                f"MuMu instance {requested} does not exist. "
                f"Available instances: {', '.join(map(str, known)) or 'none'}."
            )
        return requested
    if not known:
        raise MuMuError("No MuMu Android instance exists yet.")
    if len(known) == 1:
        return known[0]
    raise MuMuError(
        "Several MuMu instances exist ("
        + ", ".join(map(str, known))
        + "). Select one explicitly with --vmindex."
    )


def create_player(mumu_dir: str = DEFAULT_MUMU_DIR, version: str = "15",
                  vmindex: int = 1) -> None:
    """Create one MuMu player instance using the bundled Android image."""
    _run_checked([str(mumu_manager(mumu_dir)), "create",
                  "--vmindex", str(vmindex), "--number", "1",
                  "--ver", version], timeout=300, what="MuMu instance creation")


def launch_player(vmindex: int, mumu_dir: str = DEFAULT_MUMU_DIR) -> None:
    """Launch a specific MuMu player instance."""
    _run_checked([str(mumu_manager(mumu_dir)), "control",
                  "--vmindex", str(vmindex), "launch"], timeout=120,
                 what=f"MuMu instance {vmindex} launch")


def wait_adb(adb: str, timeout_seconds: int = MUMU_STARTUP_SECONDS,
             ports: tuple[str, ...] = DEFAULT_PORTS) -> str | None:
    """Poll ADB until an online device appears, then return its serial.

    Tries the known MuMu ADB ports first, then falls back to whatever
    `adb devices` already reports.
    """
    import shutil

    adb_exe = shutil.which(adb) if not os.path.isabs(adb) else adb
    if not adb_exe:
        return None

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        for port in ports:
            _run([adb_exe, "connect", f"127.0.0.1:{port}"], timeout=10)
        result = _run([adb_exe, "devices"], timeout=10)
        lines = [line for line in result.stdout.splitlines()
                 if "\tdevice" in line]
        if lines:
            return lines[0].split()[0]
        time.sleep(ADB_POLL_SECONDS)
    return None


def wait_instance_adb(adb: str, port: str,
                      timeout_seconds: int = MUMU_STARTUP_SECONDS) -> str | None:
    """Wait until the ADB serial for one MuMu instance is online.

    Only the serial for the selected instance's ADB port is returned, so a
    second emulator or a physical device cannot be selected by accident.
    """
    import shutil

    adb_exe = shutil.which(adb) if not os.path.isabs(adb) else adb
    if not adb_exe:
        return None
    serial = f"127.0.0.1:{port}"
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        _run([adb_exe, "connect", serial], timeout=10)
        result = _run([adb_exe, "devices"], timeout=10)
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == serial and fields[1] == "device":
                return serial
        time.sleep(ADB_POLL_SECONDS)
    return None


def start_android(mumu_dir: str = DEFAULT_MUMU_DIR,
                  timeout_seconds: int = MUMU_STARTUP_SECONDS,
                  adb_path: str | None = None,
                  vmindex: int | None = None,
                  create_missing: bool = False,
                  android_version: str = "15") -> str:
    """Start MuMu, launch the selected instance, return its ADB serial.

    An existing instance is always preferred. A new instance is only created
    when the caller explicitly sets ``create_missing=True`` (the TUI asks the
    user first). Raises MuMuError when MuMu cannot be started, the requested
    instance does not exist, or no ADB device appears.
    """
    launch_mumu(mumu_dir)

    try:
        selected = pick_instance(mumu_dir, requested=vmindex)
    except MuMuError:
        if not create_missing or vmindex is not None:
            raise
        create_player(mumu_dir, version=android_version)
        selected = pick_instance(mumu_dir, requested=None)

    launch_player(selected, mumu_dir)

    instance_info = players(mumu_dir).get(str(selected), {})
    port = str(instance_info.get("adb_port") or "")
    if not port:
        raise MuMuError(
            f"MuMu instance {selected} did not report an ADB port. "
            "Wait for the instance to finish booting and try again."
        )
    if adb_path is None:
        adb_path = str(Path(mumu_dir) / "nx_main" / "adb.exe")
    serial = wait_instance_adb(adb_path, port, timeout_seconds=timeout_seconds)
    if not serial:
        raise MuMuError(
            "MuMu started but no Android device appeared over ADB. "
            "Enable ADB debugging in MuMu and try again."
        )
    return serial



def detect_google_accounts(adb_path: str, serial: str,
                           timeout_seconds: int = 60) -> list[str]:
    """Read signed-in Google accounts from the running Android instance.

    Uses the local AccountManager dump over ADB shell. This never reads or
    stores passwords; it only lists account identities already signed in on
    the device. The result is returned to the caller, never logged with
    full addresses by this module.
    """
    adb_exe = adb_path if os.path.isabs(adb_path) else (
        __import__("shutil").which(adb_path) or adb_path)
    if not adb_exe:
        return []
    deadline = time.time() + timeout_seconds
    accounts: set[str] = set()
    while time.time() < deadline:
        try:
            result = _run([adb_exe, "-s", serial, "shell",
                           "dumpsys", "account"], timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            for line in result.stdout.splitlines():
                if "type=com.google" not in line:
                    continue
                for email in re.findall(EMAIL_PATTERN, line, flags=re.I):
                    if len(email) <= 254:
                        accounts.add(email.lower())
            if accounts:
                break
        time.sleep(ADB_POLL_SECONDS)
    return sorted(accounts)


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Start MuMu, launch an Android instance and print its ADB serial.")
    parser.add_argument("--mumu-dir", default=DEFAULT_MUMU_DIR,
                        help="MuMu installation directory (default: %(default)s)")
    parser.add_argument("--adb-path", default=None,
                        help="ADB binary used to probe for the device")
    parser.add_argument("--timeout", type=int, default=MUMU_STARTUP_SECONDS,
                        help="Seconds to wait for ADB device after launch")
    parser.add_argument("--vmindex", type=int, default=None,
                        help="Existing MuMu instance number to launch")
    parser.add_argument("--create-missing", action="store_true",
                        help="Create a new instance when none exists (opt-in)")
    parser.add_argument("--android-version", default="15",
                        help="Android version for a newly created instance")
    parser.add_argument("--list-accounts", action="store_true",
                        help="Also detect signed-in Google accounts after startup")
    args = parser.parse_args()
    try:
        serial = start_android(mumu_dir=args.mumu_dir,
                               timeout_seconds=args.timeout,
                               adb_path=args.adb_path,
                               vmindex=args.vmindex,
                               create_missing=args.create_missing,
                               android_version=args.android_version)
        accounts: list[str] = []
        if args.list_accounts:
            adb_path = args.adb_path or str(Path(args.mumu_dir) / "nx_main" / "adb.exe")
            accounts = detect_google_accounts(adb_path, serial)
        if args.list_accounts:
            print(json.dumps({"serial": serial, "accounts": accounts}))
        else:
            print(serial, end="")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
