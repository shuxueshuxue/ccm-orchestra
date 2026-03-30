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

    def test_candidate_projects_roots_prefers_active_cac_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fallback = home / ".claude" / "projects"
            active = home / ".cac" / "envs" / "main" / ".claude" / "projects"
            active.mkdir(parents=True)
            fallback.mkdir(parents=True)
            (home / ".cac" / "current").parent.mkdir(parents=True, exist_ok=True)
            (home / ".cac" / "current").write_text("main\n")

            roots = ccm.candidate_projects_roots(home=home)

            self.assertEqual(roots[0], active)
            self.assertIn(fallback, roots)

    @mock.patch("claude_coop_manager.candidate_projects_roots", autospec=True)
    def test_resolve_transcript_searches_cac_projects_root(self, candidate_projects_roots):
        with tempfile.TemporaryDirectory() as tmp:
            fallback = Path(tmp) / "fallback"
            active = Path(tmp) / "active"
            fallback.mkdir()
            active.mkdir()
            candidate_projects_roots.return_value = [active, fallback]

            transcript = active / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "custom-title",
                        "customTitle": "hello-smoke-20260330-3",
                        "sessionId": "right",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "user",
                        "cwd": "/work/app",
                        "sessionId": "right",
                    }
                )
                + "\n"
            )

            record = ccm.SessionRecord(
                name="hello-smoke-20260330-3",
                tmux_session="ccm-hello",
                display_name="hello-smoke-20260330-3",
                cwd="/work/app",
                started_at=0.0,
            )

            match = ccm.resolve_transcript(record)

            self.assertEqual(match, transcript)


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


class ParserHelpTests(unittest.TestCase):
    def test_root_help_mentions_daily_loop_and_open_exception(self):
        help_text = ccm.build_parser().format_help()

        self.assertIn("Daily loop: start -> send -> read.", help_text)
        self.assertIn("Use 'open' only", help_text)
        self.assertIn("For agents/LLMs, run 'ccm guide agent'", help_text)

    def test_open_help_marks_open_as_exception_tool(self):
        parser = ccm.build_parser()
        open_parser = parser._subparsers._group_actions[0].choices["open"]
        help_text = open_parser.format_help()

        self.assertIn("Open is an exception tool", help_text)
        self.assertIn("start ->", help_text)
        self.assertIn("send -> read", help_text)

    def test_relay_help_marks_relay_as_preferred_over_tell_for_agents(self):
        parser = ccm.build_parser()
        relay_parser = parser._subparsers._group_actions[0].choices["relay"]
        help_text = relay_parser.format_help()

        self.assertIn("Prefer 'relay' over 'tell'", help_text)
        self.assertIn("reply hint", help_text)
        self.assertIn("no receipt convention", help_text)

    def test_read_help_explains_poll_model_and_contrasts_with_relay(self):
        parser = ccm.build_parser()
        read_parser = parser._subparsers._group_actions[0].choices["read"]
        help_text = read_parser.format_help()

        self.assertIn("poll-based", help_text)
        self.assertIn("does not push a", help_text)
        self.assertIn("wakeup into another agent tab", help_text)
        self.assertIn("use 'relay'", help_text)

    def test_guide_help_mentions_agent_playbook(self):
        parser = ccm.build_parser()
        guide_parser = parser._subparsers._group_actions[0].choices["guide"]
        help_text = guide_parser.format_help()

        self.assertIn("long-form guidance", help_text)
        self.assertIn("agents and LLMs", help_text)


class GuideOutputTests(unittest.TestCase):
    def test_render_agent_guide_covers_long_lived_helper_and_relay(self):
        guide_text = ccm.render_guide("agent")

        self.assertIn("global `ccm` only", guide_text)
        self.assertIn("long-lived", guide_text)
        self.assertIn("dedicated", guide_text)
        self.assertIn("relay", guide_text)
        self.assertIn("push", guide_text)
        self.assertIn("poll", guide_text)
        self.assertIn("doctor", guide_text)

    def test_render_human_guide_points_to_agent_guide_when_needed(self):
        guide_text = ccm.render_guide("human")

        self.assertIn("start -> send -> read", guide_text)
        self.assertIn("ccm guide agent", guide_text)


class CommandBuildTests(unittest.TestCase):
    @mock.patch("claude_coop_manager.resolve_claude_executable", autospec=True)
    def test_build_claude_command_uses_interactive_mode(self, resolve_claude_executable):
        resolve_claude_executable.return_value = "/Users/test/.cac/bin/claude"
        command = ccm.build_claude_command("frontend-helper")

        self.assertEqual(
            command[:3],
            ["/Users/test/.cac/bin/claude", "--dangerously-skip-permissions", "-n"],
        )
        self.assertNotIn("--print", command)

    @mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": "/Users/test/.cac/envs/main/.claude"}, clear=False)
    @mock.patch("claude_coop_manager.resolve_claude_executable", autospec=True)
    def test_build_tmux_claude_command_pins_binary_and_config_root(self, resolve_claude_executable):
        resolve_claude_executable.return_value = "/Users/test/.cac/bin/claude"

        command = ccm.build_tmux_claude_command("frontend-helper")

        self.assertIn("env", command)
        self.assertIn("CLAUDE_CONFIG_DIR=/Users/test/.cac/envs/main/.claude", command)
        self.assertIn("/Users/test/.cac/bin/claude", command)

    def test_format_relay_message_includes_sender_identity_and_reply_hint(self):
        sender = {
            "title": "main",
            "worktree": "/work/app",
            "branch": "feat/demo",
            "repo_root": "/work",
            "helper": "frontend-helper",
            "helper_tmux_session": "ccm-frontend-helper-1234",
            "helper_transcript": "/tmp/demo.jsonl",
        }

        rendered = ccm.format_relay_message(
            "Please review the current frontend.",
            sender,
            task="wizard refinement",
            scene="untouched",
            ports="5183/8013",
        )

        self.assertIn("[from: main", rendered)
        self.assertIn("worktree: /work/app", rendered)
        self.assertIn("branch: feat/demo", rendered)
        self.assertIn("helper: frontend-helper", rendered)
        self.assertIn("tmux: ccm-frontend-helper-1234", rendered)
        self.assertIn('reply-via: ccm relay main "..."', rendered)
        self.assertTrue(rendered.endswith("Please review the current frontend."))


class LifecycleTests(unittest.TestCase):
    @mock.patch("claude_coop_manager.build_tmux_claude_command", autospec=True)
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
        build_tmux_claude_command,
    ):
        tmux_has_session.return_value = False
        build_tmux_claude_command.return_value = "env CLAUDE_CONFIG_DIR=/Users/test/.cac/envs/main/.claude /Users/test/.cac/bin/claude --dangerously-skip-permissions -n frontend-helper"
        state = ccm.State()

        record = ccm.start_session(state, "frontend-helper", "/work/app")

        self.assertEqual(record.tmux_session, ccm.build_tmux_session_name("frontend-helper", "/work/app"))
        command = run_command.call_args_list[0].args[0]
        self.assertEqual(command[:6], ["tmux", "new-session", "-d", "-s", record.tmux_session, "-c"])
        self.assertEqual(command[6], "/work/app")
        self.assertEqual(
            command[7],
            "env CLAUDE_CONFIG_DIR=/Users/test/.cac/envs/main/.claude /Users/test/.cac/bin/claude --dangerously-skip-permissions -n frontend-helper",
        )
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

    @mock.patch("claude_coop_manager.workspace_identity", autospec=True)
    @mock.patch("claude_coop_manager.run_command", autospec=True)
    def test_list_kitty_tabs_includes_workspace_identity(self, run_command, workspace_identity):
        run_command.return_value = mock.Mock(
            stdout=json.dumps(
                [
                    {
                        "tabs": [
                            {
                                "title": "feat/main-thread-for-member",
                                "windows": [
                                    {
                                        "id": 550,
                                        "is_active": True,
                                        "cwd": "/work/app",
                                        "cmdline": ["/bin/zsh"],
                                    }
                                ],
                            }
                        ]
                    }
                ]
            )
        )
        workspace_identity.return_value = {
            "worktree": "/work/app",
            "repo_root": "/work",
            "branch": "feat/demo",
            "helper": "frontend-helper",
            "helper_status": "running",
            "helper_tmux_session": "ccm-frontend-helper-1234",
            "helper_transcript": "/tmp/demo.jsonl",
        }

        tabs = ccm.list_kitty_tabs("unix:/tmp/mykitty")

        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["title"], "feat/main-thread-for-member")
        self.assertEqual(tabs[0]["branch"], "feat/demo")
        self.assertEqual(tabs[0]["helper"], "frontend-helper")
        self.assertEqual(tabs[0]["helper_status"], "running")

    @mock.patch("claude_coop_manager.send_message_to_kitty_tab", autospec=True)
    @mock.patch("claude_coop_manager.resolve_current_sender_context", autospec=True)
    def test_relay_message_to_kitty_tab_wraps_message_with_sender_context(
        self,
        resolve_current_sender_context,
        send_message_to_kitty_tab,
    ):
        resolve_current_sender_context.return_value = {
            "title": "main",
            "worktree": "/work/app",
            "repo_root": "/work",
            "branch": "feat/demo",
            "helper": "frontend-helper",
            "helper_status": "running",
            "helper_tmux_session": "ccm-frontend-helper-1234",
            "helper_transcript": "/tmp/demo.jsonl",
        }
        send_message_to_kitty_tab.return_value = {"title": "target", "window_id": "550", "endpoint": "unix:/tmp/mykitty"}

        payload = ccm.relay_message_to_kitty_tab(
            "target",
            "Please review this branch.",
            "unix:/tmp/mykitty",
            cwd="/work/app",
            task="frontend refinement",
            scene="untouched",
            ports="5183/8013",
        )

        forwarded = send_message_to_kitty_tab.call_args.args[1]
        self.assertIn("[from: main", forwarded)
        self.assertIn("reply-via: ccm relay main \"...\"", forwarded)
        self.assertIn("Please review this branch.", forwarded)
        self.assertEqual(payload["title"], "target")


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
    @mock.patch("claude_coop_manager.claude_version_from_binary", autospec=True)
    @mock.patch("claude_coop_manager.shutil.which", autospec=True)
    def test_doctor_report_includes_binary_and_state_info(self, which, claude_version_from_binary):
        which.side_effect = lambda name, path=None: f"/usr/bin/{name}"
        claude_version_from_binary.return_value = "Claude Code v2.1.86"
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


class CleanupTests(unittest.TestCase):
    @mock.patch("claude_coop_manager.run_command", autospec=True)
    @mock.patch("claude_coop_manager.tmux_has_session", autospec=True)
    def test_cleanup_removes_dead_sessions_only_by_default(self, tmux_has_session, run_command):
        tmux_has_session.side_effect = lambda session: session == "ccm-live"
        state = ccm.State(
            sessions={
                "live": ccm.SessionRecord("live", "ccm-live", "live", "/work/a", 0.0),
                "dead": ccm.SessionRecord("dead", "ccm-dead", "dead", "/work/a", 0.0),
            }
        )

        payload = ccm.cleanup_sessions(state, kill_live=False)

        self.assertEqual(payload["removed_dead"], ["dead"])
        self.assertEqual(payload["killed_live"], [])
        self.assertEqual(sorted(state.sessions), ["live"])
        run_command.assert_not_called()

    @mock.patch("claude_coop_manager.run_command", autospec=True)
    @mock.patch("claude_coop_manager.tmux_has_session", autospec=True)
    def test_cleanup_can_kill_live_sessions(self, tmux_has_session, run_command):
        tmux_has_session.return_value = True
        state = ccm.State(
            sessions={
                "live": ccm.SessionRecord("live", "ccm-live", "live", "/work/a", 0.0),
            }
        )

        payload = ccm.cleanup_sessions(state, kill_live=True)

        self.assertEqual(payload["removed_dead"], [])
        self.assertEqual(payload["killed_live"], ["live"])
        self.assertEqual(state.sessions, {})
        run_command.assert_called_once_with(["tmux", "kill-session", "-t", "ccm-live"], check=False)


class KittyMessagingTests(unittest.TestCase):
    def test_kitty_window_worktree_prefers_env_pwd(self):
        window = {
            "cwd": "/work/stale",
            "env": {"PWD": "/work/canonical"},
        }

        self.assertEqual(ccm.kitty_window_worktree(window), "/work/canonical")

    @mock.patch("claude_coop_manager.workspace_identity", autospec=True)
    @mock.patch("claude_coop_manager.require_binary", autospec=True)
    @mock.patch("claude_coop_manager.run_command", autospec=True)
    def test_list_kitty_tabs_uses_active_window_per_tab(self, run_command, require_binary, workspace_identity):
        run_command.return_value = mock.Mock(
            stdout=json.dumps(
                [
                    {
                        "tabs": [
                            {
                                "title": "Main",
                                "windows": [
                                    {
                                        "id": 11,
                                        "is_active": True,
                                        "cwd": "/work/stale-main",
                                        "env": {"PWD": "/work/main"},
                                        "cmdline": ["codex"],
                                    }
                                ],
                            },
                            {
                                "title": "Claude Helper",
                                "windows": [
                                    {"id": 20, "is_active": False, "cwd": "/old", "env": {"PWD": "/old"}, "cmdline": ["zsh"]},
                                    {
                                        "id": 21,
                                        "is_active": True,
                                        "cwd": "/work/stale-ui",
                                        "env": {"PWD": "/work/ui"},
                                        "cmdline": ["claude"],
                                    },
                                ],
                            },
                        ]
                    }
                ]
            )
        )
        workspace_identity.side_effect = [
            {
                "worktree": "/work/main",
                "repo_root": "/work",
                "branch": "main",
                "helper": "",
                "helper_status": "",
                "helper_tmux_session": "",
                "helper_transcript": "",
            },
            {
                "worktree": "/work/ui",
                "repo_root": "/work",
                "branch": "feat/ui",
                "helper": "frontend-helper",
                "helper_status": "running",
                "helper_tmux_session": "ccm-frontend-helper-1234",
                "helper_transcript": "/tmp/demo.jsonl",
            },
        ]

        peers = ccm.list_kitty_tabs("unix:/tmp/mykitty")

        self.assertEqual(
            peers,
            [
                {
                    "title": "Main",
                    "window_id": "11",
                    "cwd": "/work/main",
                    "cmdline": "codex",
                    "branch": "main",
                    "repo_root": "/work",
                    "helper": "",
                    "helper_status": "",
                    "helper_tmux_session": "",
                    "helper_transcript": "",
                },
                {
                    "title": "Claude Helper",
                    "window_id": "21",
                    "cwd": "/work/ui",
                    "cmdline": "claude",
                    "branch": "feat/ui",
                    "repo_root": "/work",
                    "helper": "frontend-helper",
                    "helper_status": "running",
                    "helper_tmux_session": "ccm-frontend-helper-1234",
                    "helper_transcript": "/tmp/demo.jsonl",
                },
            ],
        )

    @mock.patch("claude_coop_manager.workspace_identity", autospec=True)
    @mock.patch("claude_coop_manager.require_binary", autospec=True)
    @mock.patch("claude_coop_manager.run_command", autospec=True)
    def test_send_message_to_kitty_tab_injects_text_and_enter(self, run_command, require_binary, workspace_identity):
        run_command.side_effect = [
            mock.Mock(
                stdout=json.dumps(
                    [
                        {
                            "tabs": [
                                {
                                    "title": "scheduled-tasks",
                                    "windows": [
                                        {
                                            "id": 31,
                                            "is_active": True,
                                            "cwd": "/work/tasks",
                                            "cmdline": ["codex"],
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                )
            ),
            mock.Mock(stdout=""),
            mock.Mock(stdout=""),
        ]
        workspace_identity.return_value = {
            "worktree": "/work/tasks",
            "repo_root": "/work",
            "branch": "scheduled-tasks",
            "helper": "",
            "helper_status": "",
            "helper_tmux_session": "",
            "helper_transcript": "",
        }

        payload = ccm.send_message_to_kitty_tab(
            "scheduled-tasks",
            "Please review the frontend.",
            "unix:/tmp/mykitty",
        )

        self.assertEqual(payload["title"], "scheduled-tasks")
        self.assertEqual(payload["window_id"], "31")
        self.assertEqual(run_command.call_args_list[1].args[0][:5], ["kitty", "@", "--to", "unix:/tmp/mykitty", "send-text"])
        self.assertEqual(run_command.call_args_list[2].args[0][:5], ["kitty", "@", "--to", "unix:/tmp/mykitty", "send-key"])


if __name__ == "__main__":
    unittest.main()
