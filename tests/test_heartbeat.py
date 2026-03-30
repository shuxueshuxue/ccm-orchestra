import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ccm_orchestra import heartbeat as heartbeat


class HeartbeatPathTests(unittest.TestCase):
    def test_state_paths_are_scoped_by_tab_title_slug(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(heartbeat, "STATE_DIR", Path(tmp)):
            pid_path = heartbeat.heartbeat_pid_path("Feature Main")
            log_path = heartbeat.heartbeat_log_path("Feature Main")

        self.assertEqual(pid_path, Path(tmp) / "feature-main.pid")
        self.assertEqual(log_path, Path(tmp) / "feature-main.log")


class HeartbeatDeliveryTests(unittest.TestCase):
    @mock.patch("ccm_orchestra.heartbeat.subprocess.run", autospec=True)
    @mock.patch("ccm_orchestra.heartbeat.resolve_tab_window_id", autospec=True, return_value=777)
    def test_send_heartbeat_targets_custom_tab_title(self, resolve_tab_window_id, run):
        window_id = heartbeat.send_heartbeat("unix:/tmp/mykitty", "hello", "Feature Main")

        self.assertEqual(window_id, 777)
        resolve_tab_window_id.assert_called_once_with("unix:/tmp/mykitty", "Feature Main")
        self.assertEqual(run.call_args_list[0].args[0][-1], "hello")
        self.assertEqual(run.call_args_list[1].args[0][-1], "enter")

    @mock.patch("ccm_orchestra.heartbeat.send_heartbeat", autospec=True, return_value=888)
    def test_test_once_reports_sent_window(self, send_heartbeat):
        with mock.patch("sys.stdout.write") as write:
            exit_code = heartbeat.test_once("unix:/tmp/mykitty", "ping", "Feature Main")

        self.assertEqual(exit_code, 0)
        send_heartbeat.assert_called_once_with("unix:/tmp/mykitty", "ping", "Feature Main")
        self.assertIn("sent tab_title=Feature Main window_id=888", "".join(call.args[0] for call in write.call_args_list))


class HeartbeatLifecycleTests(unittest.TestCase):
    @mock.patch("ccm_orchestra.heartbeat.subprocess.Popen", autospec=True)
    @mock.patch("ccm_orchestra.heartbeat.resolve_tab_window_id", autospec=True, return_value=777)
    def test_start_background_passes_tab_title_and_uses_scoped_log(self, resolve_tab_window_id, popen):
        process = mock.Mock()
        process.pid = 42424
        popen.return_value = process

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(heartbeat, "STATE_DIR", Path(tmp)), \
             mock.patch("ccm_orchestra.heartbeat.time.sleep"), \
             mock.patch("ccm_orchestra.heartbeat.pid_is_alive", side_effect=[False, True]):
            exit_code = heartbeat.start_background("unix:/tmp/mykitty", 30, "hello", "Feature Main")

            self.assertEqual(exit_code, 0)
            self.assertIn("--tab-title", popen.call_args.args[0])
            self.assertIn("Feature Main", popen.call_args.args[0])
            self.assertTrue((Path(tmp) / "feature-main.log").exists())

        resolve_tab_window_id.assert_called_once_with("unix:/tmp/mykitty", "Feature Main")

    def test_status_background_uses_scoped_paths(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(heartbeat, "STATE_DIR", Path(tmp)):
            pid_path = heartbeat.heartbeat_pid_path("Feature Main")
            pid_path.write_text("43210\n")

            with mock.patch("ccm_orchestra.heartbeat.pid_is_alive", return_value=True), \
                 mock.patch("sys.stdout.write") as write:
                exit_code = heartbeat.status_background(tab_title="Feature Main")

        self.assertEqual(exit_code, 0)
        self.assertIn("feature-main.log", "".join(call.args[0] for call in write.call_args_list))
