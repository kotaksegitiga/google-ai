"""Interactive Textual frontend for ePlus AI Toolkit (Windows, macOS and Linux).

UI-only orchestration: the original run-all.ps1 and ultra0.py still own all
Android, Frida, Google Play, payment and account validation. The TUI never
declares an activated subscription from launchBillingFlow or a Subscribe tap.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
from collections import deque
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Header, Link, RichLog, Select, Static

from tui_core import STAGES, Outcome, command_for_step, mask_email, \
    needs_setup, offer_explorer_lines, outcome_for_stage, read_fresh_report, cli_issue_outcome, \
    readable_issue_line, report_summary, safe_launch_diagnostic, validate_step
from device_diagnostics import ConnectionDiagnosis, ConnectionStatus, inspect_connection, poll_connection
from stage_history import latest_history, read_history, record_history

try:  # Windows only: MuMu remote start is optional
    from mumu_remote import detect_google_accounts, start_android as mumu_start_android
    MUMU_REMOTE_AVAILABLE = True
except Exception:  # pragma: no cover - macOS or hosts without MuMu
    MUMU_REMOTE_AVAILABLE = False
    mumu_start_android = None
    detect_google_accounts = None


class TechnicalLog(RichLog):
    """Keep the latest technical output visible when a hidden log is revealed."""

    def on_resize(self, event) -> None:
        # RichLog may defer writes while display:none; its first visible resize
        # flushes those writes and finally gives the log a real scroll range.
        super().on_resize(event)
        if self.display:
            self.scroll_end(animate=False, immediate=True, x_axis=False)


class Approval(ModalScreen[bool]):
    """Clickable approval without long typed phrases."""

    CSS = """
    Approval { align: center middle; background: #070d19 75%; }
    #approval_card {
        width: 66; max-width: 92%; height: auto; max-height: 95%;
        padding: 1 3; background: #15243a; border: round #3d6383;
    }
    #approval_title { text-style: bold; color: #ebf5ff; margin-bottom: 1; }
    #approval_scroll { height: 1fr; min-height: 0; overflow-y: auto; }
    #approval_message { color: #c9d8e8; margin-bottom: 2; }
    #approval_actions { height: 3; min-height: 3; align-horizontal: right; }
    #approval_actions Button { margin-left: 1; }
    """

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.title_text, self.message_text = title, message

    def compose(self) -> ComposeResult:
        with Vertical(id="approval_card"):
            yield Static(self.title_text, id="approval_title")
            with VerticalScroll(id="approval_scroll", can_focus=True):
                yield Static(self.message_text, id="approval_message")
            with Horizontal(id="approval_actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="approve", variant="warning")

    @on(Button.Pressed, "#approval_actions Button")
    def approve(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")


class CheckComplete(ModalScreen[bool]):
    """Offer a clear next action; this dialog never authorizes checkout."""
    CSS = """
    CheckComplete { align: center middle; }
    #check_card { width: 64; max-width: 94%; height: auto; max-height: 90%;
                  padding: 1 2; background: #15243a; border: round #3d6383; }
    #check_scroll { height: auto; max-height: 1fr; }
    #check_actions { height: 3; align-horizontal: right; }
    #check_actions Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="check_card"):
            with VerticalScroll(id="check_scroll"):
                yield Static("Device and offer check complete", classes="section")
                yield Static("The 12-month offer has a zero-priced first phase. "
                             "This does not confirm eligibility or final cost. "
                             "Continue to review Step 2; a separate approval is required.")
            with Horizontal(id="check_actions"):
                yield Button("Later", id="check_later")
                yield Button("Continue to Step 2", id="check_continue", variant="primary")

    @on(Button.Pressed, "#check_actions Button")
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "check_continue")


class SetupNotice(ModalScreen[None]):
    CSS = """
    SetupNotice { align: center middle; }
    #notice_card { width: 64; max-width: 94%; height: auto; max-height: 90%;
                   padding: 1 2; background: #15243a; border: round #d6a34f; }
    #notice_scroll { height: auto; max-height: 1fr; }
    #notice_close { width: 100%; }
    """

    def __init__(self, title: str, instruction: str) -> None:
        super().__init__()
        self.notice_title, self.instruction = title, instruction

    def compose(self) -> ComposeResult:
        with Vertical(id="notice_card"):
            with VerticalScroll(id="notice_scroll"):
                yield Static(self.notice_title, classes="section")
                yield Static(self.instruction)
            yield Button("Got it", id="notice_close", variant="primary")

    @on(Button.Pressed, "#notice_close")
    def close_notice(self) -> None:
        self.dismiss(None)


class AccountPicker(ModalScreen[str | None]):
    """Choose a target from Android account candidates, not a billing identity."""
    CSS = """
    AccountPicker { align: center middle; background: #071526 80%; }
    #account_picker { width: 64; max-width: 94%; height: auto; padding: 1 2;
                      background: #15243a; border: round #3d6383; }
    #account_choice { width: 100%; }
    #account_picker_actions { height: 3; margin-top: 1; }
    #account_picker_actions Button { width: 1fr; }
    """
    def __init__(self, accounts: list[str]) -> None:
        super().__init__()
        self.accounts = accounts

    def compose(self) -> ComposeResult:
        with Vertical(id="account_picker"):
            yield Static("Choose a Google account on this device", classes="section")
            yield Static("These are Android account candidates, NOT proof of which "
                         "account Google Play uses. Switch accounts inside Play Store "
                         "and run Check Device & Offers to verify.")
            # Show complete addresses only in this local selection dialog so
            # two similar aliases cannot be confused; cards/logs stay masked.
            yield Select([(f"{i + 1}. {email}", email)
                          for i, email in enumerate(self.accounts)],
                         value=self.accounts[0], allow_blank=False, id="account_choice")
            with Horizontal(id="account_picker_actions"):
                yield Button("Cancel", id="account_cancel")
                yield Button("Use selected account", id="account_accept", variant="primary")

    @on(Button.Pressed, "#account_picker_actions Button")
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id != "account_accept":
            self.dismiss(None)
            return
        value = self.query_one("#account_choice", Select).value
        self.dismiss(value if isinstance(value, str) and value in self.accounts else None)


class ConnectionNotice(SetupNotice):
    """Dismissible warning for a missing Android connection."""
    CSS = SetupNotice.CSS + """
    ConnectionNotice { align: center middle; background: #081424 75%; }
    #connection_actions { height: 3; }
    #connection_actions Button { width: 1fr; min-width: 0; }
    #connection_retry { margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="notice_card"):
            with VerticalScroll(id="notice_scroll"):
                yield Static(self.notice_title, classes="section")
                yield Static(self.instruction, id="connection_instruction")
            if sys.platform == "win32" and MUMU_REMOTE_AVAILABLE:
                yield Checkbox("Create a new Android instance if MuMu has none",
                               id="connection_create_instance")
            with Horizontal(id="connection_actions"):
                if sys.platform == "win32" and MUMU_REMOTE_AVAILABLE:
                    yield Button("Start MuMu automatically", id="connection_start_mumu", variant="success")
                yield Button("Retry connection", id="connection_retry", variant="primary")
                yield Button("Got it", id="notice_close")

    def on_mount(self) -> None:
        self._retry_started: float | None = None
        self._mumu_started: float | None = None
        self._mumu_create_missing = False
        self.set_interval(1, self._update_retry_time)

    def _update_retry_time(self) -> None:
        if self._retry_started is not None:
            elapsed = int(time.monotonic() - self._retry_started)
            self.query_one("#connection_retry", Button).label = f"Retrying… {elapsed}s"
        if self._mumu_started is not None:
            elapsed = int(time.monotonic() - self._mumu_started)
            if self.query("#connection_start_mumu"):
                self.query_one("#connection_start_mumu", Button).label = f"Starting MuMu… {elapsed}s"

    @on(Button.Pressed, "#connection_start_mumu")
    def start_mumu_connection(self) -> None:
        if self._mumu_started is not None or self.app.busy:
            return
        self._mumu_create_missing = False
        if self.query("#connection_create_instance"):
            self._mumu_create_missing = bool(
                self.query_one("#connection_create_instance", Checkbox).value)
        self._mumu_started = time.monotonic()
        self.query_one("#connection_start_mumu", Button).disabled = True
        self.query_one("#connection_retry", Button).disabled = True
        self.query_one("#notice_close", Button).disabled = True
        self._update_retry_time()
        self._mumu_start_worker()

    @work(thread=True, exclusive=True, group="mumu-start")
    def _mumu_start_worker(self) -> None:
        # Existing MuMu instances are always preferred; a new Android image is
        # only created when the user explicitly opts in on the notice screen.
        try:
            serial = mumu_start_android(create_missing=self._mumu_create_missing)
            result = poll_connection()
        except Exception:
            result = ConnectionStatus("adb_error")
            serial = ""
        accounts: list[str] = []
        if serial and result.state == "connected" and detect_google_accounts:
            try:
                from device_diagnostics import locate_adb
                adb = locate_adb() or ""
                accounts = detect_google_accounts(adb, serial, timeout_seconds=30)
            except Exception:
                accounts = []
        self.app.call_from_thread(self._mumu_start_finished, result, accounts)

    def _mumu_start_finished(self, result: ConnectionStatus,
                             accounts: list[str]) -> None:
        if not self.is_mounted:
            return
        self._mumu_started = None
        if self.query("#connection_start_mumu"):
            self.query_one("#connection_start_mumu", Button).disabled = False
            self.query_one("#connection_start_mumu", Button).label = "Start MuMu automatically"
        self.query_one("#connection_retry", Button).disabled = False
        self.query_one("#notice_close", Button).disabled = False
        if result.state == "connected":
            self.app._connection_polled(result)
            self.dismiss(None)
            if accounts:
                masked = ", ".join(mask_email(a) for a in accounts[:3])
                self.app._set_status("MuMu started — account detected")
                self.app._set_message(
                    f"Google account on device: {masked}. "
                    "Run Check Device & Offers to verify it is the active Play Store account."
                )
            else:
                self.app._set_status("MuMu started — no Google account signed in")
                self.app._set_message(
                    "Android is connected, but no Google account is signed in on this "
                    "MuMu instance. Open Play Store in MuMu and sign in once; the "
                    "session is saved for future runs."
                )
        else:
            self.query_one("#connection_instruction", Static).update(
                "MuMu did not report a device yet. Wait for Android to finish booting, "
                "enable ADB debugging, then press Retry connection."
            )

    @on(Button.Pressed, "#connection_retry")
    def retry_connection(self) -> None:
        if self._retry_started is not None or self.app.busy:
            return
        self._retry_started = time.monotonic()
        self.query_one("#connection_retry", Button).disabled = True
        self.query_one("#notice_close", Button).disabled = True
        self._update_retry_time()
        self._retry_connection_worker()

    @work(thread=True, exclusive=True, group="connection-retry")
    def _retry_connection_worker(self) -> None:
        try:
            if sys.platform == "win32":
                from device_diagnostics import locate_adb
                from setup_env import connect_emulator
                adb = locate_adb()
                if adb:
                    connect_emulator(adb)
            result = poll_connection()
        except Exception:
            result = ConnectionStatus("adb_error")
        self.app.call_from_thread(self._retry_finished, result)

    def _retry_finished(self, result: ConnectionStatus) -> None:
        if not self.is_mounted:
            return
        self._retry_started = None
        self.query_one("#connection_retry", Button).disabled = False
        self.query_one("#connection_retry", Button).label = "Retry connection"
        self.query_one("#notice_close", Button).disabled = False
        if result.state == "connected":
            self.app._connection_polled(result)
        else:
            if getattr(self.app, "linux_mode", False):
                guidance = ("Still unable to connect. Unlock the Android phone, check USB "
                            "debugging and Linux USB permissions, then press Retry connection.")
            elif getattr(self.app, "macos_mode", False):
                guidance = ("Still unable to connect. Check the Android device or emulator, "
                            "approve ADB debugging and press Retry connection.")
            else:
                guidance = ("Still unable to connect. Close and reopen MuMu, wait for Android "
                            "to boot, enable ADB debugging, then press Retry connection.")
            self.query_one("#connection_instruction", Static).update(guidance)


def is_macos_platform() -> bool:
    return sys.platform == "darwin"


class GoogleAITUI(App[None]):
    TITLE = "ePlus AI Toolkit"
    SUB_TITLE = "ePlus.DEV"
    BINDINGS = [
        ("d", "toggle_debug", "Debug"),
        ("r", "run_selected", "Run step"),
        ("q", "quit_safely", "Quit"),
    ]
    CSS = """
    Screen { background: #081424; color: #e8f2fc; }
    Header, Footer { background: #11243e; color: #d7e8fa; }
    #screen { height: 1fr; min-height: 0; padding: 0 1; }
    #brand {
        height: 3; background: #122741; border: round #28628d;
        color: #f0f8ff; padding: 0 2; align: left middle;
    }
    #support_label { width: auto; height: 1; color: #c8dff1; }
    #support_link {
        width: auto; height: 1; color: #75d8ff;
        text-style: underline bold;
    }
    #support_link:hover { color: #e6f8ff; }
    #credit { height: 1; color: #87b8d4; }
    #credit Static { width: auto; height: 1; color: #87b8d4; }
    #credit Link { width: auto; height: 1; color: #75d8ff; text-style: underline; }
    #credit Link:hover { color: #e6f8ff; }
    #details { height: 3; margin-bottom: 1; }
    #device, #account {
        width: 1fr; height: 3; background: #12263d;
        border: round #295a7e; padding: 0 1;
    }
    #account { margin-left: 1; }
    #content { height: 1fr; min-height: 0; }
    #controls {
        width: 26; min-width: 26; min-height: 0;
        border: round #2b5577; background: #101f35;
        padding: 0 1; overflow-y: auto; scrollbar-size: 1 1;
    }
    #results { width: 1fr; min-width: 26; min-height: 0; margin-left: 1; overflow-y: auto; scrollbar-size: 1 1; }
    .section { color: #6cdbf8; text-style: bold; margin: 0 0 1 0; }
    #workflow { height: auto; margin-bottom: 1; }
    .workflow-step { color: #b4cbe0; height: 1; }
    #nav_help, #diagnose, #debug, #reload, #exit {
        width: 100%; height: 3; margin: 0;
    }
    #nav_home, #nav_quick, #advanced, #sequence, #run_all { display: none; }
    #choose_account { width: 18; min-width: 18; height: 3; margin: 0 0 0 1; }
    #quickstart { display: block; width: 100%; height: 3; margin: 0; }
    #quickstart.checking:disabled, #run.checking:disabled {
        background: #123b31; color: #e5fff2; text-style: bold;
        text-opacity: 1; opacity: 1;
    }
    #advanced_toggle { width: 100%; height: 3; margin: 0; }
    #refresh_offers { display: none; width: 100%; height: 3; }
    #workflow_actions { display: none; height: auto; margin-top: 1; border-top: solid #285d83; padding-top: 1; }
    #status_panel { min-height: 8; overflow-y: auto; scrollbar-size: 1 1; }

    #welcome_panel {
        height: auto; max-height: 60%; min-height: 8; overflow-y: auto; scrollbar-size: 1 1;
        border: round #2b97bc; background: #112b49; padding: 0 2; margin-bottom: 1;
    }
    #welcome { color: #91e3ff; text-style: bold; height: 1; }
    #quick_hint { color: #afcce3; height: auto; margin: 0; }
    #read_only { color: #81c6df; height: auto; margin-top: 1; }
    #workflow_actions { height: auto; }
    #form_row { height: auto; }
    #mode_field { display: none; }
    .field { width: 1fr; min-width: 12; height: auto; }
    #step_field { margin-left: 1; }
    #mode_label, #advanced_label {
        color: #8edff1; text-style: bold; height: 1; margin: 0 0 1 0;
    }
    #mode, #step { width: 100%; margin: 0; }
    #run_row { layout: vertical; width: 1fr; height: auto; margin: 1 0 0 1; }
    #run { width: 100%; min-width: 0; height: 3; margin: 0; content-align: center middle; }
    #mode_hint {
        width: 1fr; height: auto; color: #a5c8df;
        padding: 1 0 0 0;
    }
    #next, #stop { display: none; width: 100%; height: 3; margin-top: 1; }
    #status_panel {
        height: 1fr; min-height: 8; border: round #285a80;
        background: #101f35; padding: 0 1;
    }
    #state { color: #f2f9ff; text-style: bold; margin: 1 0 0 0; }
    #message { color: #c5ddee; }
    #step_result { color: #8cdfbb; height: auto; }
    #offers_title { display: none; color: #8edff1; text-style: bold; margin-top: 1; }
    #offer_view {
        display: none; height: 8; min-height: 4; margin-top: 1;
        border: round #285a80; background: #081a2d;
    }
    #elapsed, #activity { color: #95c4dd; }
    #debug_log {
        display: none; height: 7;
        border: round #294b67; background: #071528;
    }
    #manual { display: none; width: 100%; height: 3; }
    #hint { color: #83b1cf; height: auto; margin-top: 1; }
    Screen.compact #brand { height: 1; border: none; padding: 0 1; }
    Screen.compact #credit, Screen.compact #hint { display: none; }
    Screen.compact #details { height: 1; }
    Screen.compact #device, Screen.compact #account { height: 1; border: none; }
    Screen.compact #choose_account { height: 1; min-height: 1; border: none; padding: 0; }
    Screen.compact #welcome_panel { padding: 0 1; }
    Screen.compact #quick_hint, Screen.compact #read_only { display: none; }
    Screen.compact #welcome { display: none; }
    Screen.compact #debug_log { height: 5; }
    Screen.compact #welcome_panel { padding: 0 1; }
    Screen.compact #welcome_panel { min-height: 7; }
    Screen.compact #status_panel { height: 1fr; min-height: 5; }
    Screen.compact #status_panel .section, Screen.compact #state { margin: 0; }
    Screen.compact #history { display: none; }
    Screen.compact #workflow_actions { margin-top: 0; padding-top: 0; border-top: none; }
    Screen.compact #mode_label, Screen.compact #advanced_label { margin-bottom: 0; }
    Screen.compact #run_row { margin-top: 0; margin-right: 1; offset: 1 0; }
    Screen.compact #mode_hint { padding: 0; }
    Screen.compact #quickstart, Screen.compact #advanced_toggle { height: 3; }
    Screen.narrow #form_row { layout: vertical; }
    Screen.narrow #step_field { margin: 1 0 0 0; }
    Screen.narrow #run_row { layout: vertical; }
    Screen.narrow #run { width: 1fr; margin-left: 1; }
    Screen.narrow #mode_hint { padding: 1 0 0 0; }
    Screen.narrow #controls { width: 21; min-width: 21; }
    Screen.tiny #content { layout: vertical; }
    Screen.tiny #controls {
        width: 100%; min-width: 0; height: auto;
        max-height: 11; overflow-y: auto; margin-bottom: 1;
    }
    Screen.tiny #workflow { display: none; }
    Screen.tiny #results { width: 100%; min-width: 0; margin-left: 0; }
    """

    def __init__(self, *, repo: Path, selected_step: int = 1,
                 start_sequence: bool = False, initial_debug: bool = False,
                 show_advanced: bool = False) -> None:
        super().__init__()
        self.repo = repo.resolve()
        self.initial_step = validate_step(selected_step)
        self.macos_mode = is_macos_platform()
        self.linux_mode = sys.platform == "linux" and not self.macos_mode
        self.native_mode = self.macos_mode or self.linux_mode
        self.start_sequence = start_sequence
        self.show_advanced = show_advanced
        self.debug_visible = initial_debug
        self.busy = False
        self._diagnosing = False
        self.sequence = False
        self.current_step: int | None = None
        self._child: subprocess.Popen[str] | None = None
        self._last_output: deque[str] = deque(maxlen=350)
        self._run_output: deque[str] = deque(maxlen=35)
        self._stop_after_step = False
        self._cancel_requested = False
        self._exit_after_step = False
        self._last_outcome: Outcome | None = None
        self._verified_account: str | None = None
        self._selected_account: str | None = None
        self._history_account: str | None = None
        self._environment_checked = False
        self._offer_ready_account: str | None = None
        self._accounts_loading = False
        self._offer_check_ready = False
        self._initial_offer_ready = False
        self._checked_device: str | None = None
        self._offer_ready_device: str | None = None
        self._connection_state: ConnectionStatus | None = None
        self._connection_polling = False
        self._connection_generation = 0
        self._started_at: float | None = None
        self._last_activity_at: float | None = None
        self.advanced_visible = False
        self.more_modes_visible = False
        self._last_selected_step = self.initial_step
        self._last_mode = "single"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="screen"):
            with Horizontal(id="brand"):
                yield Static("Support: ", id="support_label")
                yield Link("https://t.me/eplus_news", url="https://t.me/eplus_news",
                           tooltip="Telegram support channel", id="support_link")
            with Horizontal(id="credit"):
                yield Static("  Original ")
                yield Link("@gylive", url="https://github.com/gylive",
                           tooltip="Original author on GitHub", id="original_author")
                yield Static("  |  Maintained ")
                yield Link("@hoangsvit", url="https://github.com/hoangsvit",
                           tooltip="Maintainer on GitHub", id="maintainer")
            with Horizontal(id="details"):
                yield Static("Device  •  Auto-detect", id="device")
                yield Static("Account  •  Not checked", id="account")
                yield Button("Choose account", id="choose_account")
            with Horizontal(id="content"):
                with Vertical(id="controls"):
                    yield Static("STEPS", classes="section")
                    with Vertical(id="workflow"):
                        for stage in STAGES:
                            yield Static(f"○  {stage.number}. {stage.label}",
                                         id=f"stage_{stage.number}", classes="workflow-step")
                    yield Button("Help & Guide", id="nav_help")
                    yield Button("Diagnose Android connection", id="diagnose")
                    yield Button("Show technical details", id="debug")
                    yield Button("Reload toolkit", id="reload")
                    yield Button("Exit toolkit", id="exit")
                with Vertical(id="results"):
                    yield TechnicalLog(id="debug_log", wrap=True, highlight=False, markup=False)
                    with Vertical(id="welcome_panel"):
                        yield Static(
                            "GET STARTED · OFFER CHECK" if self.linux_mode
                            else "GET STARTED · READ-ONLY CHECK", id="welcome")
                        yield Static(
                            "Check Android offers without opening checkout." if self.linux_mode
                            else "Check MuMu connection and Google AI offers without opening checkout.",
                            id="quick_hint")
                        yield Button("Check Device & Offers", id="quickstart", variant="success")
                        yield Button("Stop after current step", id="stop", disabled=True)
                        yield Button("Verified in Google Play → Next", id="next", disabled=True)
                        yield Button("Choose another step  +", id="advanced_toggle")
                        with Vertical(id="workflow_actions"):
                            yield Button("Check Device & Offers — Step 1", id="refresh_offers", variant="success")
                            with Horizontal(id="form_row"):
                                with Vertical(classes="field", id="step_field"):
                                    yield Static("Choose a step", id="advanced_label")
                                    yield Select(
                                        [(f"{stage.number}. {stage.label}", stage.number)
                                         for stage in STAGES] + [("More run options…", 99)],
                                        value=self.initial_step, id="step", allow_blank=False)
                                with Vertical(classes="field", id="mode_field"):
                                    yield Static("After this step", id="mode_label")
                                    yield Select([
                                        ("Stop after this step", "single"),
                                        ("Offer the next step", "from"),
                                        ("Start at Step 2", "all"),
                                    ], value="single", id="mode", allow_blank=False)
                            with Horizontal(id="run_row"):
                                yield Button("Run this step", id="run", variant="primary")
                                yield Static("Runs one step; no next step is automatic.", id="mode_hint")
                            yield Button("Run sequence from step", id="sequence")
                            yield Button("Run all", id="run_all")
                            yield Static("Steps 2–5 can open checkout and require confirmation.", id="read_only")
                    with Vertical(id="status_panel"):
                        yield Static("LIVE STATUS", classes="section")
                        yield Static("Waiting for your action.", id="activity")
                        yield Static("History appears after selecting or verifying an account.", id="history", markup=False)
                        yield Static("Ready — no check started", id="state")
                        yield Static("Check the device and offers to begin.", id="message")
                        yield Static("", id="step_result")
                        yield Static("AVAILABLE OFFERS · FIRST PHASE ONLY", id="offers_title")
                        yield RichLog(id="offer_view", wrap=True, highlight=False, markup=False)
                        yield Static("Elapsed: --", id="elapsed")
                        yield Button("Completed action on Android", id="manual", disabled=True)
                        yield Static("Review Google Play pricing and renewal terms "
                                     "before each checkout.", id="hint")
        yield Footer()

    def on_resize(self, event) -> None:
        self.screen.set_class(event.size.height < 27, "compact")
        self.screen.set_class(event.size.width < 90, "narrow")
        self.screen.set_class(event.size.width < 62, "tiny")

    @on(Button.Pressed, "#nav_home")
    def home_pressed(self) -> None:
        if not self.busy:
            self._set_message("Select Check Device & Offers to begin.")

    @on(Button.Pressed, "#nav_quick")
    def quick_pressed(self) -> None:
        self.quickstart_pressed()

    @on(Button.Pressed, "#reload")
    def reload_pressed(self) -> None:
        if self.busy or self._diagnosing or self._accounts_loading:
            return
        try:
            os.execv(sys.executable, [sys.executable, str(self.repo / "tui.py")])
        except OSError:
            self.notify("Could not reload toolkit. Close and reopen it.", severity="error")

    @on(Button.Pressed, "#nav_help")
    def help_pressed(self) -> None:
        self._set_message("Start with a read-only offer check. Steps 2-5 can request "
                          "real payments; verify the account, terms and renewal date.")

    @on(Button.Pressed, "#choose_account")
    def choose_account_pressed(self) -> None:
        if self.busy or self._diagnosing or self._accounts_loading:
            return
        self._accounts_loading = True
        self.query_one("#choose_account", Button).disabled = True
        self._set_status("Reading Android accounts...")
        self._set_message("Looking for accounts on the connected device. "
                          "This does not open checkout or change the active Play Store account.")
        self.discover_account_candidates()

    @work(thread=True, exclusive=True, group="account-candidates")
    def discover_account_candidates(self) -> None:
        try:
            lookup = self.repo / "scripts" / "runtime-lookup.py"
            def read_value(command: str, serial: str = "") -> str:
                args = [sys.executable, str(lookup), command]
                if serial:
                    args.extend(["--serial", serial])
                result = subprocess.run(args, capture_output=True, text=True,
                                        errors="replace", timeout=60, check=False)
                line = result.stdout.strip()
                match = re.fullmatch(r"RUNTIME_RESULT:([A-Za-z0-9+/=]*)", line)
                if result.returncode or not match:
                    raise RuntimeError("Android account lookup failed")
                return base64.b64decode(match.group(1), validate=True).decode("utf-8")
            serial = self._checked_device or read_value("device")
            if not serial or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,90}", serial):
                raise RuntimeError("No unambiguous Android device was found")
            raw = json.loads(read_value("play-accounts", serial))
            if not isinstance(raw, list):
                raise ValueError("Android account list was invalid")
            accounts = sorted({x.lower() for x in raw if isinstance(x, str) and
                               re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", x)})
            self.call_from_thread(self._account_candidates_loaded, accounts)
        except (OSError, UnicodeError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            self.call_from_thread(self._account_candidates_loaded, [])

    def _account_candidates_loaded(self, accounts: list[str]) -> None:
        self._accounts_loading = False
        self.query_one("#choose_account", Button).disabled = self.busy or self._diagnosing
        if not accounts:
            self._set_status("Account list unavailable")
            self._set_message("Android did not provide a readable account list. "
                              "Open Google Play, select the intended profile manually, "
                              "then run Check Device & Offers.")
            return
        self.push_screen(AccountPicker(accounts), self._account_chosen)

    def _account_chosen(self, account: str | None) -> None:
        if not account or self.busy:
            return
        self._selected_account = account
        self._show_history(account)
        self._verified_account = None
        self._offer_ready_account = None
        self._offer_check_ready = False
        self._initial_offer_ready = False
        self._offer_ready_device = None
        self.query_one("#account", Static).update("Account  •  " + escape(mask_email(account)) + " (selected)")
        self.query_one("#quickstart", Button).label = "Check Device & Offers"
        self._update_run_mode()
        self._set_status("Account selected — not yet verified")
        self._set_message("In Google Play, switch to the selected account. "
                          "Run Check Device & Offers. Selecting a candidate alone "
                          "never authorizes a checkout.")

    @on(Button.Pressed, "#diagnose")
    def diagnose_pressed(self) -> None:
        if self.busy or self._diagnosing:
            return
        self._invalidate_connection_polls()
        self._offer_check_ready = False
        self._initial_offer_ready = False
        self._offer_ready_device = None
        self.query_one("#quickstart", Button).label = "Check Device & Offers"
        self._diagnosing = True
        self.query_one("#diagnose", Button).disabled = True
        self.query_one("#quickstart", Button).disabled = True
        self.query_one("#quickstart", Button).display = True
        self.query_one("#next", Button).display = False
        self.query_one("#next", Button).disabled = True
        self.sequence = False
        self.query_one("#run", Button).disabled = True
        self._set_status("Inspecting Android connection...")
        self._set_message(
            "Checking local ADB, Android transport and Google One. No checkout, "
            "installation or emulator reset; adb devices may start the local ADB server."
        )
        self.inspect_android_connection()

    @work(thread=True, exclusive=True, group="android-connection")
    def inspect_android_connection(self) -> None:
        try:
            diagnosis = inspect_connection()
        except Exception:
            diagnosis = ConnectionDiagnosis(
                "error", "Connection check could not complete", (),
                "Check the ADB installation and try again. No checkout was started.",
            )
        self.call_from_thread(self._diagnosis_done, diagnosis)

    def _diagnosis_done(self, diagnosis: ConnectionDiagnosis) -> None:
        self._diagnosing = False
        self.query_one("#diagnose", Button).disabled = False
        self.query_one("#quickstart", Button).disabled = False
        self.query_one("#run", Button).disabled = False
        self._set_status(diagnosis.headline)
        self._set_message(diagnosis.guidance)
        self.query_one("#step_result", Static).update("\n".join(diagnosis.details))
        self.query_one("#offers_title", Static).display = False
        self.query_one("#offer_view", RichLog).display = False
        self._update_run_mode()

    def on_mount(self) -> None:
        self._render_history(latest_history(self.repo),
                             "Previous run · select an account and run Step 1 to verify")
        self.set_interval(1, self._refresh_elapsed)
        self.set_interval(5, self._poll_connection)
        if self.native_mode:
            self.query_one("#device", Static).update("Device  •  Auto-detect")
            self.query_one("#quick_hint", Static).update(
                "Check Android and Google AI offers before any subscription step."
            )
            self.query_one("#read_only", Static).update(
                "Steps 2–5 can charge money. Confirm account, future price and renewal date."
            )
        if self.linux_mode:
            self._set_message(
                "Offer checks can change Android settings. Use Diagnose Android connection to inspect USB first."
            )
        self.query_one("#debug_log", RichLog).display = self.debug_visible
        self.query_one("#debug", Button).label = "Hide technical details" if self.debug_visible else "Show technical details"
        if self.start_sequence:
            self.query_one("#mode", Select).value = "from"
            self.more_modes_visible = True
            self.advanced_visible = True
            self.query_one("#workflow_actions", Vertical).display = True
            self.query_one("#advanced_toggle", Button).label = "Hide step selection  −"
            self.query_one("#mode_field", Vertical).display = True
        elif self.show_advanced:
            self.advanced_visible = True
            self.query_one("#workflow_actions", Vertical).display = True
            self.query_one("#advanced_toggle", Button).label = "Hide step selection  −"
        self._update_run_mode()

    def _invalidate_connection_polls(self) -> None:
        """A pre-operation poll must never overwrite a newer stage or diagnosis."""
        self._connection_generation += 1
        self._connection_polling = False

    def _poll_connection(self) -> None:
        if (self.busy or self._diagnosing or self._connection_polling or
                (isinstance(self.screen, ModalScreen) and not isinstance(self.screen, ConnectionNotice))):
            return
        self._connection_polling = True
        self._inspect_connection_periodically(self._connection_generation)

    @work(thread=True, exclusive=True, group="connection-monitor")
    def _inspect_connection_periodically(self, generation: int) -> None:
        try:
            result = poll_connection()
        except Exception:
            result = ConnectionStatus("adb_error")
        try:
            self.call_from_thread(self._connection_polled, result, generation)
        except RuntimeError:
            pass  # The app was closed while the ADB command was in flight.

    def _connection_polled(self, result: ConnectionStatus, generation: int | None = None) -> None:
        if generation is not None and generation != self._connection_generation:
            return
        self._connection_polling = False
        if (self.busy or self._diagnosing or
                (isinstance(self.screen, ModalScreen) and not isinstance(self.screen, ConnectionNotice))):
            return
        previous = self._connection_state
        self._connection_state = result
        changed = previous != result
        if result.state == "connected":
            label = "Android connected (ADB)" if len(result.devices) == 1 else "Multiple ADB devices"
            self.query_one("#device", Static).update("Device  •  " + label)
            if changed and previous and previous.state != "connected":
                self.notify("Android connected. Run Check Device & Offers.")
            if isinstance(self.screen, ConnectionNotice):
                self.screen.dismiss(None)
        else:
            self.query_one("#device", Static).update("Device  •  Not connected")
        # Compare against the device Step 1 actually checked, not the older
        # idle observation. ADB polling alone never enables paid steps.
        checked_device_missing = self._offer_check_ready and (
            not self._offer_ready_device or self._offer_ready_device not in result.devices
        )
        if (result.state != "connected" or checked_device_missing or
                (not self._offer_check_ready and previous and previous.state == "connected" and changed)):
            self._offer_check_ready = False
            self._initial_offer_ready = False
            self._offer_ready_device = None
            self.sequence = False
            self.query_one("#next", Button).display = False
            self.query_one("#next", Button).disabled = True
            self.query_one("#quickstart", Button).display = True
            self.query_one("#quickstart", Button).label = "Check Device & Offers"
            self._update_run_mode()
        if result.state in ("missing_adb", "disconnected", "adb_error") and changed:
            if isinstance(self.screen, ConnectionNotice):
                return
            if result.state == "missing_adb":
                guidance = ("Run Check Device & Offers to prepare ADB on Windows. On macOS, "
                            "install Android SDK Platform-Tools and reconnect Android.")
                title = "ADB not found"
            elif result.state == "adb_error":
                title = "ADB is not responding"
                guidance = "Check the ADB installation, then run Check Device & Offers."
            else:
                title = "Android is not connected"
                guidance = "Start MuMu, enable ADB debugging and approve any Android authorization prompt. The connection will be checked again automatically."
            self.push_screen(ConnectionNotice(title, guidance))

    @on(Button.Pressed, "#advanced_toggle")
    def advanced_pressed(self) -> None:
        self.advanced_visible = not self.advanced_visible
        panel = self.query_one("#workflow_actions", Vertical)
        panel.display = self.advanced_visible
        self.query_one("#advanced_toggle", Button).label = (
            "Hide step selection  −" if self.advanced_visible
            else "Choose another step  +"
        )
        target = "#mode_hint" if self.advanced_visible else "#advanced_toggle"
        self.call_after_refresh(
            lambda: self._reveal_step_control(target)
        )

    def _reveal_step_control(self, selector: str) -> None:
        self.query_one(selector).scroll_visible(animate=False, immediate=True)
        if selector == "#run":
            # Keep the bottom border inside the viewport on compact terminals.
            self.query_one("#results").scroll_relative(y=1, animate=False, immediate=True)

    def toggle_more_modes(self) -> None:
        if self.busy or self._diagnosing:
            return
        self.more_modes_visible = not self.more_modes_visible
        self.query_one("#mode_field", Vertical).display = self.more_modes_visible
        if not self.more_modes_visible:
            self.query_one("#mode", Select).value = "single"
        self._update_run_mode()
        self.call_after_refresh(
            lambda: self._reveal_step_control("#mode_hint")
        )

    @on(Select.Changed, "#mode")
    def mode_changed(self, event: Select.Changed) -> None:
        self._update_run_mode()

    @on(Select.Changed, "#step")
    def step_changed(self, event: Select.Changed) -> None:
        if event.value == 99:
            # Access advanced modes through the existing dropdown: never
            # attempt to run this synthetic menu item as a stage.
            self.query_one("#step", Select).value = self._last_selected_step
            self.toggle_more_modes()
            return
        if isinstance(event.value, int) and event.value in range(6):
            self._last_selected_step = event.value
        self._update_run_mode()

    def _update_run_mode(self) -> None:
        if self.busy:
            return
        # Textual can deliver a queued Select.Changed while its screen is
        # being unmounted (especially on Windows). Ignore stale UI events.
        if not len(self.query("#mode")) or not len(self.query("#step")):
            return
        mode = self.query_one("#mode", Select).value
        step = self.query_one("#step", Select).value
        if mode == "all":
            if self.query_one("#step", Select).value != 2:
                self.query_one("#step", Select).value = 2
            self.query_one("#step", Select).disabled = True
            self.query_one("#run", Button).label = "Start Step 2"
            hint = "After each step, review Google Play before continuing."
        elif mode == "from":
            self.query_one("#step", Select).disabled = False
            self.query_one("#run", Button).label = "Run this step"
            hint = ("Choose Step 2–5 to continue through later steps."
                    if step in (0, 1) else "After this step, verify Google Play before the next one.")
        else:
            self.query_one("#step", Select).disabled = False
            self.query_one("#run", Button).label = "Run this step"
            hint = "Only this step will run. Steps 2–5 can request payment."
        paid_selected = mode == "all" or (mode in ("single", "from") and step in (2, 3, 4, 5))
        self.query_one("#refresh_offers", Button).display = paid_selected and not self._offer_check_ready
        self.query_one("#run", Button).disabled = paid_selected and not self._offer_check_ready
        if paid_selected and not self._offer_check_ready:
            hint = "A fresh Step 1 check is required for this session, device and account. Use the green Step 1 button above."
            self.query_one("#run", Button).label = "Step 1 check required"
        elif paid_selected and mode in ("single", "from"):
            hint = ("Steps 2-5 unlocked for selection. Each paid run rechecks "
                    "the Play account, device and selected offer.")
        self.query_one("#mode_hint", Static).update(hint)

    def action_toggle_advanced(self) -> None:
        self.advanced_pressed()

    def _refresh_elapsed(self) -> None:
        # Textual may fire a queued interval while the screen is unmounting.
        # Avoid selecting widgets after shutdown (especially on Windows).
        if not self.is_mounted or not len(self.query("#elapsed")):
            return
        if not self.busy or self._started_at is None:
            return
        elapsed = int(time.monotonic() - self._started_at)
        self.query_one("#elapsed", Static).update(f"Elapsed: {elapsed // 60:02d}:{elapsed % 60:02d}")
        if self.current_step == 1:
            self.query_one("#quickstart", Button).label = f"Checking offers… {elapsed}s"
        elif self.current_step is not None:
            self.query_one("#quickstart", Button).label = f"Running Step {self.current_step}… {elapsed}s"
        if self.current_step is not None:
            self.query_one("#run", Button).label = f"Running Step {self.current_step}… {elapsed}s"
        if self._last_activity_at is not None:
            idle = int(time.monotonic() - self._last_activity_at)
            if idle >= 30:
                self.query_one("#activity", Static).update(
                    f"No new output for {idle}s. Check "
                    f"{'Android' if self.native_mode else 'MuMu'} or technical details."
                )

    def _set_workflow(self, step: int, state: str) -> None:
        marker = {"running": "●", "done": "✓", "review": "!"}.get(state, "○")
        self.query_one(f"#stage_{step}", Static).update(
            f"{marker}  {step}. {STAGES[step].label}"
        )

    def _set_message(self, value: str) -> None:
        self.query_one("#message", Static).update(value)

    def _set_status(self, value: str) -> None:
        self.query_one("#state", Static).update(value)

    def _selected(self) -> int:
        return validate_step(int(self.query_one("#step", Select).value))

    def _confirm_stage(self, step: int, sequence: bool,
                       previous_verified: bool = False) -> None:
        if self.busy or self._diagnosing:
            return
        if step >= 2 and not self._offer_check_ready:
            self._set_status("Check Device & Offers first")
            self._set_message("Connect Android and complete the offer check before starting a paid step.")
            return
        # Block a known device change immediately. The final _start gate also
        # rejects an unknown/undiscovered connection before any paid command.
        if step >= 2 and self._offer_ready_device and self._connection_state and (
                self._connection_state.state != "connected"
                or self._offer_ready_device not in self._connection_state.devices):
            self._offer_check_ready = False
            self._set_status("Android device changed")
            self._set_message("Reconnect the device checked in Step 1 and run Check Device & Offers again. No checkout started.")
            return
        if step == 2:
            title = "Start initial 12-month checkout?"
            message = ("Confirm this Google Play account has NO active Google One subscription. "
                       "This step can automatically press Subscribe and create real charges.")
        elif step >= 3:
            title = f"Start Step {step} (+6 months)?"
            message = ("Google AI Pro must already be active. Verify the previous "
                       "subscription and renewal date in Google Play. Google may reject "
                       "additional free offers or apply paid renewal terms.")
        else:
            self._start(step, sequence)
            return
        account = self._selected_account or self._offer_ready_account
        if step >= 2 and account:
            message += f" Expected account: {mask_email(account)}. Check the checkout account."
        if step in (2, 3, 4):
            number = step - 1
            message += (
                f"\n\nCard check {number} of 3 in the reported example: "
                "about US$1 each, NOT one US$3 payment. "
                "It may be a temporary hold, not a final charge. "
                "Verify the amount and renewal date in Google Play. "
                "24 months is not guaranteed."
            )
            if step == 2:
                message += " Have at least US$3 available for the reported three-step sequence."
        elif step == 5:
            message += (
                "\n\nStep 5 is optional, outside the 3 x US$1 / 24-month example. "
                "Check the actual amount and renewal terms in Google Play."
            )
        if previous_verified:
            message = ("Confirm the previous stage ACTUALLY changed the Google Play "
                       "subscription/renewal date. " + message)
        if step >= 2 and step == self.current_step and self._last_outcome and self._last_outcome.code not in (
                "listed", "environment_ok", "checkout_requested"):
            message = ("RETRY SAFETY: Check Google Play subscriptions, pending transactions "
                       "and payment history first. Do not repeat if a transaction is "
                       "pending or the plan already changed. This retries only the "
                       "selected step, with a fresh environment, account and offer check.\n\n"
                       + message)
        self.push_screen(Approval(title, message),
                         lambda approved: self._start(step, sequence) if approved else None)

    @on(Button.Pressed, "#quickstart")
    def quickstart_pressed(self) -> None:
        if self._initial_offer_ready and self._offer_check_ready:
            self._confirm_stage(2, True)
        else:
            self._confirm_stage(1, False)

    @on(Button.Pressed, "#refresh_offers")
    def refresh_offers_pressed(self) -> None:
        self._confirm_stage(1, False)

    @on(Button.Pressed, "#run")
    def selected_pressed(self) -> None:
        if self.busy or self._diagnosing:
            return
        mode = self.query_one("#mode", Select).value
        if mode == "all":
            self._confirm_stage(2, True)
        elif mode == "from":
            step = self._selected()
            if step < 2:
                self._set_message("Choose Step 2-5 to start a guided checkout sequence.")
                return
            self._confirm_stage(step, True)
        else:
            self._confirm_stage(self._selected(), False)

    @on(Button.Pressed, "#sequence")
    def sequence_pressed(self) -> None:
        step = self._selected()
        if step < 2:
            self._set_message("Choose Step 2, 3, 4 or 5 to start the subscription sequence.")
            return
        self._confirm_stage(step, True)

    @on(Button.Pressed, "#next")
    def next_pressed(self) -> None:
        if self.busy or not self.sequence or self.current_step is None:
            return
        if self._last_outcome is None or not self._last_outcome.can_review:
            return
        next_step = self.current_step + 1
        if next_step > 5:
            return
        self._save_history(self._history_account, self.current_step, "user_verified")
        self._confirm_stage(next_step, True, previous_verified=True)

    def _show_history(self, account: str) -> None:
        self._render_history(read_history(self.repo, account),
                             f"Recent history · {mask_email(account)}")

    def _render_history(self, entries: list, title: str) -> None:
        labels = {
            "started": "Started; outcome unverified",
            "checkout_requested": "Checkout submitted; verify in Google Play",
            "user_verified": "User verified in Google Play",
            "listed": "Offers loaded",
            "account_unverified": "Account could not be verified",
            "wrong_account": "Account mismatch",
        }
        lines = [title, "↺ = previously attempted; verify current device and account."]
        if not self.busy:
            for stage in STAGES:
                self._set_workflow(stage.number, "pending")
            for entry in entries:
                if isinstance(entry, dict) and entry.get("step") in range(6):
                    step = entry["step"]
                    self.query_one(f"#stage_{step}", Static).update(
                        f"↺  {step}. {STAGES[step].label}")
        for entry in reversed(entries[-6:]):
            if isinstance(entry, dict) and isinstance(entry.get("step"), int):
                status = labels.get(entry.get("status"), "Stopped; review Google Play before retrying")
                lines.append(f"Step {entry['step']} · {status} · {entry.get('at', '')}")
        if not entries:
            lines.append("No saved runs.")
        self.query_one("#history", Static).update("\n".join(lines))

    def _save_history(self, account: str | None, step: int, status: str) -> None:
        try:
            record_history(self.repo, account or "", step, status)
            if account:
                self._show_history(account)
            else:
                self._render_history(read_history(self.repo, ""),
                                     "Recent history · account not verified")
        except OSError:
            self.notify("Could not save local step history.", severity="warning")

    @on(Button.Pressed, "#stop")
    def stop_pressed(self) -> None:
        if not self.busy:
            return
        if self.current_step in (0, 1):
            self._cancel_readonly()
        else:
            self._stop_after_step = True
            self._set_message("No later step will run. Finish or review the checkout on Android; the current transaction cannot be force-stopped.")
            self.query_one("#stop", Button).disabled = True

    def _cancel_readonly(self) -> None:
        if not self.busy or self.current_step not in (0, 1) or self._cancel_requested:
            return
        self._cancel_requested = True
        self._stop_after_step = True
        self._set_status("Stopping read-only check...")
        self._set_message("Stopping the local check; no checkout was started.")
        self.query_one("#stop", Button).disabled = True
        self.terminate_readonly_child()

    @work(thread=True, exclusive=True, group="read-only-cancel")
    def terminate_readonly_child(self) -> None:
        child = self._child
        if child is None or child.poll() is not None:
            return
        try:
            if os.name == "nt":
                result = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                        capture_output=True, text=True, timeout=8, check=False)
                if result.returncode != 0 and child.poll() is None:
                    raise OSError("taskkill failed")
            else:
                os.killpg(child.pid, signal.SIGTERM)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.call_from_thread(self._set_message,
                                  f"Cannot stop the check: {error}. Open technical details.")

    @on(Button.Pressed, "#exit")
    def exit_pressed(self) -> None:
        self.action_quit_safely()

    @on(Button.Pressed, "#manual")
    def manual_pressed(self) -> None:
        child = self._child
        if child and child.stdin:
            try:
                child.stdin.write("\n")
                child.stdin.flush()
                self.query_one("#manual", Button).disabled = True
                self.query_one("#manual", Button).display = False
            except (OSError, ValueError):
                self._set_message("Could not continue the Android prompt. See Debug.")

    @on(Button.Pressed, "#debug")
    def debug_pressed(self) -> None:
        self.action_toggle_debug()

    def action_toggle_debug(self) -> None:
        self.debug_visible = not self.debug_visible
        log = self.query_one("#debug_log", RichLog)
        log.display = self.debug_visible
        if self.debug_visible:
            # Rebuild from the bounded current-run buffer: lines received while
            # details were hidden must not disappear or be duplicated.
            log.clear()
            last_index = len(self._last_output) - 1
            for index, line in enumerate(self._last_output):
                log.write(line, scroll_end=index == last_index)
            self.query_one("#results").scroll_home(animate=False)
            # Follow on the log's own refresh, not the parent screen's:
            # its first visible resize can render previously deferred lines.
            log.refresh(layout=True)
            log.call_after_refresh(
                lambda: log.scroll_end(animate=False, immediate=True, x_axis=False)
                if self.debug_visible else None
            )
        self.query_one("#debug", Button).label = (
            "Hide technical details" if self.debug_visible else "Show technical details"
        )

    def action_run_selected(self) -> None:
        self.selected_pressed()

    def action_quit_safely(self) -> None:
        if not self.busy:
            self.exit()
            return
        self._exit_after_step = True
        if self.current_step in (0, 1):
            self._cancel_readonly()
            self._set_message("Stopping read-only check; toolkit will exit when it stops.")
        else:
            self._stop_after_step = True
            self._set_message("Exit queued after the current checkout. Review Google Play on Android; no later step will start.")
            self.query_one("#stop", Button).disabled = True
            self.query_one("#exit", Button).disabled = True

    def _start(self, step: int, sequence: bool) -> None:
        if self.busy or self._diagnosing:
            return
        if step >= 2 and not self._offer_check_ready:
            self._set_status("Check Device & Offers first")
            self._set_message("Connect Android and complete the offer check before starting a paid step.")
            return
        if step >= 2 and (
                not self._offer_ready_device
                or not self._connection_state
                or self._connection_state.state != "connected"
                or self._offer_ready_device not in self._connection_state.devices):
            self._offer_check_ready = False
            self._set_status("Android device changed")
            self._set_message("Reconnect the device checked in Step 1 and run Check Device & Offers again. No checkout started.")
            return
        self._invalidate_connection_polls()
        self._history_account = self._selected_account or self._offer_ready_account
        self._save_history(self._history_account, step, "started")
        self._checked_device = None
        if step <= 1:
            self._environment_checked = False
            self._set_workflow(0, "pending")
            self._offer_check_ready = False
            self._initial_offer_ready = False
            self._offer_ready_device = None
            self._offer_ready_account = None
        else:
            self._initial_offer_ready = False
        self.query_one("#quickstart", Button).label = "Check Device & Offers"
        self.busy = True
        self.query_one("#reload", Button).disabled = True
        self.query_one("#refresh_offers", Button).disabled = True
        self.sequence = sequence
        self.current_step = step
        self._stop_after_step = False
        self._cancel_requested = False
        self._exit_after_step = False
        self._last_outcome = None
        self._verified_account = None
        self._last_output.clear()
        self._run_output.clear()
        self.query_one("#debug_log", RichLog).clear()
        self.query_one("#offer_view", RichLog).clear()
        self.query_one("#offer_view", RichLog).display = False
        self.query_one("#offers_title", Static).display = False
        self.query_one("#device", Static).tooltip = None
        self.query_one("#device", Static).update("Device  •  Auto-detect")
        self.query_one("#account", Static).update("Account  •  Not checked")
        self.query_one("#manual", Button).display = False
        self.query_one("#manual", Button).disabled = True
        self._started_at = time.monotonic()
        self._last_activity_at = self._started_at
        self.query_one("#elapsed", Static).update("Elapsed: 00:00")
        self.query_one("#status_panel").scroll_home(animate=False)
        self.query_one("#activity", Static).update(
            "Connecting to Android diagnostics..." if self.native_mode
            else "Connecting to MuMu and preparing tools..."
        )
        quick_check = self.query_one("#quickstart", Button)
        quick_check.disabled = True
        quick_check.add_class("checking")
        run_button = self.query_one("#run", Button)
        run_button.add_class("checking")
        run_button.label = f"Running Step {step}… 0s"
        if step == 1:
            quick_check.label = "Checking offers… 0s"
        else:
            quick_check.label = f"Running Step {step}… 0s"
        self.query_one("#diagnose", Button).disabled = True
        self.query_one("#quickstart", Button).display = True
        self.query_one("#choose_account", Button).disabled = True
        self.query_one("#run", Button).disabled = True
        self.query_one("#sequence", Button).disabled = True
        self.query_one("#mode", Select).disabled = True
        self.query_one("#step", Select).disabled = True
        self.query_one("#next", Button).disabled = True
        self.query_one("#stop", Button).disabled = False
        self.query_one("#stop", Button).label = "Stop check" if step <= 1 else "Stop after this step"
        self.query_one("#stop", Button).display = True
        self.query_one("#next", Button).display = False
        self.query_one("#step_result", Static).update(f"Step {step} · {STAGES[step].label}")
        self._set_workflow(step, "running")
        self._set_status(
            "Running — check Android if prompted" if self.native_mode
            else "Running — check MuMu if prompted"
        )
        self._set_message(
            "Checking Android over ADB. Setup may alter Frida or device settings." if self.linux_mode
            else "Preparing tools and checking Google Play. Missing dependencies are installed automatically."
        )
        self.run_stage(step)

    def _execute(self, command: list[str]) -> int:
        """Run the existing engine and send output to the TUI without blocking it."""
        # Windows Python defaults to a locale code page when stdout is piped.
        # Force UTF-8 for nested setup/CLI Python processes without changing billing logic.
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        # Internal marker from PowerShell is only enabled for this TUI consumer.
        child_env["EPLUS_TUI"] = "1"
        if self._cancel_requested:
            return -1
        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
        with subprocess.Popen(
            command, cwd=self.repo, env=child_env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **options
        ) as child:
            self._child = child
            if self._cancel_requested and child.poll() is None:
                try:
                    if os.name == "nt":
                        child.terminate()
                    else:
                        os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if child.stdout:
                for line in child.stdout:
                    try:
                        self.call_from_thread(self._received, line.rstrip("\r\n"))
                    except RuntimeError:
                        return -1
            return child.wait()

    @work(thread=True, exclusive=True)
    def run_stage(self, step: int) -> None:
        try:
            if not self.native_mode and needs_setup():
                self.call_from_thread(self._set_status, "Setting up...")
                self.call_from_thread(self._set_message,
                                      "Preparing Frida and ADB. You do not need a separate setup command.")
                setup = self.repo / "setup_env.py"
                if not setup.is_file():
                    raise FileNotFoundError("setup_env.py is missing from the project directory")
                setup_code = self._execute([sys.executable, str(setup)])
                if self._cancel_requested:
                    self.call_from_thread(self._finished, step, Outcome("cancelled", "Check stopped", "Cancelled read-only check."), None)
                    return
                if setup_code != 0:
                    self.call_from_thread(
                        self._finished, step,
                        Outcome("setup_failed", "Setup did not finish",
                                "Open Debug, check the setup issue, then retry. No checkout was started."),
                        None)
                    return

            # Never treat a report from setup or a previous invocation as the
            # outcome of the selected step.
            if self._cancel_requested:
                self.call_from_thread(self._finished, step, Outcome("cancelled", "Check stopped", "Cancelled read-only check."), None)
                return
            started = time.time()
            command = command_for_step(
                step, self.repo, account=self._selected_account or self._offer_ready_account,
                device=self._offer_ready_device if step >= 2 else None)
            if self.linux_mode:
                # The Linux bridge publishes a minimal allow-listed report for
                # this invocation. Never consume an older shared report.
                with TemporaryDirectory(prefix="eplus-dashboard-") as directory:
                    result_file = Path(directory) / "result.json"
                    return_code = self._execute([*command, "--result-file", str(result_file)])
                    report = None
                    if return_code == 0 and step > 0 and not self._cancel_requested:
                        try:
                            data = json.loads(result_file.read_text(encoding="utf-8"))
                            if isinstance(data, dict) and isinstance(data.get("status"), str):
                                report = data
                        except (OSError, ValueError):
                            pass
                connection = poll_connection()
                self.call_from_thread(self._remember_linux_transport, connection)
            else:
                return_code = self._execute(command)
                report = read_fresh_report(self.repo / "ultra0-report", step, started,
                                           expected_account=self._verified_account)
            if self._cancel_requested:
                self.call_from_thread(self._finished, step, Outcome("cancelled", "Check stopped", "Cancelled read-only check."), None)
                return
            result = outcome_for_stage(step, return_code, report)
            if return_code != 0 or result.code in ("missing_report", "tool_error"):
                result = cli_issue_outcome(list(self._run_output), step) or result
            self.call_from_thread(self._finished, step, result, report)
        except Exception as error:
            # Keep a safe, concrete failure category in Technical details,
            # even when a subprocess could not start and produced no output.
            diagnostic = safe_launch_diagnostic(error)
            self.call_from_thread(self._record_internal_diagnostic, diagnostic)
            result = Outcome("exception", "Could not start the selected step",
                             "Check Python, ADB and the Android connection. Open technical "
                             "details if needed; verify Google Play before retrying payments.")
            self.call_from_thread(self._finished, step, result, None)
        finally:
            self._child = None

    def _remember_linux_transport(self, result: ConnectionStatus) -> None:
        # Do not display serials. The selected transport must be unique.
        self._connection_state = result
        self._checked_device = (
            result.devices[0] if result.state == "connected" and len(result.devices) == 1
            else None
        )

    def _record_internal_diagnostic(self, diagnostic: str) -> None:
        # Do not add to _run_output: protocol parsing only accepts child output.
        self._last_output.append(diagnostic)
        if self.debug_visible:
            self.query_one("#debug_log", RichLog).write(diagnostic, scroll_end=True)

    def _received(self, line: str) -> None:
        if not line:
            return
        if line.strip() == "EPLUS_STAGE_DONE:0":
            if self.busy:
                self._environment_checked = True
                self._set_workflow(0, "done")
            return
        # Keep the original marker for the parser, but never print raw
        # EPLUS_ISSUE codes in the visible RichLog, even when Debug is open.
        visible_line = readable_issue_line(line)
        self._last_output.append(visible_line)
        self._run_output.append(line)
        self._last_activity_at = time.monotonic()
        lower = line.lower()
        if "restarting google one before querying offers" in lower:
            phase = "Restarting Google One before querying offers..."
        elif "frida" in lower or "setup" in lower:
            phase = "Preparing Android tools..."
        elif "adb" in lower or "device" in lower:
            phase = "Checking Android device..."
        elif "offer" in lower or "billing" in lower or "google one" in lower:
            phase = "Checking available offers..."
        elif "account" in lower or "play store" in lower:
            phase = "Checking Google Play account..."
        else:
            phase = "Process responding. Details available in technical log."
        self.query_one("#activity", Static).update(phase)
        if self.debug_visible:
            self.query_one("#debug_log", RichLog).write(visible_line, scroll_end=True)
        if re.search(r"Action required:|Press Enter to continue", line, re.I):
            btn = self.query_one("#manual", Button)
            btn.display = True
            btn.disabled = False
            self._set_message("Complete the requested action on Android, then click Completed.")
        if self.linux_mode:
            if line == "Android device connected.":
                self.query_one("#device", Static).update("Device  •  ADB Connected")
            elif line == "Google Play account verified.":
                self.query_one("#account", Static).update("Account  •  Verified by offer check")
            elif line == "Google Play account could not be verified.":
                self.query_one("#account", Static).update("Account  •  Unverified")
            # The Linux stage adapter emits only categorized progress, not
            # raw device/account identities or untrusted Android output.
            return
        selected_device = re.search(
            r"\b(?:Connected ADB device:|Device)\s+([A-Za-z0-9._:/-]{1,90})\s*$", line
        )
        if selected_device:
            self._checked_device = selected_device.group(1)
            card = self.query_one("#device", Static)
            card.update("Device  •  " + escape(self._checked_device))
            card.tooltip = self._checked_device
        android = re.fullmatch(
            r"\s*Android\s+([A-Za-z0-9._-]{1,16}) \(SDK ([0-9]{1,4})\), ABI ([A-Za-z0-9_-]{1,24})\s*",
            line,
        )
        if android and self._checked_device:
            release, sdk, abi = android.groups()
            details = f"Android {release} · SDK {sdk} · {abi}"
            card = self.query_one("#device", Static)
            card.update(f"{details} · {escape(self._checked_device)}")
            card.tooltip = f"{details}\nADB: {self._checked_device}"
        # Trust only the structured PowerShell "Account <email>" status line,
        # not arbitrary log messages containing an email address.
        account = re.match(
            r"^\s*Account\s+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s*$",
            line,
        )
        if account:
            self._verified_account = account.group(1).lower()
            self.query_one("#account", Static).update(
                "Account  •  " + escape(mask_email(self._verified_account))
            )

    def _finished(self, step: int, outcome: Outcome, report: dict | None) -> None:
        self.busy = False
        self.query_one("#reload", Button).disabled = False
        self.query_one("#run", Button).remove_class("checking")
        self.query_one("#refresh_offers", Button).disabled = False
        history_account = self._history_account
        if step == 1:
            history_account = str((report or {}).get("detected_play_account") or "") or history_account
            if self._environment_checked:
                self._save_history(history_account, 0, "environment_ok")
        self._save_history(history_account, step, outcome.code)
        if self._environment_checked:
            self._set_workflow(0, "done")
        if step == 1:
            detected = str((report or {}).get("detected_play_account") or "").strip().lower()
            verified = (self._verified_account or "").strip().lower()
            offers = (report or {}).get("offers")
            if self.linux_mode:
                # Linux publishes no email or device serial in its result.
                # The bridge validates an explicitly selected account against
                # the Step 1 Play listing; it repeats live lookup before billing.
                selected = (self._selected_account or "").strip().lower()
                self._offer_check_ready = bool(
                    outcome.code == "listed" and isinstance(offers, list) and offers
                    and selected and self._checked_device and self._connection_state
                    and self._connection_state.state == "connected"
                    and len(self._connection_state.devices) == 1
                )
                self._verified_account = selected if self._offer_check_ready else None
            else:
                self._offer_check_ready = bool(
                    outcome.code == "listed" and isinstance(offers, list) and offers
                    and verified and (not detected or detected == verified)
                    and (not self._selected_account or self._selected_account == verified)
                    and self._checked_device
                )
            self._offer_ready_device = self._checked_device if self._offer_check_ready else None
            self._offer_ready_account = (
                (self._selected_account or "").strip().lower() if self.linux_mode else verified
            ) if self._offer_check_ready else None
            if self._offer_ready_device:
                # The Stage 1 transport is authoritative; the previous idle
                # poll may describe a different device from before the check.
                self._connection_state = ConnectionStatus("connected", (self._offer_ready_device,))
            self._initial_offer_ready = bool(self._offer_check_ready and any(
                isinstance(offer, dict) and offer.get("id") == "12months-eft"
                and str(offer.get("micros")) == "0"
                and (not self.linux_mode or offer.get("product") == "g1.2tb.ai.subs_variant")
                for offer in offers
            ))
        elif step == 0:
            self._offer_check_ready = False
            self._initial_offer_ready = False
            self._offer_ready_device = None
            self._offer_ready_account = None
        elif step >= 2:
            # Step 1 unlocks *all* paid choices (2-5), not just Step 2.
            # A failed or inconclusive paid attempt must not silently revoke
            # the user's ability to choose a different step or manually retry.
            # Actual paid operations still run fresh account/device/offer
            # preflight and require a separate checkout approval every time.
            self._initial_offer_ready = False
            account_changed = bool(
                self._verified_account and self._offer_ready_account
                and self._verified_account != self._offer_ready_account
            )
            device_changed = bool(
                self._checked_device and self._offer_ready_device
                and self._checked_device != self._offer_ready_device
            )
            identity_invalid = outcome.code in (
                "account_mismatch", "account_unavailable", "account_unverified",
                "wrong_account", "play_account_switch_failed",
                "device_missing", "device_mismatch", "device_ambiguous", "adb_missing",
            )
            if account_changed or device_changed or identity_invalid:
                # Only a known account/device problem revokes the read-only
                # selection gate. Idle ADB monitoring also revokes it if the
                # previously checked device disconnects.
                self._offer_check_ready = False
                self._offer_ready_device = None
                self._offer_ready_account = None
        self._last_outcome = outcome
        self.query_one("#manual", Button).display = False
        self.query_one("#manual", Button).disabled = True
        self.query_one("#quickstart", Button).remove_class("checking")
        self.query_one("#choose_account", Button).disabled = self._accounts_loading
        self.query_one("#quickstart", Button).disabled = False
        self.query_one("#quickstart", Button).label = (
            "Continue to Step 2" if self._initial_offer_ready else "Check Device & Offers"
        )
        self.query_one("#diagnose", Button).disabled = False
        self.query_one("#run", Button).disabled = False
        self.query_one("#sequence", Button).disabled = False
        self.query_one("#mode", Select).disabled = False
        self.query_one("#step", Select).disabled = False
        self._update_run_mode()
        self.query_one("#stop", Button).disabled = True
        self.query_one("#stop", Button).display = False
        self.query_one("#exit", Button).disabled = False
        self._started_at = None
        self.query_one("#activity", Static).update("Step finished. Review the result below.")
        self.query_one("#status_panel").scroll_home(animate=False)
        self._set_workflow(step, "done" if outcome.code in ("listed", "environment_ok") else "review")
        self._set_status(outcome.title)
        instruction = outcome.instruction
        if step == 1 and outcome.code == "listed" and not self._offer_check_ready:
            instruction += (" Verify the active Play account and run the check again "
                            "before starting a paid step. A selected Android account "
                            "is not proof of the Google Play billing account.")
        elif step == 1 and self._offer_check_ready and not self._initial_offer_ready:
            instruction += " The 12-month offer was not found with a zero-priced first phase; review available offers."
        if outcome.code in ("tool_error", "exception", "missing_report", "setup_failed"):
            # Never echo arbitrary child-process lines on the main status screen:
            # they can contain internal codes, local paths or private account data.
            instruction += " Open Show technical details for troubleshooting."
        self._set_message(instruction)
        if step == 1:
            self.query_one("#step_result", Static).update(report_summary(report))
            if isinstance(report, dict):
                offer_view = self.query_one("#offer_view", RichLog)
                offer_view.clear()
                for item in offer_explorer_lines(report):
                    offer_view.write(item)
                offer_view.display = True
                self.query_one("#offers_title", Static).display = True
            if self.macos_mode and isinstance(report, dict):
                detected = report.get("detected_play_account")
                if isinstance(detected, str) and detected:
                    self.query_one("#account", Static).update(
                        "Account  •  " + escape(mask_email(detected))
                    )
        if outcome.can_review and self.sequence and step < 5 and not self._stop_after_step:
            self.query_one("#next", Button).disabled = False
            self.query_one("#next", Button).display = True
            self.query_one("#quickstart", Button).display = False
        else:
            self.query_one("#next", Button).disabled = True
            self.query_one("#next", Button).display = False
            self.query_one("#quickstart", Button).display = True
            self.sequence = False
        if self._exit_after_step:
            self.exit()
            return
        if step == 1 and self._initial_offer_ready:
            self.push_screen(CheckComplete(), lambda proceed: self._confirm_stage(2, True) if proceed else None)
            return
        if step == 1 and outcome.code == "listed" and not self._initial_offer_ready:
            self.push_screen(SetupNotice("Review the offer check", instruction))
            return
        if step <= 1 and outcome.code not in ("listed", "environment_ok", "cancelled"):
            # Step 0/1 errors are always actionable for non-technical users,
            # including unknown setup failures and newly added engine codes.
            self.push_screen(SetupNotice(outcome.title, outcome.instruction))
            return
        if outcome.code == "checkout_requested":
            self.notify("Review the actual subscription in Google Play.", severity="warning")
        elif outcome.code not in ("listed", "environment_ok", "cancelled"):
            self.notify("Step stopped; check Google Play charges and subscription status "
                        "before a manual retry.", severity="warning")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ePlus AI Toolkit Textual frontend")
    parser.add_argument("--step", type=int)
    parser.add_argument("--sequence", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    linux = sys.platform == "linux"
    default_step = 2 if linux and args.sequence else 1
    try:
        selected = validate_step(default_step if args.step is None else args.step)
    except ValueError as exc:
        parser.error(str(exc))
    GoogleAITUI(repo=Path(__file__).resolve().parent, selected_step=selected,
                start_sequence=args.sequence, initial_debug=args.debug,
                show_advanced=linux and (args.step is not None or args.sequence)).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
