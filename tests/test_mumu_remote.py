"""Offline tests for mumu_remote.py. No MuMu instance or ADB device is started."""
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

import mumu_remote as mu


def fake_result(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "")

DUMMY_ADB = str(Path(__file__).resolve().parent.parent / "setup_env.py")


class MuMuRemoteTests(unittest.TestCase):
    def test_players_parses_and_rejects_bad_metadata(self):
        with patch.object(mu, "_run", return_value=fake_result('{"1": {"adb_port": 16416}}')):
            self.assertEqual(mu.players()["1"]["adb_port"], 16416)
        with patch.object(mu, "_run", return_value=fake_result("not json")):
            with self.assertRaises(mu.MuMuError):
                mu.players()
        with patch.object(mu, "_run", return_value=fake_result('{"errcode": 1}', returncode=1)):
            with self.assertRaises(mu.MuMuError):
                mu.players()

    def test_pick_instance_respects_request_and_refuses_guess(self):
        with patch.object(mu, "players", return_value={"3": {}, "1": {}}):
            self.assertEqual(mu.pick_instance(requested=3), 3)
            with self.assertRaises(mu.MuMuError):
                mu.pick_instance(requested=9)
            with self.assertRaises(mu.MuMuError):
                mu.pick_instance()  # two instances: refuse to guess
        with patch.object(mu, "players", return_value={"2": {}}):
            self.assertEqual(mu.pick_instance(), 2)
        with patch.object(mu, "players", return_value={}):
            with self.assertRaises(mu.MuMuError):
                mu.pick_instance()

    def test_start_android_launches_existing_instance_without_creating(self):
        with patch.object(mu, "mumu_running", return_value=True), \
             patch.object(mu, "players", return_value={"1": {"adb_port": "16416"}}), \
             patch.object(mu, "launch_player") as launch, \
             patch.object(mu, "create_player") as create, \
             patch.object(mu, "wait_instance_adb", return_value="127.0.0.1:16416"):
            serial = mu.start_android(adb_path=DUMMY_ADB, vmindex=1)
        self.assertEqual(serial, "127.0.0.1:16416")
        launch.assert_called_once_with(1, mu.DEFAULT_MUMU_DIR)
        create.assert_not_called()

    def test_start_android_requires_consent_to_create(self):
        with patch.object(mu, "mumu_running", return_value=True), \
             patch.object(mu, "players", return_value={}), \
             patch.object(mu, "create_player") as create:
            with self.assertRaises(mu.MuMuError):
                mu.start_android(adb_path=DUMMY_ADB)
            create.assert_not_called()

    def test_start_android_creates_only_with_explicit_consent(self):
        # pick_instance runs twice (before/after creation); the final
        # players() call looks up the new instance's ADB port.
        with patch.object(mu, "mumu_running", return_value=True), \
             patch.object(mu, "players", return_value={"1": {"adb_port": "16416"}}), \
             patch.object(mu, "create_player") as create, \
             patch.object(mu, "launch_player"), \
             patch.object(mu, "wait_instance_adb", return_value="127.0.0.1:16416") as wait, \
             patch.object(mu, "pick_instance", side_effect=[mu.MuMuError("none"), 1]):
            serial = mu.start_android(adb_path=DUMMY_ADB, create_missing=True)
        self.assertEqual(serial, "127.0.0.1:16416")
        create.assert_called_once()
        wait.assert_called_once()

    def test_start_android_binds_to_selected_instance_port(self):
        with patch.object(mu, "mumu_running", return_value=True), \
             patch.object(mu, "players", return_value={"1": {"adb_port": "16416"}}), \
             patch.object(mu, "launch_player"), \
             patch.object(mu, "wait_instance_adb", return_value=None):
            with self.assertRaises(mu.MuMuError):
                mu.start_android(adb_path=DUMMY_ADB, timeout_seconds=1)

    def test_wait_adb_returns_first_online_serial(self):
        def fake_run(args, **kwargs):
            if args[-1] == "devices":
                return fake_result("List of devices attached\nemulator-5554\tdevice\n")
            return fake_result("")
        with patch.object(mu, "_run", side_effect=fake_run) as run:
            serial = mu.wait_adb(DUMMY_ADB, timeout_seconds=1, ports=("5555",))
        self.assertEqual(serial, "emulator-5554")
        run.assert_any_call([DUMMY_ADB, "connect", "127.0.0.1:5555"], timeout=10)
        run.assert_any_call([DUMMY_ADB, "devices"], timeout=10)

    def test_wait_adb_returns_none_when_no_device(self):
        def fake_run(args, **kwargs):
            if args[-1] == "devices":
                return fake_result("List of devices attached\n")
            return fake_result("")
        with patch.object(mu, "_run", side_effect=fake_run):
            serial = mu.wait_adb(DUMMY_ADB, timeout_seconds=1, ports=("5555",))
        self.assertIsNone(serial)

    def test_wait_instance_adb_only_accepts_matching_serial(self):
        def fake_run(args, **kwargs):
            if args[-1] == "devices":
                return fake_result("List of devices attached\n"
                                   "emulator-5554\tdevice\n"
                                   "127.0.0.1:16416\tdevice\n")
            return fake_result("")
        with patch.object(mu, "_run", side_effect=fake_run):
            serial = mu.wait_instance_adb(DUMMY_ADB, "16416", timeout_seconds=1)
        self.assertEqual(serial, "127.0.0.1:16416")

    def test_wait_instance_adb_ignores_other_devices(self):
        def fake_run(args, **kwargs):
            if args[-1] == "devices":
                return fake_result("List of devices attached\nemulator-5554\tdevice\n")
            return fake_result("")
        with patch.object(mu, "_run", side_effect=fake_run):
            serial = mu.wait_instance_adb(DUMMY_ADB, "16416", timeout_seconds=1)
        self.assertIsNone(serial)

    def test_detect_google_accounts_reads_dumpsys(self):
        dump = ("User UserInfo{0:Owner}:\n"
                "  Accounts: 1\n"
                "  Account {name=user@example.com, type=com.google}\n")
        def fake_run(args, **kwargs):
            if "dumpsys" in args:
                return fake_result(dump)
            return fake_result("")
        with patch.object(mu, "_run", side_effect=fake_run):
            accounts = mu.detect_google_accounts(DUMMY_ADB, "127.0.0.1:16416",
                                                 timeout_seconds=1)
        self.assertEqual(accounts, ["user@example.com"])

    def test_detect_google_accounts_ignores_non_google_and_garbage(self):
        dump = ("Accounts: 2\n"
                "  Account {name=local, type=com.example.local}\n"
                "  Account {name=Person@Example.COM, type=com.google}\n")
        def fake_run(args, **kwargs):
            if "dumpsys" in args:
                return fake_result(dump)
            return fake_result("")
        with patch.object(mu, "_run", side_effect=fake_run):
            accounts = mu.detect_google_accounts(DUMMY_ADB, "127.0.0.1:16416",
                                                 timeout_seconds=1)
        self.assertEqual(accounts, ["person@example.com"])

    def test_detect_google_accounts_empty_when_none_signed_in(self):
        def fake_run(args, **kwargs):
            return fake_result("Accounts: 0\n")
        with patch.object(mu, "_run", side_effect=fake_run):
            accounts = mu.detect_google_accounts(DUMMY_ADB, "127.0.0.1:16416",
                                                 timeout_seconds=1)
        self.assertEqual(accounts, [])

    def test_launch_failure_raises_mumu_error(self):
        with patch.object(mu, "mumu_running", return_value=False), \
             patch.object(mu, "_run", return_value=fake_result("boom", returncode=1)):
            with self.assertRaises(mu.MuMuError):
                mu.launch_mumu()


if __name__ == "__main__":
    unittest.main()
