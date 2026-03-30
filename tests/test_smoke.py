import json
import subprocess
import unittest
from unittest import mock

from ccm_orchestra import smoke as smoke


def completed(*, stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["cmd"], returncode=returncode, stdout=stdout, stderr=stderr)


class SmokeCheckTests(unittest.TestCase):
    def test_parse_args_uses_ccm_smoke_prog_name(self):
        with mock.patch("sys.argv", ["ccm-smoke"]):
            args = smoke.parse_args()

        self.assertEqual(args.helper_name, smoke.DEFAULT_HELPER_NAME)

    @mock.patch("ccm_orchestra.smoke.run_cli", autospec=True)
    def test_run_smoke_happy_path_reads_probe_token_and_returns_summary(self, run_cli):
        token = "CCM_SMOKE_ACK_TEST"
        run_cli.side_effect = [
            completed(stdout=json.dumps({"ok": True})),
            completed(stdout="not-running\n", returncode=1),
            completed(stdout=json.dumps({"name": "smoke-helper", "status": "running"})),
            completed(stdout=json.dumps([{"name": "smoke-helper", "status": "running"}])),
            completed(stdout=json.dumps({"name": "smoke-helper", "transcript": "/tmp/demo.jsonl"})),
            completed(stdout=json.dumps([{"kind": "assistant", "text": token}])),
            completed(stdout=json.dumps([{"name": "smoke-helper", "status": "killed"}])),
            completed(stdout=json.dumps({"removed_dead": [], "killed_live": []})),
        ]

        payload = smoke.run_smoke(
            cwd="/work/demo",
            helper_name="smoke-helper",
            read_wait_seconds=12.0,
            probe_token=token,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["probe_token"], token)
        self.assertEqual(payload["heartbeat"]["running"], False)
        self.assertEqual(payload["events"][0]["text"], token)
        self.assertEqual(run_cli.call_args_list[0].args[0], ["ccm", "--json", "--cwd", "/work/demo", "doctor"])
        self.assertEqual(run_cli.call_args_list[1].args[0], ["codex-heartbeat", "status"])
        self.assertEqual(run_cli.call_args_list[2].args[0], ["ccm", "--json", "--cwd", "/work/demo", "start", "smoke-helper"])
        self.assertEqual(run_cli.call_args_list[5].args[0][:6], ["ccm", "--json", "--cwd", "/work/demo", "read", "smoke-helper"])
        self.assertIn("--wait-seconds", run_cli.call_args_list[5].args[0])
        self.assertEqual(run_cli.call_args_list[-2].args[0], ["ccm", "--json", "--cwd", "/work/demo", "kill", "smoke-helper"])
        self.assertEqual(run_cli.call_args_list[-1].args[0], ["ccm", "--json", "--cwd", "/work/demo", "cleanup"])

    @mock.patch("ccm_orchestra.smoke.run_cli", autospec=True)
    def test_run_smoke_cleans_up_when_read_output_is_missing_probe_token(self, run_cli):
        run_cli.side_effect = [
            completed(stdout=json.dumps({"ok": True})),
            completed(stdout=json.dumps({"running": True})),
            completed(stdout=json.dumps({"name": "smoke-helper", "status": "running"})),
            completed(stdout=json.dumps([{"name": "smoke-helper", "status": "running"}])),
            completed(stdout=json.dumps({"name": "smoke-helper", "transcript": "/tmp/demo.jsonl"})),
            completed(stdout=json.dumps([{"kind": "assistant", "text": "wrong"}])),
            completed(stdout=json.dumps([{"name": "smoke-helper", "status": "killed"}])),
            completed(stdout=json.dumps({"removed_dead": [], "killed_live": []})),
        ]

        with self.assertRaisesRegex(RuntimeError, "probe token"):
            smoke.run_smoke(
                cwd="/work/demo",
                helper_name="smoke-helper",
                read_wait_seconds=12.0,
                probe_token="EXPECTED_TOKEN",
            )

        self.assertEqual(run_cli.call_args_list[-2].args[0], ["ccm", "--json", "--cwd", "/work/demo", "kill", "smoke-helper"])
        self.assertEqual(run_cli.call_args_list[-1].args[0], ["ccm", "--json", "--cwd", "/work/demo", "cleanup"])

    @mock.patch("ccm_orchestra.smoke.run_cli", autospec=True)
    def test_heartbeat_status_treats_not_running_as_valid_observation(self, run_cli):
        run_cli.return_value = completed(stdout="not-running\n", returncode=1)

        payload = smoke.heartbeat_status()

        self.assertEqual(payload, {"running": False, "raw": "not-running"})
        self.assertEqual(run_cli.call_args.args[0], ["codex-heartbeat", "status"])
        self.assertFalse(run_cli.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
