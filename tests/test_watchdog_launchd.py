import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "bin" / "watchdog_gateway_running.sh"


class WatchdogLaunchdDomainTests(unittest.TestCase):
    def run_check(self, scenario: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            calls = temp / "calls"
            launchctl = temp / "launchctl"
            launchctl.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    printf '%s\\n' "$2" >> "$WATCHDOG_LAUNCHCTL_CALLS"
                    case "$WATCHDOG_LAUNCHCTL_SCENARIO:$2" in
                      user:user/501/ai.hermes.gateway|gui:gui/501/ai.hermes.gateway)
                        printf 'state = running\\n'
                        exit 0
                        ;;
                      misleading:user/501/ai.hermes.gateway)
                        printf 'state = running\\n'
                        exit 113
                        ;;
                      stopped:user/501/ai.hermes.gateway|stopped:gui/501/ai.hermes.gateway)
                        printf 'state = waiting\\n'
                        exit 0
                        ;;
                      *)
                        exit 113
                        ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            env = os.environ.copy()
            env.update(
                WATCHDOG_LAUNCHCTL=str(launchctl),
                WATCHDOG_LAUNCHCTL_CALLS=str(calls),
                WATCHDOG_LAUNCHCTL_SCENARIO=scenario,
            )
            result = subprocess.run(
                ["/bin/zsh", str(CHECK), "501", "ai.hermes.gateway"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            return result, calls.read_text(encoding="utf-8").splitlines()

    def test_running_in_user_domain_passes(self):
        result, calls = self.run_check("user")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["user/501/ai.hermes.gateway"], calls)

    def test_running_in_gui_domain_passes(self):
        result, calls = self.run_check("gui")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["user/501/ai.hermes.gateway", "gui/501/ai.hermes.gateway"], calls
        )

    def test_absent_in_both_domains_alerts(self):
        result, calls = self.run_check("absent")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(
            ["user/501/ai.hermes.gateway", "gui/501/ai.hermes.gateway"], calls
        )

    def test_failed_launchctl_with_misleading_output_alerts(self):
        result, calls = self.run_check("misleading")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(
            ["user/501/ai.hermes.gateway", "gui/501/ai.hermes.gateway"], calls
        )

    def test_not_running_in_both_domains_alerts(self):
        result, calls = self.run_check("stopped")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(
            ["user/501/ai.hermes.gateway", "gui/501/ai.hermes.gateway"], calls
        )


if __name__ == "__main__":
    unittest.main()
