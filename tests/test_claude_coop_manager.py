import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_coop_manager as ccm


class SanitizeNameTests(unittest.TestCase):
    def test_sanitize_name_normalizes_spaces_and_symbols(self):
        self.assertEqual(ccm.sanitize_name("Frontend Helper #1"), "frontend-helper-1")

    def test_sanitize_name_rejects_empty_result(self):
        with self.assertRaises(ValueError):
            ccm.sanitize_name("!!!")


class TranscriptReadTests(unittest.TestCase):
    def test_incremental_reader_consumes_only_complete_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            first = json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}}) + "\n"
            partial = json.dumps({"type": "system", "subtype": "api_error", "error": {"status": 502}})
            path.write_text(first + partial)

            events, offset, buffer = ccm.read_incremental_jsonl(path, 0, "")

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "user")
            self.assertNotEqual(buffer, "")

            path.write_text(first + partial + "\n")
            events2, offset2, buffer2 = ccm.read_incremental_jsonl(path, offset, buffer)

            self.assertEqual(len(events2), 1)
            self.assertEqual(events2[0]["type"], "system")
            self.assertEqual(buffer2, "")
            self.assertGreater(offset2, offset)


class RenderEventTests(unittest.TestCase):
    def test_render_event_reads_assistant_text_blocks(self):
        event = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "Final answer"},
                ],
            },
        }

        rendered = ccm.render_event(event)

        self.assertIsNotNone(rendered)
        self.assertEqual(rendered["kind"], "assistant")
        self.assertEqual(rendered["text"], "Final answer")

    def test_render_event_formats_system_api_error(self):
        event = {
            "type": "system",
            "subtype": "api_error",
            "error": {"status": 502},
            "retryAttempt": 2,
            "maxRetries": 10,
        }

        rendered = ccm.render_event(event)

        self.assertEqual(rendered["kind"], "system")
        self.assertIn("502", rendered["text"])
        self.assertIn("2/10", rendered["text"])


class PaneStateTests(unittest.TestCase):
    def test_queued_message_banner_is_not_a_ready_prompt(self):
        pane = """
✢ Sautéing…

  ❯ Reply with exactly DEBUG_TURN_2
────────────────────────────────────────────────────────────────────────────────
❯ Press up to edit queued messages
────────────────────────────────────────────────────────────────────────────────
"""
        self.assertFalse(ccm.pane_has_prompt(pane))
        self.assertTrue(ccm.pane_has_queued_messages(pane))
        self.assertFalse(ccm.pane_is_ready_for_input(pane))

    def test_pane_with_real_world_spinner_glyph_is_not_ready(self):
        pane = """
❯ Reply with exactly DEBUG_TURN_1

✢ Sautéing…

────────────────────────────────────────────────────────────────────────────────
❯ 
"""
        self.assertTrue(ccm.pane_has_prompt(pane))
        self.assertTrue(ccm.pane_has_active_work(pane))
        self.assertFalse(ccm.pane_is_ready_for_input(pane))

    def test_pane_with_spinner_is_not_ready(self):
        pane = """
❯ Reply with exactly READY_DEBUG_ACK

✽ Hatching…

────────────────────────────────────────────────────────────────────────────────
❯ 
"""
        self.assertTrue(ccm.pane_has_prompt(pane))
        self.assertTrue(ccm.pane_has_active_work(pane))
        self.assertFalse(ccm.pane_is_ready_for_input(pane))

    def test_pane_with_other_spinner_word_is_not_ready(self):
        pane = """
❯ Reply with exactly STRESS_ACK_01

✽ Shenaniganing…

────────────────────────────────────────────────────────────────────────────────
❯ 
"""
        self.assertTrue(ccm.pane_has_active_work(pane))
        self.assertFalse(ccm.pane_is_ready_for_input(pane))

    def test_idle_pane_with_prompt_is_ready(self):
        pane = """
╭─── Claude Code v2.1.81 ──────────────────────────────────────────────────────╮
────────────────────────────────────────────────────────────────────────────────
❯ 
────────────────────────────────────────────────────────────────────────────────
"""
        self.assertTrue(ccm.pane_has_prompt(pane))
        self.assertFalse(ccm.pane_has_active_work(pane))
        self.assertTrue(ccm.pane_is_ready_for_input(pane))


class ReadyTimeoutTests(unittest.TestCase):
    def test_ready_retry_budget_uses_default_timeout(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertEqual(ccm.ready_retry_budget(2.0), 150)

    def test_ready_retry_budget_respects_env_override(self):
        with mock.patch.dict("os.environ", {"CCM_READY_TIMEOUT_SECONDS": "12"}, clear=False):
            self.assertEqual(ccm.ready_retry_budget(2.0), 6)


class TranscriptResolutionTests(unittest.TestCase):
    def test_find_transcript_prefers_matching_title_and_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong = root / "wrong.jsonl"
            right = root / "right.jsonl"
            wrong.write_text(
                json.dumps({"type": "custom-title", "customTitle": "other", "sessionId": "wrong"})
                + "\n"
                + json.dumps({"type": "user", "cwd": "/tmp/a", "sessionId": "wrong"})
                + "\n"
            )
            right.write_text(
                json.dumps({"type": "custom-title", "customTitle": "frontend-helper", "sessionId": "right"})
                + "\n"
                + json.dumps({"type": "user", "cwd": "/work/app", "sessionId": "right"})
                + "\n"
            )

            match = ccm.find_transcript_file(
                projects_root=root,
                display_name="frontend-helper",
                cwd="/work/app",
                started_after=0.0,
            )

            self.assertEqual(match, right)


class NamespaceTests(unittest.TestCase):
    def test_tmux_session_name_includes_cwd_fingerprint(self):
        first = ccm.build_tmux_session_name("frontend-helper", "/work/a")
        second = ccm.build_tmux_session_name("frontend-helper", "/work/b")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("ccm-frontend-helper-"))

    def test_default_state_path_isolated_by_cwd(self):
        first = ccm.default_state_path("/work/a")
        second = ccm.default_state_path("/work/b")

        self.assertNotEqual(first, second)
        self.assertEqual(first.name, "state.json")


class MainDispatchTests(unittest.TestCase):
    @mock.patch("claude_coop_manager.emit_list", autospec=True)
    @mock.patch("claude_coop_manager.load_state", autospec=True)
    def test_main_uses_global_cwd_for_state_namespace(self, load_state, emit_list):
        state = ccm.State()
        load_state.return_value = state

        exit_code = ccm.main(["--cwd", "/work/app", "list"])

        self.assertEqual(exit_code, 0)
        load_state.assert_called_once_with(ccm.default_state_path("/work/app"))
        emit_list.assert_called_once()

    @mock.patch("claude_coop_manager.emit", autospec=True)
    @mock.patch("claude_coop_manager.load_state", autospec=True)
    def test_main_accepts_global_json_flag_after_subcommand(self, load_state, emit):
        load_state.return_value = ccm.State()

        exit_code = ccm.main(["doctor", "--json"])

        self.assertEqual(exit_code, 0)
        emit.assert_called_once()

    @mock.patch("claude_coop_manager.emit", autospec=True)
    @mock.patch("claude_coop_manager.load_state", autospec=True)
    def test_main_uses_sys_argv_when_not_explicitly_provided(self, load_state, emit):
        load_state.return_value = ccm.State()

        with mock.patch("sys.argv", ["ccm", "doctor", "--json"]):
            exit_code = ccm.main()

        self.assertEqual(exit_code, 0)
        emit.assert_called_once()


class CommandBuildTests(unittest.TestCase):
    def test_build_claude_command_uses_interactive_mode(self):
        command = ccm.build_claude_command("frontend-helper")

        self.assertEqual(command[:3], ["claude", "--dangerously-skip-permissions", "-n"])
        self.assertNotIn("--print", command)


class LifecycleTests(unittest.TestCase):
    @mock.patch("claude_coop_manager.time.sleep", autospec=True)
    @mock.patch("claude_coop_manager.ensure_session_ready", autospec=True)
    @mock.patch("claude_coop_manager.tmux_has_session", autospec=True)
    @mock.patch("claude_coop_manager.require_binary", autospec=True)
    @mock.patch("claude_coop_manager.run_command", autospec=True)
    def test_start_session_launches_detached_tmux(
        self,
        run_command,
        require_binary,
        tmux_has_session,
        ensure_ready,
        _sleep,
    ):
        tmux_has_session.return_value = False
        state = ccm.State()

        record = ccm.start_session(state, "frontend-helper", "/work/app")

        self.assertEqual(record.tmux_session, ccm.build_tmux_session_name("frontend-helper", "/work/app"))
        command = run_command.call_args_list[0].args[0]
        self.assertEqual(command[:6], ["tmux", "new-session", "-d", "-s", record.tmux_session, "-c"])
        self.assertEqual(command[6], "/work/app")
        self.assertIn("claude --dangerously-skip-permissions -n frontend-helper", command[7])
        self.assertIn("frontend-helper", state.sessions)

    @mock.patch("claude_coop_manager.time.sleep", autospec=True)
    @mock.patch("claude_coop_manager.resolve_transcript", autospec=True)
    @mock.patch("claude_coop_manager.tmux_send_enter", autospec=True)
    @mock.patch("claude_coop_manager.tmux_paste", autospec=True)
    @mock.patch("claude_coop_manager.ensure_session_ready", autospec=True)
    @mock.patch("claude_coop_manager.tmux_has_session", autospec=True)
    def test_send_prompt_pastes_and_presses_enter(
        self,
        tmux_has_session,
        ensure_ready,
        tmux_paste,
        tmux_send_enter,
        resolve_transcript,
        _sleep,
    ):
        tmux_has_session.return_value = True
        resolve_transcript.return_value = Path("/tmp/transcript.jsonl")
        record = ccm.SessionRecord(
            name="frontend-helper",
            tmux_session="ccm-frontend-helper",
            display_name="frontend-helper",
            cwd="/work/app",
            started_at=0.0,
        )
        state = ccm.State(sessions={"frontend-helper": record})

        updated = ccm.send_prompt(state, "frontend-helper", "build the page")

        tmux_paste.assert_called_once_with("ccm-frontend-helper", "build the page")
        tmux_send_enter.assert_called_once_with("ccm-frontend-helper")
        self.assertEqual(updated.transcript_path, "/tmp/transcript.jsonl")

    @mock.patch("claude_coop_manager.require_binary", autospec=True)
    @mock.patch("claude_coop_manager.tmux_has_session", autospec=True)
    @mock.patch("claude_coop_manager.run_command", autospec=True)
    def test_open_in_kitty_launches_marked_tab(self, run_command, tmux_has_session, require_binary):
        tmux_has_session.return_value = True
        run_command.return_value = mock.Mock(stdout="")
        record = ccm.SessionRecord(
            name="frontend-helper",
            tmux_session="ccm-frontend-helper",
            display_name="frontend-helper",
            cwd="/work/app",
            started_at=0.0,
        )
        state = ccm.State(sessions={"frontend-helper": record})

        payload = ccm.open_in_kitty(state, "frontend-helper", "unix:/tmp/mykitty")

        command = run_command.call_args.args[0]
        self.assertEqual(command[:5], ["kitty", "@", "--to", "unix:/tmp/mykitty", "launch"])
        self.assertIn("[ccm:frontend-helper]", command)
        self.assertEqual(payload["title"], "[ccm:frontend-helper]")


class ReadWaitTests(unittest.TestCase):
    @mock.patch("claude_coop_manager.time.sleep", autospec=True)
    @mock.patch("claude_coop_manager.read_incremental_jsonl", autospec=True)
    @mock.patch("claude_coop_manager.resolve_transcript", autospec=True)
    def test_read_updates_waits_for_late_events(self, resolve_transcript, read_incremental_jsonl, _sleep):
        transcript = Path("/tmp/transcript.jsonl")
        resolve_transcript.return_value = transcript
        read_incremental_jsonl.side_effect = [
            ([], 0, ""),
            ([{"type": "assistant", "message": {"content": [{"type": "text", "text": "ready"}]}}], 10, ""),
        ]
        state = ccm.State(
            sessions={
                "frontend-helper": ccm.SessionRecord(
                    name="frontend-helper",
                    tmux_session="ccm-frontend-helper",
                    display_name="frontend-helper",
                    cwd="/work/app",
                    started_at=0.0,
                )
            }
        )

        events = ccm.read_updates(state, "frontend-helper", wait_seconds=3, poll_interval=1)

        self.assertEqual(events, [{"kind": "assistant", "text": "ready"}])
        self.assertEqual(read_incremental_jsonl.call_count, 2)


class DoctorTests(unittest.TestCase):
    @mock.patch("claude_coop_manager.shutil.which", autospec=True)
    def test_doctor_report_includes_binary_and_state_info(self, which):
        which.side_effect = lambda name: f"/usr/bin/{name}"
        state = ccm.State(
            sessions={
                "frontend-helper": ccm.SessionRecord(
                    name="frontend-helper",
                    tmux_session="ccm-frontend-helper",
                    display_name="frontend-helper",
                    cwd="/work/app",
                    started_at=0.0,
                )
            }
        )

        report = ccm.doctor_report(state, "/work/app", Path("/tmp/state.json"))

        self.assertEqual(report["cwd"], "/work/app")
        self.assertEqual(report["state_path"], "/tmp/state.json")
        self.assertTrue(report["binaries"]["tmux"])
        self.assertTrue(report["binaries"]["claude"])
        self.assertEqual(report["sessions"], ["frontend-helper"])


if __name__ == "__main__":
    unittest.main()
