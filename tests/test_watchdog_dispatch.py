from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

MODULE_PATH = Path(__file__).parents[1] / "bin" / "watchdog_dispatch.py"
spec = importlib.util.spec_from_file_location("watchdog_dispatch", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load watchdog_dispatch")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)


def snapshot(mode="--check", message="NOVO: proxy fora", active=("proxy",), new=("proxy",), gone=()):
    return {
        "mode": mode,
        "observed_at": "2026-09-16T14:00:00-03:00",
        "message": message,
        "active_keys": list(active),
        "new_keys": list(new),
        "gone_keys": list(gone),
        "texts": {key: f"detail {key}" for key in active},
    }


class WatchdogDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        self.spool = self.root / "spool"
        self.received = self.root / "received.jsonl"
        self.launcher = self.root / "launcher.py"
        self.launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import os,sys\n"
            "from pathlib import Path\n"
            "data=sys.stdin.buffer.read()\n"
            "with Path(os.environ['RECEIVED']).open('ab') as f: f.write(data+b'\\n')\n"
            "raise SystemExit(int(os.environ.get('FAIL','0')))\n",
            encoding="utf-8",
        )
        self.launcher.chmod(0o700)
        self.env = {"RECEIVED": str(self.received), "FAIL": "0"}

    def run_dispatch(self, value, *, fail=False):
        original = wd.os.environ.copy()
        wd.os.environ.update(self.env | {"FAIL": "1" if fail else "0"})
        try:
            event = wd.event_from_snapshot(value, self.state)
            ok = wd.dispatch(event, launcher=self.launcher, spool=self.spool)
            if ok:
                wd._write_state(self.state, event["active_keys"], event["observed_at"])
            return ok, event
        finally:
            wd.os.environ.clear()
            wd.os.environ.update(original)

    def received_events(self):
        if not self.received.exists():
            return []
        return [json.loads(line) for line in self.received.read_text(encoding="utf-8").splitlines()]

    def test_all_notify_shapes_are_structured(self):
        cases = [
            ("actionable NEW", snapshot()),
            ("repeated unchanged", snapshot(message="unchanged", new=())),
            ("mixed NEW+GONE", snapshot(message="NOVO + RESOLVIDO", active=("new",), new=("new",), gone=("old",))),
            ("standalone RESOLVIDO", snapshot(message="RESOLVIDO: old", active=(), new=(), gone=("old",))),
            ("daily ATENCAO", snapshot(mode="--daily", message="ATENCAO: proxy", new=(), gone=())),
            ("daily Brain OK", snapshot(mode="--daily", message="Brain OK", active=(), new=(), gone=())),
        ]
        for name, value in cases:
            with self.subTest(name=name):
                ok, event = self.run_dispatch(value)
                self.assertTrue(ok)
                self.assertEqual("daily" if value["mode"] == "--daily" else "check", event["mode"])
                self.assertEqual(value["message"], event["message"])
        self.assertEqual(6, len(self.received_events()))
        self.assertFalse(any(self.spool.glob("*.json")))

    def test_failed_event_is_atomic_and_retry_uses_same_identity(self):
        value = snapshot(message="danger $(touch /tmp/nope); `false`")
        ok, event = self.run_dispatch(value, fail=True)
        self.assertFalse(ok)
        queued = list(self.spool.glob("*.json"))
        self.assertEqual(1, len(queued))
        identity = queued[0].stem
        self.assertEqual(wd._identity(wd._encoded(event)), identity)
        self.assertFalse(self.state.exists())

        ok, retried = self.run_dispatch(value)
        self.assertTrue(ok)
        self.assertEqual(identity, wd._identity(wd._encoded(retried)))
        self.assertFalse(any(self.spool.glob("*.json")))
        self.assertEqual(2, len(self.received_events()))
        self.assertFalse(Path("/tmp/nope").exists())

    def test_bounded_spool_refuses_new_event_without_discarding_old(self):
        first = wd.event_from_snapshot(snapshot(message="first"), self.state)
        second = wd.event_from_snapshot(snapshot(message="second"), self.state)
        original = wd.os.environ.copy()
        wd.os.environ.update(self.env | {"FAIL": "1"})
        try:
            self.assertFalse(wd.dispatch(first, launcher=self.launcher, spool=self.spool, max_files=1))
            self.assertFalse(wd.dispatch(second, launcher=self.launcher, spool=self.spool, max_files=1))
        finally:
            wd.os.environ.clear(); wd.os.environ.update(original)
        queued = list(self.spool.glob("*.json"))
        self.assertEqual(1, len(queued))
        self.assertEqual("first", json.loads(queued[0].read_text())["message"])

    def test_oversized_and_malformed_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            wd._encoded(wd.event_from_snapshot(snapshot(message="x" * 70000), self.state))
        result = subprocess.run(
            [str(MODULE_PATH), "--state", str(self.state), "--spool", str(self.spool),
             "--launcher", str(self.launcher), "--fixture-json"],
            input=b"not-json", env=self.env, capture_output=True,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(any(self.spool.glob("*.json")))

    def test_watchdog_routes_all_notifications_without_raw_delivery(self):
        script = (Path(__file__).parents[1] / "bin" / "watchdog.sh").read_text(encoding="utf-8")
        notify_body = script.split("notify() {", 1)[1].split("\n}\n\n#", 1)[0]
        self.assertIn("watchdog_dispatch.py", script)
        self.assertIn("printf '%s\\0'", notify_body)
        self.assertNotIn("127.0.0.1:8787", notify_body)
        self.assertNotIn('"$msg" 2>>', notify_body)
        self.assertIn('notify "${OUT:-$MSG}"', script)
        self.assertEqual(2, script.count('notify "'))
        self.assertIn("Watchdog automation failed", notify_body)

    def test_watchdog_sanitizes_v3_alarm_keys_for_intake_boundary(self):
        script = (Path(__file__).parents[1] / "bin" / "watchdog.sh").read_text(encoding="utf-8")
        self.assertIn("tr '[:upper:] ' '[:lower:]-'", script)
        self.assertIn("tr -cd 'a-z0-9._-'", script)
        self.assertIn('[ -n "$V3KEY" ] || V3KEY="alarm"', script)
        self.assertIn('add "v3-${V3KEY}"', script)

    def test_test_mode_synthetic_new_cannot_reach_production_board_launcher(self):
        fake_home = self.root / "home"
        production_board = fake_home / ".hermes" / "kanban.db"
        production_board.parent.mkdir(parents=True)
        production_board.write_bytes(b"production-board-sentinel")
        launcher_calls = self.root / "launcher-calls"
        dispatcher = self.root / "dispatcher.py"
        dispatcher.write_text(
            "from pathlib import Path\n"
            "import os, subprocess, sys\n"
            "launcher = sys.argv[sys.argv.index('--launcher') + 1]\n"
            "subprocess.run([launcher], input=sys.stdin.buffer.read(), check=True)\n",
            encoding="utf-8",
        )
        launcher = self.root / "production-intake"
        launcher.write_text(
            "#!/bin/sh\n"
            f"printf called >> {str(launcher_calls)!r}\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        python_path = fake_home / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
        python_path.parent.mkdir(parents=True)
        python_path.symlink_to(sys.executable)
        env = os.environ.copy()
        env.update({
            "HOME": str(fake_home),
            "HERMES_KANBAN_DB": str(production_board),
            "WATCHDOG_TEST_KANBAN_DB": str(production_board),
            "WATCHDOG_TEST_MODE": "1",
            "WATCHDOG_TEST_SYNTHETIC_NEW": "no-linkedin-session",
            "WATCHDOG_STATE": str(self.root / "state.json"),
            "WATCHDOG_LOG": str(self.root / "watchdog.log"),
            "WATCHDOG_TEST_NOTIFICATION_LOG": str(self.root / "notifications.log"),
            "WATCHDOG_SPOOL": str(self.root / "spool"),
            "WATCHDOG_DISPATCH": str(dispatcher),
            "WATCHDOG_INTAKE": str(launcher),
            "WATCHDOG_TEST_INTAKE": str(launcher),
        })
        result = subprocess.run(
            ["/bin/zsh", str(Path(__file__).parents[1] / "bin" / "watchdog.sh"), "--check"],
            env=env, capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(64, result.returncode, result.stderr)
        self.assertIn("isolated Kanban", result.stderr)
        self.assertFalse(launcher_calls.exists(), "test mode leaked synthetic NEW to Kanban intake")
        self.assertEqual(b"production-board-sentinel", production_board.read_bytes())

    def test_test_mode_can_explicitly_disable_kanban(self):
        script = Path(__file__).parents[1] / "bin" / "watchdog.sh"
        fake_home = self.root / "home"
        python_path = fake_home / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
        python_path.parent.mkdir(parents=True)
        python_path.symlink_to(sys.executable)
        launcher_calls = self.root / "launcher-calls"
        launcher = self.root / "intake"
        launcher.write_text(
            "#!/bin/sh\n"
            f"printf called >> {str(launcher_calls)!r}\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        env = os.environ.copy()
        env.update({
            "HOME": str(fake_home),
            "WATCHDOG_TEST_MODE": "1",
            "WATCHDOG_TEST_DISABLE_KANBAN": "1",
            "WATCHDOG_STATE": str(self.root / "state.json"),
            "WATCHDOG_LOG": str(self.root / "watchdog.log"),
            "WATCHDOG_TEST_NOTIFICATION_LOG": str(self.root / "notifications.log"),
            "WATCHDOG_SPOOL": str(self.root / "spool"),
            "WATCHDOG_DISPATCH": str(self.root / "missing-dispatcher"),
            "WATCHDOG_INTAKE": str(launcher),
        })
        result = subprocess.run(
            ["/bin/zsh", str(script), "--check"], env=env, capture_output=True,
            text=True, check=False, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(launcher_calls.exists())
        self.assertFalse((self.root / "spool").exists())
        self.assertTrue((self.root / "notifications.log").exists())


if __name__ == "__main__":
    unittest.main()
