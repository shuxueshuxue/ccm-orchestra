import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ccm_orchestra import cli as ccm


class SanitizeNameTests(unittest.TestCase):
    def test_sanitize_name_normalizes_spaces_and_symbols(self):
        self.assertEqual(ccm.sanitize_name("Frontend Agent #1"), "frontend-agent-1")

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

    def test_pane_with_effort_suffix_is_not_ready(self):
        pane = """
❯ Reply with exactly ENTER_PROBE_03

✢ Scampering… (thinking with medium effort)

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


class ReadyWaitTests(unittest.TestCase):
    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_capture", autospec=True)
    def test_ensure_tmux_session_ready_requires_stable_ready_observation(self, tmux_capture, _sleep):
        transient_ready = """
────────────────────────────────────────────────────────────────────────────────
❯
"""
        busy_again = """
✢ Scampering… (thinking with medium effort)

────────────────────────────────────────────────────────────────────────────────
❯
"""
        stable_ready = """
⏺ ENTER_PROBE_06

────────────────────────────────────────────────────────────────────────────────
❯
"""
        tmux_capture.side_effect = [transient_ready, busy_again, stable_ready, stable_ready]

        pane = ccm.ensure_tmux_session_ready("ccm-frontend-agent", retries=4, delay_seconds=0.1)

        self.assertEqual(pane, stable_ready)
        self.assertEqual(tmux_capture.call_count, 4)


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
                json.dumps({"type": "custom-title", "customTitle": "frontend-agent", "sessionId": "right"})
                + "\n"
                + json.dumps({"type": "user", "cwd": "/work/app", "sessionId": "right"})
                + "\n"
            )

            match = ccm.find_transcript_file(
                projects_root=root,
                display_name="frontend-agent",
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

    @mock.patch("ccm_orchestra.cli.candidate_projects_roots", autospec=True)
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

    @mock.patch("ccm_orchestra.cli.candidate_projects_roots", autospec=True)
    def test_describe_transcript_search_lists_roots_and_identity(self, candidate_projects_roots):
        candidate_projects_roots.return_value = [Path("/tmp/cac/projects"), Path("/tmp/fallback/projects")]
        record = ccm.SessionRecord(
            name="frontend-agent",
            tmux_session="ccm-frontend-agent",
            display_name="frontend-agent",
            cwd="/work/app",
            started_at=123.0,
        )

        payload = ccm.describe_transcript_search(record)

        self.assertEqual(payload["display_name"], "frontend-agent")
        self.assertEqual(payload["cwd"], "/work/app")
        self.assertEqual(payload["projects_roots"], ["/tmp/cac/projects", "/tmp/fallback/projects"])


class NamespaceTests(unittest.TestCase):
    def test_tmux_session_name_includes_cwd_fingerprint(self):
        first = ccm.build_tmux_session_name("frontend-agent", "/work/a")
        second = ccm.build_tmux_session_name("frontend-agent", "/work/b")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("ccm-frontend-agent-"))

    def test_default_state_path_isolated_by_cwd(self):
        first = ccm.default_state_path("/work/a")
        second = ccm.default_state_path("/work/b")

        self.assertNotEqual(first, second)
        self.assertEqual(first.name, "state.json")

    def test_discover_state_paths_finds_namespace_state_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "aa11" / "state.json").parent.mkdir(parents=True)
            (home / "aa11" / "state.json").write_text("{}")
            (home / "bb22" / "state.json").parent.mkdir(parents=True)
            (home / "bb22" / "state.json").write_text("{}")
            (home / "wechat-transport.json").write_text("{}")

            paths = ccm.discover_state_paths(home)

        self.assertEqual(paths, [home / "aa11" / "state.json", home / "bb22" / "state.json"])

    def test_list_sessions_all_scopes_flattens_namespace_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            first_path = home / "aa11" / "state.json"
            second_path = home / "bb22" / "state.json"
            first_path.parent.mkdir(parents=True)
            second_path.parent.mkdir(parents=True)
            first_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sessions": {
                            "frontend-agent": {
                                "name": "frontend-agent",
                                "tmux_session": "ccm-frontend-agent-aa11",
                                "display_name": "frontend-agent",
                                "cwd": "/work/a",
                                "started_at": 1.0,
                            }
                        },
                    }
                )
            )
            second_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sessions": {
                            "docs-editor": {
                                "name": "docs-editor",
                                "tmux_session": "ccm-docs-editor-bb22",
                                "display_name": "docs-editor",
                                "cwd": "/work/b",
                                "started_at": 2.0,
                            }
                        },
                    }
                )
            )

            with mock.patch("ccm_orchestra.cli.session_status", side_effect=["running", "dead"]):
                payload = ccm.list_sessions_all_scopes(home)

        self.assertEqual(
            payload,
            [
                {
                    "name": "frontend-agent",
                    "tmux_session": "ccm-frontend-agent-aa11",
                    "cwd": "/work/a",
                    "status": "running",
                    "transcript": "-",
                    "state_path": str(first_path),
                },
                {
                    "name": "docs-editor",
                    "tmux_session": "ccm-docs-editor-bb22",
                    "cwd": "/work/b",
                    "status": "dead",
                    "transcript": "-",
                    "state_path": str(second_path),
                },
            ],
        )


class MainDispatchTests(unittest.TestCase):
    @mock.patch("ccm_orchestra.cli.emit_list", autospec=True)
    @mock.patch("ccm_orchestra.cli.load_state", autospec=True)
    def test_main_uses_global_cwd_for_state_namespace(self, load_state, emit_list):
        state = ccm.State()
        load_state.return_value = state

        exit_code = ccm.main(["--cwd", "/work/app", "list"])

        self.assertEqual(exit_code, 0)
        load_state.assert_called_once_with(ccm.default_state_path("/work/app"))
        emit_list.assert_called_once()

    def test_normalize_global_args_keeps_state_path_with_other_global_flags(self):
        normalized = ccm.normalize_global_args(["doctor", "--json", "--state-path", "/tmp/override.json"])

        self.assertEqual(normalized, ["--json", "--state-path", "/tmp/override.json", "doctor"])

    @mock.patch("ccm_orchestra.cli.emit_list", autospec=True)
    @mock.patch("ccm_orchestra.cli.load_state", autospec=True)
    def test_main_uses_explicit_state_path_when_provided(self, load_state, emit_list):
        load_state.return_value = ccm.State()

        exit_code = ccm.main(["--state-path", "/tmp/override.json", "list"])

        self.assertEqual(exit_code, 0)
        load_state.assert_called_once_with(Path("/tmp/override.json"))
        emit_list.assert_called_once()

    @mock.patch("ccm_orchestra.cli.emit", autospec=True)
    @mock.patch("ccm_orchestra.cli.load_state", autospec=True)
    def test_main_accepts_global_json_flag_after_subcommand(self, load_state, emit):
        load_state.return_value = ccm.State()

        exit_code = ccm.main(["doctor", "--json"])

        self.assertEqual(exit_code, 0)
        emit.assert_called_once()

    @mock.patch("ccm_orchestra.cli.emit", autospec=True)
    @mock.patch("ccm_orchestra.cli.load_state", autospec=True)
    def test_main_uses_sys_argv_when_not_explicitly_provided(self, load_state, emit):
        load_state.return_value = ccm.State()

        with mock.patch("sys.argv", ["ccm", "doctor", "--json"]):
            exit_code = ccm.main()

        self.assertEqual(exit_code, 0)
        emit.assert_called_once()

    @mock.patch("ccm_orchestra.cli.emit", autospec=True)
    @mock.patch("ccm_orchestra.cli.list_sessions_all_scopes", autospec=True)
    @mock.patch("ccm_orchestra.cli.load_state", autospec=True)
    def test_main_can_list_all_scopes(self, load_state, list_sessions_all_scopes, emit):
        load_state.return_value = ccm.State()
        list_sessions_all_scopes.return_value = [{"name": "frontend-agent"}]

        exit_code = ccm.main(["list", "--all-scopes", "--json"])

        self.assertEqual(exit_code, 0)
        list_sessions_all_scopes.assert_called_once_with(ccm.DEFAULT_HOME_ROOT)
        emit.assert_called_once_with([{"name": "frontend-agent"}], as_json=True)


class ParserHelpTests(unittest.TestCase):
    def test_root_parser_uses_ccm_prog_name(self):
        parser = ccm.build_parser()

        self.assertEqual(parser.prog, "ccm")

    def test_root_help_mentions_daily_loop_and_open_exception(self):
        help_text = ccm.build_parser().format_help()

        self.assertIn("Daily loop: start -> send -> read.", help_text)
        self.assertIn("Use 'open' only", help_text)
        self.assertIn("For agents/LLMs, run 'ccm guide agent'", help_text)
        self.assertIn("Mental model:", help_text)
        self.assertIn("'codex-heartbeat' keeps", help_text)
        self.assertIn("one visible tab awake", help_text)
        self.assertIn("'wechat-watch' is the phone watcher", help_text)
        self.assertIn("lowers rare submit misses", help_text)

    def test_open_help_marks_open_as_exception_tool(self):
        parser = ccm.build_parser()
        open_parser = parser._subparsers._group_actions[0].choices["open"]
        help_text = open_parser.format_help()

        self.assertIn("Open is an exception tool", help_text)
        self.assertIn("start ->", help_text)
        self.assertIn("send -> read", help_text)

    def test_start_help_mentions_specific_names_and_namespace_collisions(self):
        parser = ccm.build_parser()
        start_parser = parser._subparsers._group_actions[0].choices["start"]
        help_text = start_parser.format_help()

        self.assertIn("specific agent name", help_text)
        self.assertIn("current namespace", help_text)
        self.assertIn("frontend-agent", help_text)

    def test_relay_help_marks_relay_as_preferred_over_tell_for_agents(self):
        parser = ccm.build_parser()
        relay_parser = parser._subparsers._group_actions[0].choices["relay"]
        help_text = relay_parser.format_help()

        self.assertIn("Prefer 'relay' over 'tell'", help_text)
        self.assertIn("primary path", help_text)
        self.assertIn("reply hint", help_text)
        self.assertIn("no receipt convention", help_text)
        self.assertIn("extra Enter retry", help_text)

    def test_tell_help_marks_tell_as_legacy_raw_path(self):
        parser = ccm.build_parser()
        tell_parser = parser._subparsers._group_actions[0].choices["tell"]
        help_text = tell_parser.format_help()

        self.assertIn("legacy", help_text)
        self.assertIn("raw fire-and-forget", help_text)
        self.assertIn("Prefer 'relay'", help_text)

    def test_wechat_shift_help_mentions_phone_notice(self):
        parser = ccm.build_parser()
        shift_parser = parser._subparsers._group_actions[0].choices["wechat-shift"]
        help_text = shift_parser.format_help()

        self.assertIn("WeChat thread", help_text)
        self.assertIn("handoff notice", help_text)

    def test_read_help_explains_poll_model_and_contrasts_with_relay(self):
        parser = ccm.build_parser()
        read_parser = parser._subparsers._group_actions[0].choices["read"]
        help_text = read_parser.format_help()

        self.assertIn("poll-based", help_text)
        self.assertIn("does not push a", help_text)
        self.assertIn("wakeup into another agent tab", help_text)
        self.assertIn("use 'relay'", help_text)
        self.assertIn("--raw", help_text)

    def test_list_help_mentions_all_scopes(self):
        parser = ccm.build_parser()
        list_parser = parser._subparsers._group_actions[0].choices["list"]
        help_text = list_parser.format_help()

        self.assertIn("--all-scopes", help_text)

    def test_root_help_mentions_state_path_override(self):
        help_text = ccm.build_parser().format_help()

        self.assertIn("--state-path", help_text)

    def test_guide_help_mentions_agent_playbook(self):
        parser = ccm.build_parser()
        guide_parser = parser._subparsers._group_actions[0].choices["guide"]
        help_text = guide_parser.format_help()

        self.assertIn("long-form guidance", help_text)
        self.assertIn("agents and LLMs", help_text)


class GuideOutputTests(unittest.TestCase):
    def test_render_agent_guide_covers_long_lived_agent_and_relay(self):
        guide_text = ccm.render_guide("agent")

        self.assertIn("global `ccm` only", guide_text)
        self.assertIn("long-lived", guide_text)
        self.assertIn("dedicated", guide_text)
        self.assertIn("relay", guide_text)
        self.assertIn("primary", guide_text)
        self.assertIn("legacy", guide_text)
        self.assertIn("raw tab text", guide_text)
        self.assertIn("push", guide_text)
        self.assertIn("poll", guide_text)
        self.assertIn("doctor", guide_text)
        self.assertIn("specific", guide_text)
        self.assertIn("current namespace", guide_text)
        self.assertIn("codex-heartbeat", guide_text)
        self.assertIn("reply-via", guide_text)
        self.assertIn("extra Enter retry", guide_text)

    def test_render_human_guide_points_to_agent_guide_when_needed(self):
        guide_text = ccm.render_guide("human")

        self.assertIn("start -> send -> read", guide_text)
        self.assertIn("ccm guide agent", guide_text)
        self.assertIn("ccm relay", guide_text)
        self.assertIn("codex-heartbeat", guide_text)
        self.assertIn("extra Enter retry", guide_text)


class WeChatPeerTests(unittest.TestCase):
    def test_root_help_mentions_wechat_commands(self):
        help_text = ccm.build_parser().format_help()

        self.assertIn("wechat-connect", help_text)
        self.assertIn("wechat-status", help_text)
        self.assertIn("wechat-bind", help_text)
        self.assertIn("wechat-reply", help_text)
        self.assertIn("wechat-targets", help_text)
        self.assertIn("wechat-guide", help_text)
        self.assertIn("wechat-send", help_text)
        self.assertIn("wechat-shift", help_text)
        self.assertNotIn("wechat-register", help_text)

    def test_parse_target_spec_supports_kitty_and_tmux(self):
        kitty = ccm.parse_target_spec("kitty:scheduled-tasks")
        tmux = ccm.parse_target_spec("tmux:ccm-frontend-agent-abcd1234")

        self.assertEqual(kitty.kind, "kitty")
        self.assertEqual(kitty.value, "scheduled-tasks")
        self.assertEqual(tmux.kind, "tmux")
        self.assertEqual(tmux.value, "ccm-frontend-agent-abcd1234")

    def test_parse_target_spec_rejects_unknown_scheme(self):
        with self.assertRaises(ccm.CCMError):
            ccm.parse_target_spec("alias:mycel")

    @mock.patch("ccm_orchestra.cli.list_kitty_tabs", autospec=True)
    def test_resolve_target_spec_can_find_visible_kitty_tab(self, list_kitty_tabs):
        list_kitty_tabs.return_value = [
            {
                "title": "scheduled-tasks",
                "window_id": "498",
                "cwd": "/work/scheduled",
                "cmdline": "codex",
                "branch": "feat/scheduled",
                "repo_root": "/work",
                "agent": "",
                "agent_status": "",
                "agent_tmux_session": "",
                "agent_transcript": "",
            }
        ]

        target = ccm.resolve_target_spec("kitty:scheduled-tasks", listen_on="unix:/tmp/mykitty", cwd="/work/main")

        self.assertEqual(target.target, "kitty:scheduled-tasks")
        self.assertEqual(target.kind, "kitty")
        self.assertEqual(target.title, "scheduled-tasks")
        self.assertEqual(target.window_id, "498")

    @mock.patch("ccm_orchestra.cli.tmux_has_session", autospec=True, return_value=True)
    def test_resolve_target_spec_allows_headless_tmux_peer(self, tmux_has_session):
        target = ccm.resolve_target_spec("tmux:ccm-frontend-agent-abcd1234", listen_on=None, cwd="/work/ccm")

        self.assertEqual(target.target, "tmux:ccm-frontend-agent-abcd1234")
        self.assertEqual(target.kind, "tmux")
        self.assertEqual(target.tmux_session, "ccm-frontend-agent-abcd1234")
        self.assertEqual(target.runtime, "claude")
        tmux_has_session.assert_called_once_with("ccm-frontend-agent-abcd1234")

    def test_format_wechat_prompt_includes_target_reply_and_shift_instructions(self):
        sender = ccm.WeChatTargetRecord(
            target="kitty:mycel",
            kind="kitty",
            title="mycel",
            window_id="536",
            worktree="/work/app",
            repo_root="/work",
            branch="feat/demo",
            tmux_session="",
            agent="frontend-agent",
            agent_status="running",
            agent_transcript="/tmp/demo.jsonl",
            runtime="codex",
        )

        rendered = ccm.format_wechat_prompt(
            "Please take over the frontend pass.",
            sender,
            mode="shift",
            task="frontend pass",
            scene="untouched",
        )

        self.assertIn("<system-reminder>", rendered)
        self.assertIn("<ccm-wechat-message>", rendered)
        self.assertIn("Operator authorization", rendered)
        self.assertIn("To reply, use ccm wechat-send kitty:mycel", rendered)
        self.assertIn("To hand off, use ccm wechat-shift", rendered)
        self.assertIn("<from-target>kitty:mycel</from-target>", rendered)
        self.assertIn("<mode>shift</mode>", rendered)

    def test_format_wechat_prompt_can_compact_for_claude_targets(self):
        sender = ccm.WeChatTargetRecord(
            target="kitty:mycel",
            kind="kitty",
            title="mycel",
            window_id="536",
            worktree="/work/app",
            repo_root="/work",
            branch="feat/demo",
            tmux_session="",
            agent="frontend-agent",
            agent_status="running",
            agent_transcript="/tmp/demo.jsonl",
            runtime="codex",
        )

        rendered = ccm.format_wechat_prompt(
            "Please take over the frontend pass.",
            sender,
            mode="shift",
            task="frontend pass",
            scene="untouched",
            compact=True,
        )

        self.assertIn("<system-reminder>", rendered)
        self.assertNotIn("\n", rendered)

    @mock.patch("ccm_orchestra.cli.send_message_to_kitty_window", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_sender_target", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_target_spec", autospec=True)
    def test_wechat_send_uses_direct_target_and_window_target(
        self,
        resolve_target_spec,
        resolve_sender_target,
        send_message_to_kitty_window,
    ):
        resolve_sender_target.return_value = ccm.WeChatTargetRecord(
            target="kitty:mycel",
            kind="kitty",
            title="mycel",
            window_id="536",
            worktree="/work/main",
            repo_root="/work",
            branch="main",
            tmux_session="",
            agent="",
            agent_status="",
            agent_transcript="",
            runtime="codex",
        )
        resolve_target_spec.return_value = ccm.WeChatTargetRecord(
            target="kitty:scheduled-tasks",
            kind="kitty",
            title="scheduled-tasks",
            window_id="498",
            worktree="/work/scheduled",
            repo_root="/work",
            branch="feat/scheduled",
            tmux_session="",
            agent="",
            agent_status="",
            agent_transcript="",
            runtime="codex",
        )
        send_message_to_kitty_window.return_value = {"title": "scheduled-tasks", "window_id": "498", "endpoint": "unix:/tmp/mykitty"}

        payload = ccm.wechat_send_to_peer(
            target="kitty:scheduled-tasks",
            message="Please take over.",
            listen_on="unix:/tmp/mykitty",
            cwd="/work/main",
            mode="send",
        )

        self.assertEqual(payload["to_target"], "kitty:scheduled-tasks")
        self.assertEqual(payload["from_target"], "kitty:mycel")
        sent_message = send_message_to_kitty_window.call_args.args[1]
        self.assertIn("ccm wechat-send kitty:mycel", sent_message)

    @mock.patch("ccm_orchestra.cli.send_message_to_kitty_window", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_send_enter", autospec=True)
    @mock.patch("ccm_orchestra.cli.ensure_tmux_session_ready", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_sender_target", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_target_spec", autospec=True)
    def test_wechat_shift_compacts_multiline_envelope_for_claude_targets(
        self,
        resolve_target_spec,
        resolve_sender_target,
        ensure_tmux_session_ready,
        tmux_send_enter,
        send_message_to_kitty_window,
    ):
        def sender_side_effect(*, cwd, listen_on, explicit_target=""):
            target = explicit_target or "kitty:mycel"
            if target == "kitty:scheduled-tasks":
                return ccm.WeChatTargetRecord(
                    target="kitty:scheduled-tasks",
                    kind="kitty",
                    title="scheduled-tasks",
                    window_id="498",
                    worktree="/work/scheduled",
                    repo_root="/work/scheduled",
                    branch="feat/scheduled-task-improvements",
                    tmux_session="",
                    agent="",
                    agent_status="",
                    agent_transcript="",
                    runtime="codex",
                )
            return ccm.WeChatTargetRecord(
                target="kitty:mycel",
                kind="kitty",
                title="mycel",
                window_id="536",
                worktree="/work/main",
                repo_root="/work/main",
                branch="main",
                tmux_session="",
                agent="",
                agent_status="",
                agent_transcript="",
                runtime="codex",
            )

        resolve_sender_target.side_effect = sender_side_effect
        resolve_target_spec.return_value = ccm.WeChatTargetRecord(
            target="tmux:ccm-frontend-agent-abcd1234",
            kind="tmux",
            title="[ccm:frontend-agent]",
            window_id="562",
            worktree="/work/ccm",
            repo_root="/work/ccm",
            branch="main",
            tmux_session="ccm-frontend-agent-abcd1234",
            agent="frontend-agent",
            agent_status="running",
            agent_transcript="/tmp/agent.jsonl",
            runtime="claude",
        )
        send_message_to_kitty_window.return_value = {"title": "[ccm:frontend-agent]", "window_id": "562", "endpoint": "unix:/tmp/mykitty"}

        ccm.wechat_send_to_peer(
            target="tmux:ccm-frontend-agent-abcd1234",
            message="Take over the phone thread.",
            listen_on="unix:/tmp/mykitty",
            cwd="/work/main",
            mode="shift",
        )

        sent_message = send_message_to_kitty_window.call_args.args[1]
        self.assertNotIn("\n", sent_message)
        self.assertIn("<system-reminder>", sent_message)
        ensure_tmux_session_ready.assert_called_once_with("ccm-frontend-agent-abcd1234")
        tmux_send_enter.assert_called_once_with("ccm-frontend-agent-abcd1234")

    @mock.patch("ccm_orchestra.cli.send_message_to_kitty_window", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_sender_target", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_target_spec", autospec=True)
    @mock.patch("ccm_orchestra.cli.wechat_reply", autospec=True)
    def test_wechat_shift_rebinds_phone_owner_when_sender_currently_owns_thread(
        self,
        wechat_reply,
        resolve_target_spec,
        resolve_sender_target,
        send_message_to_kitty_window,
    ):
        resolve_sender_target.return_value = ccm.WeChatTargetRecord(
            target="kitty:mycel",
            kind="kitty",
            title="mycel",
            window_id="536",
            worktree="/work/main",
            repo_root="/work/main",
            branch="main",
            tmux_session="",
            agent="",
            agent_status="",
            agent_transcript="",
            runtime="codex",
        )
        transport = ccm.WeChatTransportState(
            token="bot-token",
            user_id="alice@im.wechat",
            context_tokens={"alice@im.wechat": "ctx-1"},
            bound_target="kitty:mycel",
        )
        resolve_target_spec.return_value = ccm.WeChatTargetRecord(
            target="tmux:ccm-frontend-agent-abcd1234",
            kind="tmux",
            title="claude-handoff",
            window_id="",
            worktree="/work/ccm",
            repo_root="/work/ccm",
            branch="main",
            tmux_session="ccm-frontend-agent-abcd1234",
            agent="frontend-agent",
            agent_status="running",
            agent_transcript="/tmp/agent.jsonl",
            runtime="claude",
        )
        send_message_to_kitty_window.return_value = {"window_id": "536", "endpoint": "unix:/tmp/mykitty"}
        wechat_reply.return_value = {"ok": True, "user_id": "alice@im.wechat"}

        with mock.patch("ccm_orchestra.cli.ensure_tmux_session_ready", autospec=True), \
             mock.patch("ccm_orchestra.cli.tmux_paste", autospec=True), \
             mock.patch("ccm_orchestra.cli.tmux_send_enter", autospec=True):
            payload = ccm.wechat_send_to_peer(
                target="tmux:ccm-frontend-agent-abcd1234",
                message="Take over the phone thread.",
                listen_on="unix:/tmp/mykitty",
                cwd="/work/main",
                mode="shift",
                transport=transport,
            )

        self.assertEqual(transport.bound_target, "tmux:ccm-frontend-agent-abcd1234")
        self.assertEqual(payload["phone_handoff"], "true")
        self.assertEqual(payload["phone_bound_target"], "tmux:ccm-frontend-agent-abcd1234")
        self.assertEqual(payload["phone_notice_user_id"], "alice@im.wechat")
        wechat_reply.assert_called_once()
        self.assertEqual(wechat_reply.call_args.kwargs["user_id"], "alice@im.wechat")
        self.assertIn("tmux:ccm-frontend-agent-abcd1234", wechat_reply.call_args.kwargs["text"])

    @mock.patch("ccm_orchestra.cli.send_message_to_kitty_window", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_sender_target", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_target_spec", autospec=True)
    @mock.patch("ccm_orchestra.cli.wechat_reply", autospec=True)
    def test_wechat_shift_defaults_sender_to_phone_owner_when_no_from_target_is_provided(
        self,
        wechat_reply,
        resolve_target_spec,
        resolve_sender_target,
        send_message_to_kitty_window,
    ):
        def sender_side_effect(*, cwd, listen_on, explicit_target=""):
            if explicit_target == "kitty:scheduled-tasks":
                return ccm.WeChatTargetRecord(
                    target="kitty:scheduled-tasks",
                    kind="kitty",
                    title="scheduled-tasks",
                    window_id="498",
                    worktree="/work/scheduled",
                    repo_root="/work/scheduled",
                    branch="feat/scheduled-task-improvements",
                    tmux_session="",
                    agent="",
                    agent_status="",
                    agent_transcript="",
                    runtime="codex",
                )
            return ccm.WeChatTargetRecord(
                target="kitty:mycel",
                kind="kitty",
                title="mycel",
                window_id="536",
                worktree="/work/main",
                repo_root="/work/main",
                branch="main",
                tmux_session="",
                agent="",
                agent_status="",
                agent_transcript="",
                runtime="codex",
            )

        resolve_sender_target.side_effect = sender_side_effect
        transport = ccm.WeChatTransportState(
            token="bot-token",
            user_id="alice@im.wechat",
            context_tokens={"alice@im.wechat": "ctx-1"},
            bound_target="kitty:scheduled-tasks",
        )

        def target_side_effect(spec, listen_on=None, cwd=None):
            mapping = {
                "kitty:scheduled-tasks": ccm.WeChatTargetRecord(
                    target="kitty:scheduled-tasks",
                    kind="kitty",
                    title="scheduled-tasks",
                    window_id="498",
                    worktree="/work/scheduled",
                    repo_root="/work/scheduled",
                    branch="feat/scheduled-task-improvements",
                    tmux_session="",
                    agent="",
                    agent_status="",
                    agent_transcript="",
                    runtime="codex",
                ),
                "kitty:mycel": ccm.WeChatTargetRecord(
                    target="kitty:mycel",
                    kind="kitty",
                    title="mycel",
                    window_id="536",
                    worktree="/work/main",
                    repo_root="/work/main",
                    branch="main",
                    tmux_session="",
                    agent="",
                    agent_status="",
                    agent_transcript="",
                    runtime="codex",
                ),
            }
            return mapping[spec]

        resolve_target_spec.side_effect = target_side_effect
        send_message_to_kitty_window.return_value = {"window_id": "536", "endpoint": "unix:/tmp/mykitty"}
        wechat_reply.return_value = {"ok": True, "user_id": "alice@im.wechat"}

        payload = ccm.wechat_send_to_peer(
            target="kitty:mycel",
            message="Return control to mycel.",
            listen_on="unix:/tmp/mykitty",
            cwd="/work/main",
            mode="shift",
            transport=transport,
        )

        self.assertEqual(payload["from_target"], "kitty:scheduled-tasks")
        self.assertEqual(transport.bound_target, "kitty:mycel")
        self.assertEqual(payload["phone_handoff"], "true")
        self.assertEqual(resolve_sender_target.call_args.kwargs["explicit_target"], "kitty:scheduled-tasks")
        wechat_reply.assert_called_once()

    @mock.patch("ccm_orchestra.cli.send_message_to_kitty_window", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_send_enter", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_paste", autospec=True)
    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.ensure_tmux_session_ready", autospec=True)
    def test_deliver_message_to_peer_can_use_headless_tmux_without_kitty(
        self,
        ensure_tmux_session_ready,
        time_sleep,
        tmux_paste,
        tmux_send_enter,
        send_message_to_kitty_window,
    ):
        target = ccm.WeChatTargetRecord(
            target="tmux:ccm-frontend-agent-abcd1234",
            kind="tmux",
            title="claude-handoff",
            window_id="",
            worktree="/work/ccm",
            repo_root="/work/ccm",
            branch="main",
            tmux_session="ccm-frontend-agent-abcd1234",
            agent="frontend-agent",
            agent_status="running",
            agent_transcript="/tmp/agent.jsonl",
            runtime="claude",
        )

        payload = ccm.deliver_message_to_target(target, "HEADLESS_DELIVERY_TEST", listen_on="unix:/tmp/mykitty")

        ensure_tmux_session_ready.assert_called_once_with("ccm-frontend-agent-abcd1234")
        tmux_paste.assert_called_once_with("ccm-frontend-agent-abcd1234", "HEADLESS_DELIVERY_TEST")
        time_sleep.assert_called_once()
        tmux_send_enter.assert_called_once_with("ccm-frontend-agent-abcd1234")
        send_message_to_kitty_window.assert_not_called()
        self.assertEqual(payload["tmux_session"], "ccm-frontend-agent-abcd1234")

    def test_render_wechat_guide_for_agent_covers_phone_onboarding(self):
        guide_text = ccm.render_wechat_guide("agent")

        self.assertIn("wechat-connect", guide_text)
        self.assertIn("scan the QR code", guide_text)
        self.assertIn("wechat-bind", guide_text)
        self.assertIn("wechat-reply", guide_text)
        self.assertIn("kitty:<tab-title>", guide_text)
        self.assertIn("tmux:<session-name>", guide_text)
        self.assertIn("wechat-send", guide_text)


class WeChatPhoneTests(unittest.TestCase):
    def test_active_wechat_user_id_prefers_state_user_id_when_known(self):
        state = ccm.WeChatTransportState(
            token="bot-token",
            user_id="alice@im.wechat",
            context_tokens={
                "alice@im.wechat": "ctx-1",
                "bob@im.wechat": "ctx-2",
            },
        )

        user_id = ccm.active_wechat_user_id(state)

        self.assertEqual(user_id, "alice@im.wechat")

    def test_active_wechat_user_id_uses_single_known_contact(self):
        state = ccm.WeChatTransportState(
            token="bot-token",
            context_tokens={"alice@im.wechat": "ctx-1"},
        )

        user_id = ccm.active_wechat_user_id(state)

        self.assertEqual(user_id, "alice@im.wechat")

    @mock.patch("ccm_orchestra.cli.wechat_reply", autospec=True)
    def test_wechat_queue_reply_only_queues_locally(self, wechat_reply):
        state = ccm.WeChatTransportState(
            token="bot-token",
            context_tokens={"alice@im.wechat": "ctx-1"},
        )

        payload = ccm.wechat_queue_reply(state, user_id="alice@im.wechat", text="queued hello")

        self.assertEqual(payload["queued"], True)
        self.assertEqual(payload["pending_count"], 1)
        self.assertEqual(
            state.pending_replies,
            [{"user_id": "alice@im.wechat", "text": "queued hello"}],
        )
        wechat_reply.assert_not_called()

    @mock.patch("ccm_orchestra.cli.wechat_reply", autospec=True)
    def test_queue_and_flush_wechat_reply_sends_immediately(self, wechat_reply):
        state = ccm.WeChatTransportState(
            token="bot-token",
            context_tokens={"alice@im.wechat": "ctx-1"},
        )
        wechat_reply.return_value = {"ok": True, "user_id": "alice@im.wechat"}

        payload = ccm.queue_and_flush_wechat_reply(
            state,
            user_id="alice@im.wechat",
            text="queued hello",
        )

        self.assertEqual(payload["queued"], True)
        self.assertEqual(payload["sent_count"], 1)
        self.assertEqual(state.pending_replies, [])
        wechat_reply.assert_called_once_with(state, user_id="alice@im.wechat", text="queued hello")

    def test_load_and_save_wechat_transport_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wechat.json"
            state = ccm.WeChatTransportState(
                token="bot-token",
                account_id="bot-1",
                user_id="user-1",
                saved_at="2026-03-30T00:00:00Z",
                sync_buf="cursor-1",
                context_tokens={"alice@im.wechat": "ctx-1"},
                bound_target="kitty:mycel",
            )

            ccm.save_wechat_transport_state(state, path)
            loaded = ccm.load_wechat_transport_state(path)

        self.assertEqual(loaded.token, "bot-token")
        self.assertEqual(loaded.bound_target, "kitty:mycel")
        self.assertEqual(loaded.context_tokens["alice@im.wechat"], "ctx-1")

    @mock.patch("ccm_orchestra.cli.wechat_http_json", autospec=True)
    def test_wechat_connect_persists_direct_bot_credentials(self, wechat_http_json):
        wechat_http_json.side_effect = [
            {
                "qrcode": "qr-token",
                "qrcode_img_content": "https://liteapp.weixin.qq.com/q/demo",
            },
            {
                "status": "confirmed",
                "bot_token": "bot-token",
                "ilink_bot_id": "bot-1",
                "ilink_user_id": "user-1",
                "baseurl": "https://ilinkai.weixin.qq.com",
            },
        ]

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("ccm_orchestra.cli.render_qr_png", return_value=Path("/tmp/wechat-qr.png")), \
             mock.patch("ccm_orchestra.cli.open_qr_preview", autospec=True):
            path = Path(tmp) / "transport.json"
            payload = ccm.wechat_connect(
                state_path=path,
                open_preview=False,
                poll_interval=0.0,
                wait_seconds=0.0,
            )
            saved = ccm.load_wechat_transport_state(path)

        self.assertEqual(payload["status"], "confirmed")
        self.assertEqual(saved.token, "bot-token")
        self.assertEqual(saved.account_id, "bot-1")
        self.assertEqual(saved.user_id, "user-1")

    @mock.patch("ccm_orchestra.cli.wechat_http_json", autospec=True)
    def test_wechat_connect_caps_poll_timeout_by_remaining_wait_window(self, wechat_http_json):
        wechat_http_json.side_effect = [
            {
                "qrcode": "qr-token",
                "qrcode_img_content": "https://liteapp.weixin.qq.com/q/demo",
            },
            {"status": "wait"},
            {"status": "wait"},
        ]

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("ccm_orchestra.cli.time.time", side_effect=[100.0, 100.1, 100.2, 101.1, 101.1]), \
             mock.patch("ccm_orchestra.cli.time.sleep", autospec=True), \
             mock.patch("ccm_orchestra.cli.render_qr_png", return_value=Path("/tmp/wechat-qr.png")), \
             mock.patch("ccm_orchestra.cli.open_qr_preview", autospec=True):
            ccm.wechat_connect(
                state_path=Path(tmp) / "transport.json",
                open_preview=False,
                poll_interval=0.2,
                wait_seconds=1.0,
            )

        timeout = wechat_http_json.call_args_list[1].kwargs["timeout"]
        self.assertLess(timeout, 5.0)

    @mock.patch("ccm_orchestra.cli.wechat_http_json", autospec=True)
    def test_wechat_connect_can_resume_existing_qrcode(self, wechat_http_json):
        wechat_http_json.return_value = {
            "status": "confirmed",
            "bot_token": "bot-token",
            "ilink_bot_id": "bot-1",
            "ilink_user_id": "user-1",
            "baseurl": "https://ilinkai.weixin.qq.com",
        }

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("ccm_orchestra.cli.render_qr_png", autospec=True) as render_qr_png, \
             mock.patch("ccm_orchestra.cli.open_qr_preview", autospec=True) as open_qr_preview:
            path = Path(tmp) / "transport.json"
            payload = ccm.wechat_connect(
                state_path=path,
                open_preview=False,
                poll_interval=0.0,
                wait_seconds=5.0,
                qrcode="qr-token",
            )
            saved = ccm.load_wechat_transport_state(path)

        render_qr_png.assert_not_called()
        open_qr_preview.assert_not_called()
        self.assertEqual(payload["qrcode"], "qr-token")
        self.assertEqual(payload["status"], "confirmed")
        self.assertEqual(saved.token, "bot-token")
        self.assertEqual(saved.account_id, "bot-1")

    @mock.patch("ccm_orchestra.cli.subprocess.Popen", autospec=True)
    def test_launch_wechat_watch_daemon_writes_pidfile_and_returns_paths(self, popen):
        process = mock.Mock()
        process.pid = 43210
        popen.return_value = process

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(
                 "os.environ",
                {
                     "CCM_WECHAT_WATCH_PID_PATH": str(Path(tmp) / "watch.pid"),
                     "CCM_WECHAT_WATCH_LOG_PATH": str(Path(tmp) / "watch.log"),
                     "CCM_WECHAT_WATCH_STATE_PATH": str(Path(tmp) / "watch.json"),
                },
                clear=False,
             ):
            payload = ccm.launch_wechat_watch_daemon(listen_on="unix:/tmp/mykitty", poll_interval=2.5)
            persisted = json.loads(Path(payload["state_path"]).read_text())

            self.assertEqual(payload["pid"], 43210)
            self.assertEqual(Path(payload["pid_path"]).read_text().strip(), "43210")
            self.assertEqual(Path(payload["log_path"]).name, "watch.log")
            self.assertEqual(persisted["pid"], 43210)
            self.assertEqual(persisted["status"], "starting")
            self.assertTrue(persisted["heartbeat_at"])
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertIn("wechat-watch", popen.call_args.args[0])

    def test_wechat_watch_status_reads_running_pid(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(
                 "os.environ",
                {
                     "CCM_WECHAT_WATCH_PID_PATH": str(Path(tmp) / "watch.pid"),
                     "CCM_WECHAT_WATCH_LOG_PATH": str(Path(tmp) / "watch.log"),
                     "CCM_WECHAT_WATCH_STATE_PATH": str(Path(tmp) / "watch.json"),
                },
                clear=False,
             ), \
             mock.patch("ccm_orchestra.cli.pid_is_running", return_value=True):
            Path(tmp, "watch.pid").write_text("43210\n")
            Path(tmp, "watch.json").write_text(
                json.dumps(
                    {
                        "pid": 43210,
                        "heartbeat_at": "2026-03-30T08:00:00Z",
                        "last_error": "",
                        "last_poll_at": "2026-03-30T08:00:01Z",
                        "last_delivery_at": "2026-03-30T08:00:02Z",
                        "last_flush_at": "2026-03-30T08:00:03Z",
                    }
                )
            )
            payload = ccm.wechat_watch_status()

        self.assertTrue(payload["running"])
        self.assertEqual(payload["pid"], 43210)
        self.assertEqual(payload["heartbeat_at"], "2026-03-30T08:00:00Z")
        self.assertEqual(payload["last_poll_at"], "2026-03-30T08:00:01Z")
        self.assertEqual(payload["last_delivery_at"], "2026-03-30T08:00:02Z")
        self.assertEqual(payload["last_flush_at"], "2026-03-30T08:00:03Z")
        self.assertEqual(payload["last_error"], "")

    @mock.patch("ccm_orchestra.cli.os.kill", autospec=True)
    def test_wechat_watch_stop_terminates_running_process_and_clears_pidfile(self, os_kill):
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.dict(
                "os.environ",
                {
                    "CCM_WECHAT_WATCH_PID_PATH": str(Path(tmp) / "watch.pid"),
                    "CCM_WECHAT_WATCH_LOG_PATH": str(Path(tmp) / "watch.log"),
                    "CCM_WECHAT_WATCH_STATE_PATH": str(Path(tmp) / "watch.json"),
                },
                clear=False,
            ), \
            mock.patch("ccm_orchestra.cli.pid_is_running", return_value=True):
            pid_path = Path(tmp) / "watch.pid"
            state_path = Path(tmp) / "watch.json"
            pid_path.write_text("43210\n")
            state_path.write_text(json.dumps({"pid": 43210, "heartbeat_at": "2026-03-30T08:00:00Z", "last_error": ""}))
            payload = ccm.wechat_watch_stop()
            persisted = json.loads(state_path.read_text())

        os_kill.assert_called_once()
        self.assertFalse(pid_path.exists())
        self.assertTrue(payload["stopped"])
        self.assertEqual(persisted["status"], "stopped")

    def test_wechat_watch_refuses_to_overwrite_newer_transport_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transport.json"
            current = ccm.WeChatTransportState(token="old-token", account_id="old-bot")
            replacement = ccm.WeChatTransportState(token="new-token", account_id="new-bot")
            ccm.save_wechat_transport_state(replacement, path)

            with self.assertRaises(ccm.CCMError) as ctx:
                ccm.guard_wechat_transport_state(current, path)

        self.assertIn("replaced", str(ctx.exception))

    def test_wechat_watch_guard_refreshes_bound_target_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transport.json"
            current = ccm.WeChatTransportState(token="same-token", account_id="bot-1", bound_target="kitty:mycel")
            persisted = ccm.WeChatTransportState(
                token="same-token",
                account_id="bot-1",
                bound_target="tmux:ccm-frontend-agent-abcd1234",
            )
            ccm.save_wechat_transport_state(persisted, path)

            ccm.guard_wechat_transport_state(current, path)

        self.assertEqual(current.bound_target, "tmux:ccm-frontend-agent-abcd1234")

    def test_save_wechat_transport_state_guarded_preserves_newer_bound_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transport.json"
            current = ccm.WeChatTransportState(
                token="same-token",
                account_id="bot-1",
                bound_target="tmux:ccm-frontend-agent-abcd1234",
            )
            persisted = ccm.WeChatTransportState(token="same-token", account_id="bot-1", bound_target="kitty:mycel")
            ccm.save_wechat_transport_state(persisted, path)

            ccm.save_wechat_transport_state_guarded(current, path)
            loaded = ccm.load_wechat_transport_state(path)

        self.assertEqual(current.bound_target, "kitty:mycel")
        self.assertEqual(loaded.bound_target, "kitty:mycel")

    def test_wechat_status_payload_reports_connection_and_binding(self):
        state = ccm.WeChatTransportState(
            token="bot-token",
            account_id="bot-1",
            user_id="user-1",
            context_tokens={"alice@im.wechat": "ctx-1", "bob@im.wechat": "ctx-2"},
            bound_target="kitty:mycel",
            saved_at="2026-03-30T08:01:02Z",
        )
        payload = ccm.wechat_status_payload(state)

        self.assertTrue(payload["connected"])
        self.assertEqual(payload["account_id"], "bot-1")
        self.assertEqual(payload["contact_count"], 2)
        self.assertEqual(payload["bound_target"], "kitty:mycel")
        self.assertEqual(payload["saved_at"], "2026-03-30T08:01:02Z")

    @mock.patch("ccm_orchestra.cli.time.strftime", autospec=True, return_value="2026-03-30T09:00:00Z")
    def test_save_wechat_transport_state_refreshes_saved_at(self, time_strftime):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transport.json"
            state = ccm.WeChatTransportState(
                token="bot-token",
                account_id="bot-1",
                saved_at="2026-03-30T08:00:00Z",
            )

            ccm.save_wechat_transport_state(state, path)
            loaded = ccm.load_wechat_transport_state(path)

        self.assertEqual(loaded.saved_at, "2026-03-30T09:00:00Z")

    def test_wechat_bind_updates_bound_target(self):
        state = ccm.WeChatTransportState(token="bot-token")

        updated = ccm.wechat_bind(state, "kitty:mycel")

        self.assertEqual(updated.bound_target, "kitty:mycel")

    @mock.patch("ccm_orchestra.cli.wechat_http_json", autospec=True)
    @mock.patch("ccm_orchestra.cli.deliver_message_to_target", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_target_spec", autospec=True)
    def test_wechat_poll_once_delivers_messages_to_bound_peer(
        self,
        resolve_target_spec,
        deliver_message_to_target,
        wechat_http_json,
    ):
        state = ccm.WeChatTransportState(
            token="bot-token",
            account_id="bot-1",
            user_id="user-1",
            bound_target="kitty:scheduled-tasks",
        )
        resolve_target_spec.return_value = ccm.WeChatTargetRecord(
            target="kitty:scheduled-tasks",
            kind="kitty",
            title="scheduled-tasks",
            window_id="498",
            worktree="/work/scheduled",
            repo_root="/work",
            branch="feat/scheduled",
            tmux_session="",
            agent="",
            agent_status="",
            agent_transcript="",
            runtime="codex",
        )
        deliver_message_to_target.return_value = {"window_id": "498", "title": "scheduled-tasks"}
        wechat_http_json.return_value = {
            "ret": 0,
            "errcode": 0,
            "get_updates_buf": "cursor-1",
            "msgs": [
                {
                    "message_type": 1,
                    "from_user_id": "alice@im.wechat",
                    "context_token": "ctx-1",
                    "item_list": [
                        {"type": 1, "text_item": {"text": "hello from phone"}},
                    ],
                }
            ],
        }

        payload = ccm.wechat_poll_once(
            state,
            listen_on="unix:/tmp/mykitty",
        )

        self.assertEqual(payload["delivered_count"], 1)
        self.assertEqual(state.sync_buf, "cursor-1")
        self.assertEqual(state.context_tokens["alice@im.wechat"], "ctx-1")
        sent_message = deliver_message_to_target.call_args.args[1]
        self.assertIn("hello from phone", sent_message)
        self.assertIn("ccm wechat-reply alice@im.wechat", sent_message)

    @mock.patch("ccm_orchestra.cli.wechat_http_json", autospec=True)
    @mock.patch("ccm_orchestra.cli.deliver_message_to_target", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_target_spec", autospec=True)
    def test_wechat_poll_once_can_deliver_to_headless_claude_peer(
        self,
        resolve_target_spec,
        deliver_message_to_target,
        wechat_http_json,
    ):
        state = ccm.WeChatTransportState(
            token="bot-token",
            account_id="bot-1",
            user_id="user-1",
            bound_target="tmux:ccm-frontend-agent-abcd1234",
        )
        resolve_target_spec.return_value = ccm.WeChatTargetRecord(
            target="tmux:ccm-frontend-agent-abcd1234",
            kind="tmux",
            title="claude-handoff",
            window_id="",
            worktree="/work/ccm",
            repo_root="/work/ccm",
            branch="main",
            tmux_session="ccm-frontend-agent-abcd1234",
            agent="frontend-agent",
            agent_status="running",
            agent_transcript="/tmp/agent.jsonl",
            runtime="claude",
        )
        deliver_message_to_target.return_value = {"window_id": "", "tmux_session": "ccm-frontend-agent-abcd1234"}
        wechat_http_json.return_value = {
            "ret": 0,
            "errcode": 0,
            "get_updates_buf": "cursor-2",
            "msgs": [
                {
                    "message_type": 1,
                    "from_user_id": "alice@im.wechat",
                    "context_token": "ctx-2",
                    "item_list": [
                        {"type": 1, "text_item": {"text": "headless phone hello"}},
                    ],
                }
            ],
        }

        payload = ccm.wechat_poll_once(
            state,
            listen_on="unix:/tmp/mykitty",
        )

        self.assertEqual(payload["delivered_count"], 1)
        self.assertEqual(state.sync_buf, "cursor-2")
        self.assertEqual(deliver_message_to_target.call_args.args[0].target, "tmux:ccm-frontend-agent-abcd1234")
        sent_message = deliver_message_to_target.call_args.args[1]
        self.assertIn("headless phone hello", sent_message)
        self.assertIn("ccm wechat-queue-reply alice@im.wechat", sent_message)
        self.assertIn("local ccm outbox", sent_message)
        self.assertNotIn("<system-reminder>", sent_message)

    @mock.patch("ccm_orchestra.cli.wechat_reply", autospec=True)
    @mock.patch("ccm_orchestra.cli.wechat_http_json", autospec=True)
    def test_wechat_poll_once_flushes_pending_replies_before_waiting_for_updates(
        self,
        wechat_http_json,
        wechat_reply,
    ):
        state = ccm.WeChatTransportState(
            token="bot-token",
            account_id="bot-1",
            user_id="user-1",
            context_tokens={"alice@im.wechat": "ctx-1"},
            pending_replies=[{"user_id": "alice@im.wechat", "text": "queued hello"}],
        )
        wechat_reply.return_value = {"ok": True, "user_id": "alice@im.wechat"}
        wechat_http_json.return_value = {"status": "wait"}

        payload = ccm.wechat_poll_once(
            state,
            listen_on="unix:/tmp/mykitty",
        )

        self.assertEqual(payload["status"], "wait")
        self.assertEqual(len(payload["sent_replies"]), 1)
        self.assertEqual(state.pending_replies, [])
        wechat_reply.assert_called_once_with(state, user_id="alice@im.wechat", text="queued hello")

    @mock.patch("ccm_orchestra.cli.wechat_http_json", autospec=True)
    def test_wechat_reply_uses_saved_context_token(self, wechat_http_json):
        state = ccm.WeChatTransportState(
            token="bot-token",
            account_id="bot-1",
            context_tokens={"alice@im.wechat": "ctx-1"},
        )
        wechat_http_json.return_value = {"ret": 0, "errcode": 0}

        payload = ccm.wechat_reply(state, user_id="alice@im.wechat", text="hello back")

        self.assertEqual(payload["user_id"], "alice@im.wechat")
        args = wechat_http_json.call_args.args
        self.assertEqual(args[0], "POST")
        self.assertIn("sendmessage", args[1])
        body = wechat_http_json.call_args.kwargs["body"]
        self.assertEqual(body["msg"]["to_user_id"], "alice@im.wechat")
        self.assertEqual(body["msg"]["context_token"], "ctx-1")

    def test_wechat_users_payload_lists_known_phone_contacts(self):
        state = ccm.WeChatTransportState(
            token="bot-token",
            context_tokens={
                "alice@im.wechat": "ctx-1",
                "bob@im.wechat": "ctx-2",
            },
        )

        payload = ccm.wechat_users_payload(state)

        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["user_id"], "alice@im.wechat")

    def test_render_incoming_wechat_prompt_includes_reply_command(self):
        rendered = ccm.format_incoming_wechat_prompt(
            user_id="alice@im.wechat",
            text="hello from phone",
            bound_target="kitty:mycel",
            reply_command='ccm wechat-reply alice@im.wechat "..."',
        )

        self.assertIn("<ccm-wechat-incoming>", rendered)
        self.assertIn("Operator authorization", rendered)
        self.assertIn("ccm wechat-reply alice@im.wechat", rendered)
        self.assertIn("hello from phone", rendered)
        self.assertIn("<bound-target>kitty:mycel</bound-target>", rendered)

    def test_render_incoming_wechat_prompt_for_claude_uses_plain_local_queue_language(self):
        rendered = ccm.format_incoming_wechat_prompt(
            user_id="alice@im.wechat",
            text="hello from phone",
            bound_target="tmux:ccm-frontend-agent-abcd1234",
            reply_command='ccm wechat-queue-reply alice@im.wechat "..."',
            runtime="claude",
        )

        self.assertIn("Phone message for your currently bound ccm thread.", rendered)
        self.assertIn("local ccm outbox", rendered)
        self.assertIn("ccm wechat-queue-reply alice@im.wechat", rendered)
        self.assertIn("bound_target: tmux:ccm-frontend-agent-abcd1234", rendered)
        self.assertNotIn("<system-reminder>", rendered)


class CommandBuildTests(unittest.TestCase):
    @mock.patch("ccm_orchestra.cli.current_cac_claude_details", autospec=True)
    @mock.patch("ccm_orchestra.cli.Path.exists", autospec=True)
    @mock.patch("ccm_orchestra.cli.shutil.which", autospec=True)
    def test_resolve_claude_executable_prefers_cac_wrapper_over_path(
        self,
        which,
        path_exists,
        current_cac_claude_details,
    ):
        which.return_value = "/opt/homebrew/bin/claude"
        path_exists.return_value = True
        current_cac_claude_details.return_value = {
            "actual_path": "/Users/test/.cac/versions/2.1.86/claude",
            "config_dir": "/Users/test/.cac/envs/main/.claude",
        }

        resolved = ccm.resolve_claude_executable()

        self.assertEqual(resolved, str(Path.home() / ".cac" / "bin" / "claude"))
        which.assert_not_called()

    @mock.patch.dict("os.environ", {}, clear=True)
    @mock.patch("ccm_orchestra.cli.current_cac_claude_details", autospec=True)
    def test_launch_environment_defaults_config_root_from_cac(self, current_cac_claude_details):
        current_cac_claude_details.return_value = {
            "actual_path": "/Users/test/.cac/versions/2.1.86/claude",
            "config_dir": "/Users/test/.cac/envs/main/.claude",
        }

        env = ccm.launch_environment()

        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/Users/test/.cac/envs/main/.claude")

    @mock.patch("ccm_orchestra.cli.resolve_claude_executable", autospec=True)
    def test_build_claude_command_uses_interactive_mode(self, resolve_claude_executable):
        resolve_claude_executable.return_value = "/Users/test/.cac/bin/claude"
        command = ccm.build_claude_command("frontend-agent")

        self.assertEqual(
            command[:3],
            ["/Users/test/.cac/bin/claude", "--dangerously-skip-permissions", "-n"],
        )
        self.assertNotIn("--print", command)

    @mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": "/Users/test/.cac/envs/main/.claude"}, clear=False)
    @mock.patch("ccm_orchestra.cli.resolve_claude_executable", autospec=True)
    def test_build_tmux_claude_command_pins_binary_and_config_root(self, resolve_claude_executable):
        resolve_claude_executable.return_value = "/Users/test/.cac/bin/claude"

        command = ccm.build_tmux_claude_command("frontend-agent")

        self.assertIn("env", command)
        self.assertIn("CLAUDE_CONFIG_DIR=/Users/test/.cac/envs/main/.claude", command)
        self.assertIn("/Users/test/.cac/bin/claude", command)

    def test_format_relay_message_includes_sender_identity_and_reply_hint(self):
        sender = {
            "title": "main",
            "worktree": "/work/app",
            "branch": "feat/demo",
            "repo_root": "/work",
            "agent": "frontend-agent",
            "agent_tmux_session": "ccm-frontend-agent-1234",
            "agent_transcript": "/tmp/demo.jsonl",
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
        self.assertIn("agent: frontend-agent", rendered)
        self.assertIn("tmux: ccm-frontend-agent-1234", rendered)
        self.assertIn('reply-via: ccm relay main "..."', rendered)
        self.assertTrue(rendered.endswith("Please review the current frontend."))


class LifecycleTests(unittest.TestCase):
    @mock.patch("ccm_orchestra.cli.build_tmux_claude_command", autospec=True)
    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.ensure_session_ready", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_has_session", autospec=True)
    @mock.patch("ccm_orchestra.cli.require_binary", autospec=True)
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
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
        build_tmux_claude_command.return_value = "env CLAUDE_CONFIG_DIR=/Users/test/.cac/envs/main/.claude /Users/test/.cac/bin/claude --dangerously-skip-permissions -n frontend-agent"
        state = ccm.State()

        record = ccm.start_session(state, "frontend-agent", "/work/app")

        self.assertEqual(record.tmux_session, ccm.build_tmux_session_name("frontend-agent", "/work/app"))
        command = run_command.call_args_list[0].args[0]
        self.assertEqual(command[:6], ["tmux", "new-session", "-d", "-s", record.tmux_session, "-c"])
        self.assertEqual(command[6], "/work/app")
        self.assertEqual(
            command[7],
            "env CLAUDE_CONFIG_DIR=/Users/test/.cac/envs/main/.claude /Users/test/.cac/bin/claude --dangerously-skip-permissions -n frontend-agent",
        )
        self.assertIn("frontend-agent", state.sessions)

    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_transcript", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_send_enter", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_paste", autospec=True)
    @mock.patch("ccm_orchestra.cli.ensure_session_ready", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_has_session", autospec=True)
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
            name="frontend-agent",
            tmux_session="ccm-frontend-agent",
            display_name="frontend-agent",
            cwd="/work/app",
            started_at=0.0,
        )
        state = ccm.State(sessions={"frontend-agent": record})

        updated = ccm.send_prompt(state, "frontend-agent", "build the page")

        tmux_paste.assert_called_once_with("ccm-frontend-agent", "build the page")
        tmux_send_enter.assert_called_once_with("ccm-frontend-agent")
        _sleep.assert_any_call(ccm.TMUX_PASTE_SUBMIT_DELAY_SECONDS)
        self.assertEqual(updated.transcript_path, "/tmp/transcript.jsonl")

    @mock.patch("ccm_orchestra.cli.require_binary", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_has_session", autospec=True)
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
    def test_open_in_kitty_launches_marked_tab(self, run_command, tmux_has_session, require_binary):
        tmux_has_session.return_value = True
        run_command.return_value = mock.Mock(stdout="")
        record = ccm.SessionRecord(
            name="frontend-agent",
            tmux_session="ccm-frontend-agent",
            display_name="frontend-agent",
            cwd="/work/app",
            started_at=0.0,
        )
        state = ccm.State(sessions={"frontend-agent": record})

        payload = ccm.open_in_kitty(state, "frontend-agent", "unix:/tmp/mykitty")

        command = run_command.call_args.args[0]
        self.assertEqual(command[:5], ["kitty", "@", "--to", "unix:/tmp/mykitty", "launch"])
        self.assertIn("[ccm:frontend-agent]", command)
        self.assertEqual(payload["title"], "[ccm:frontend-agent]")

    @mock.patch("ccm_orchestra.cli.workspace_identity", autospec=True)
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
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
            "agent": "frontend-agent",
            "agent_status": "running",
            "agent_tmux_session": "ccm-frontend-agent-1234",
            "agent_transcript": "/tmp/demo.jsonl",
        }

        tabs = ccm.list_kitty_tabs("unix:/tmp/mykitty")

        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["title"], "feat/main-thread-for-member")
        self.assertEqual(tabs[0]["branch"], "feat/demo")
        self.assertEqual(tabs[0]["agent"], "frontend-agent")
        self.assertEqual(tabs[0]["agent_status"], "running")

    @mock.patch("ccm_orchestra.cli.send_message_to_kitty_tab", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_current_sender_context", autospec=True)
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
            "agent": "frontend-agent",
            "agent_status": "running",
            "agent_tmux_session": "ccm-frontend-agent-1234",
            "agent_transcript": "/tmp/demo.jsonl",
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

    @mock.patch("ccm_orchestra.cli.tmux_send_enter", autospec=True)
    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.send_message_to_kitty_tab", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_current_sender_context", autospec=True)
    def test_relay_message_to_kitty_tab_submits_tmux_backed_visible_agent(
        self,
        resolve_current_sender_context,
        send_message_to_kitty_tab,
        time_sleep,
        tmux_send_enter,
    ):
        resolve_current_sender_context.return_value = {
            "title": "main",
            "worktree": "/work/app",
            "repo_root": "/work",
            "branch": "feat/demo",
            "agent": "frontend-agent",
            "agent_status": "running",
            "agent_tmux_session": "ccm-frontend-agent-1234",
            "agent_transcript": "/tmp/demo.jsonl",
        }
        send_message_to_kitty_tab.return_value = {
            "title": "target",
            "window_id": "550",
            "endpoint": "unix:/tmp/mykitty",
            "agent_tmux_session": "ccm-target-agent-9999",
        }

        payload = ccm.relay_message_to_kitty_tab(
            "target",
            "Please review this branch.",
            "unix:/tmp/mykitty",
            cwd="/work/app",
        )

        time_sleep.assert_called_once_with(ccm.TMUX_PASTE_SUBMIT_DELAY_SECONDS)
        tmux_send_enter.assert_called_once_with("ccm-target-agent-9999")
        self.assertEqual(payload["title"], "target")

    @mock.patch("ccm_orchestra.cli.tmux_send_enter", autospec=True)
    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.send_message_to_kitty_tab", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_current_sender_context", autospec=True)
    def test_relay_message_to_kitty_tab_does_not_submit_tmux_for_plain_visible_tab(
        self,
        resolve_current_sender_context,
        send_message_to_kitty_tab,
        time_sleep,
        tmux_send_enter,
    ):
        resolve_current_sender_context.return_value = {
            "title": "main",
            "worktree": "/work/app",
            "repo_root": "/work",
            "branch": "feat/demo",
            "agent": "frontend-agent",
            "agent_status": "running",
            "agent_tmux_session": "ccm-frontend-agent-1234",
            "agent_transcript": "/tmp/demo.jsonl",
        }
        send_message_to_kitty_tab.return_value = {
            "title": "target",
            "window_id": "550",
            "endpoint": "unix:/tmp/mykitty",
            "agent_tmux_session": "",
        }

        payload = ccm.relay_message_to_kitty_tab(
            "target",
            "Please review this branch.",
            "unix:/tmp/mykitty",
            cwd="/work/app",
        )

        time_sleep.assert_not_called()
        tmux_send_enter.assert_not_called()
        self.assertEqual(payload["title"], "target")


class ReadWaitTests(unittest.TestCase):
    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.read_incremental_jsonl", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_transcript", autospec=True)
    def test_read_updates_waits_for_late_events(self, resolve_transcript, read_incremental_jsonl, _sleep):
        transcript = Path("/tmp/transcript.jsonl")
        resolve_transcript.return_value = transcript
        read_incremental_jsonl.side_effect = [
            ([], 0, ""),
            ([{"type": "assistant", "message": {"content": [{"type": "text", "text": "ready"}]}}], 10, ""),
        ]
        state = ccm.State(
            sessions={
                "frontend-agent": ccm.SessionRecord(
                    name="frontend-agent",
                    tmux_session="ccm-frontend-agent",
                    display_name="frontend-agent",
                    cwd="/work/app",
                    started_at=0.0,
                )
            }
        )

        events = ccm.read_updates(state, "frontend-agent", wait_seconds=3, poll_interval=1)

        self.assertEqual(events, [{"kind": "assistant", "text": "ready"}])
        self.assertEqual(read_incremental_jsonl.call_count, 2)

    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.candidate_projects_roots", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_transcript", autospec=True)
    def test_read_updates_reports_search_diagnostics_when_transcript_missing(
        self,
        resolve_transcript,
        candidate_projects_roots,
        _sleep,
    ):
        resolve_transcript.return_value = None
        candidate_projects_roots.return_value = [Path("/tmp/cac/projects"), Path("/tmp/fallback/projects")]
        state = ccm.State(
            sessions={
                "frontend-agent": ccm.SessionRecord(
                    name="frontend-agent",
                    tmux_session="ccm-frontend-agent",
                    display_name="frontend-agent",
                    cwd="/work/app",
                    started_at=0.0,
                )
            }
        )

        with self.assertRaisesRegex(ccm.CCMError, "Transcript search roots: /tmp/cac/projects, /tmp/fallback/projects"):
            ccm.read_updates(state, "frontend-agent", wait_seconds=0, poll_interval=1)

    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.read_incremental_jsonl", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_transcript", autospec=True)
    def test_read_updates_raw_returns_unrendered_events(self, resolve_transcript, read_incremental_jsonl, _sleep):
        transcript = Path("/tmp/transcript.jsonl")
        resolve_transcript.return_value = transcript
        raw_event = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "ready"}],
            },
        }
        read_incremental_jsonl.return_value = ([raw_event], 10, "")
        state = ccm.State(
            sessions={
                "frontend-agent": ccm.SessionRecord(
                    name="frontend-agent",
                    tmux_session="ccm-frontend-agent",
                    display_name="frontend-agent",
                    cwd="/work/app",
                    started_at=0.0,
                )
            }
        )

        events = ccm.read_updates(state, "frontend-agent", wait_seconds=0, poll_interval=1, raw=True)

        self.assertEqual(events, [raw_event])

    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.read_incremental_jsonl", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_transcript", autospec=True)
    def test_read_updates_raw_waits_for_late_events(self, resolve_transcript, read_incremental_jsonl, _sleep):
        transcript = Path("/tmp/transcript.jsonl")
        resolve_transcript.return_value = transcript
        raw_event = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "ready"}],
            },
        }
        read_incremental_jsonl.side_effect = [
            ([], 0, ""),
            ([raw_event], 10, ""),
        ]
        state = ccm.State(
            sessions={
                "frontend-agent": ccm.SessionRecord(
                    name="frontend-agent",
                    tmux_session="ccm-frontend-agent",
                    display_name="frontend-agent",
                    cwd="/work/app",
                    started_at=0.0,
                )
            }
        )

        events = ccm.read_updates(state, "frontend-agent", wait_seconds=3, poll_interval=1, raw=True)

        self.assertEqual(events, [raw_event])
        self.assertEqual(read_incremental_jsonl.call_count, 2)


class DoctorTests(unittest.TestCase):
    @mock.patch("ccm_orchestra.cli.claude_version_from_binary", autospec=True)
    @mock.patch("ccm_orchestra.cli.shutil.which", autospec=True)
    def test_doctor_report_includes_binary_and_state_info(self, which, claude_version_from_binary):
        which.side_effect = lambda name, path=None: f"/usr/bin/{name}"
        claude_version_from_binary.return_value = "Claude Code v2.1.86"
        state = ccm.State(
            sessions={
                "frontend-agent": ccm.SessionRecord(
                    name="frontend-agent",
                    tmux_session="ccm-frontend-agent",
                    display_name="frontend-agent",
                    cwd="/work/app",
                    started_at=0.0,
                )
            }
        )

        report = ccm.doctor_report(state, "/work/app", Path("/tmp/state.json"))

        self.assertEqual(report["cwd"], "/work/app")


class InspectTests(unittest.TestCase):
    @mock.patch("ccm_orchestra.cli.tmux_has_session", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_capture", autospec=True)
    @mock.patch("ccm_orchestra.cli.session_status", autospec=True)
    @mock.patch("ccm_orchestra.cli.resolve_transcript", autospec=True)
    def test_inspect_session_reports_state_tmux_and_transcript_context(
        self,
        resolve_transcript,
        session_status,
        tmux_capture,
        tmux_has_session,
    ):
        resolve_transcript.return_value = Path("/tmp/transcript.jsonl")
        session_status.return_value = "running"
        tmux_has_session.return_value = True
        tmux_capture.return_value = "line 1\nline 2\n❯ \n"
        record = ccm.SessionRecord(
            name="frontend-agent",
            tmux_session="ccm-frontend-agent",
            display_name="frontend-agent",
            cwd="/work/app",
            started_at=123.0,
        )
        state = ccm.State(sessions={"frontend-agent": record})

        payload = ccm.inspect_session(state, "frontend-agent", Path("/tmp/state.json"))

        self.assertEqual(payload["name"], "frontend-agent")
        self.assertEqual(payload["state_path"], "/tmp/state.json")
        self.assertEqual(payload["tmux_session"], "ccm-frontend-agent")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["transcript_path"], "/tmp/transcript.jsonl")
        self.assertIn("❯", payload["pane_tail"])

    def test_inspect_help_mentions_pane_tail_and_transcript_debug(self):
        parser = ccm.build_parser()
        inspect_parser = parser._subparsers._group_actions[0].choices["inspect"]
        help_text = inspect_parser.format_help()

        self.assertIn("pane tail", help_text)
        self.assertIn("legacy", help_text)
        self.assertIn("debug", help_text)


class CleanupTests(unittest.TestCase):
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_has_session", autospec=True)
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

    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
    @mock.patch("ccm_orchestra.cli.tmux_has_session", autospec=True)
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

    def test_kitty_tab_runs_codex_accepts_bare_and_absolute_binary(self):
        self.assertTrue(ccm.kitty_tab_runs_codex({"cmdline": "codex --dangerously-bypass-approvals-and-sandbox"}))
        self.assertTrue(ccm.kitty_tab_runs_codex({"cmdline": "/opt/homebrew/bin/codex --dangerously-bypass-approvals-and-sandbox"}))
        self.assertFalse(ccm.kitty_tab_runs_codex({"cmdline": "zsh -i"}))
        self.assertFalse(ccm.kitty_tab_runs_codex({"cmdline": ""}))

    @mock.patch("ccm_orchestra.cli.require_binary", autospec=True)
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
    def test_kitty_window_cmdline_returns_matching_window_command(self, run_command, require_binary):
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
                                        "cwd": "/work/main",
                                        "cmdline": ["zsh"],
                                    }
                                ],
                            },
                            {
                                "title": "relay-codex",
                                "windows": [
                                    {
                                        "id": 31,
                                        "is_active": True,
                                        "cwd": "/work/tasks",
                                        "cmdline": ["codex", "--dangerously-bypass-approvals-and-sandbox"],
                                    }
                                ],
                            },
                        ]
                    }
                ]
            )
        )

        cmdline = ccm.kitty_window_cmdline("31", "unix:/tmp/mykitty")

        self.assertEqual(cmdline, "codex --dangerously-bypass-approvals-and-sandbox")
        run_command.assert_called_once_with(["kitty", "@", "--to", "unix:/tmp/mykitty", "ls"])

    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
    def test_submit_kitty_window_sends_single_enter_for_plain_window(self, run_command, time_sleep):
        ccm.submit_kitty_window("unix:/tmp/mykitty", "31", codex_retry=False)

        self.assertEqual(run_command.call_args_list[0].args[0][:5], ["kitty", "@", "--to", "unix:/tmp/mykitty", "send-key"])
        self.assertEqual(len(run_command.call_args_list), 1)
        time_sleep.assert_called_once_with(ccm.TMUX_PASTE_SUBMIT_DELAY_SECONDS)

    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
    def test_submit_kitty_window_retries_enter_for_codex_window(self, run_command, time_sleep):
        ccm.submit_kitty_window("unix:/tmp/mykitty", "31", codex_retry=True)

        self.assertEqual(run_command.call_args_list[0].args[0][:5], ["kitty", "@", "--to", "unix:/tmp/mykitty", "send-key"])
        self.assertEqual(run_command.call_args_list[1].args[0][:5], ["kitty", "@", "--to", "unix:/tmp/mykitty", "send-key"])
        self.assertEqual(
            time_sleep.call_args_list,
            [
                mock.call(ccm.TMUX_PASTE_SUBMIT_DELAY_SECONDS),
                mock.call(ccm.CODEX_SUBMIT_RETRY_DELAY_SECONDS),
            ],
        )

    @mock.patch("ccm_orchestra.cli.workspace_identity", autospec=True)
    @mock.patch("ccm_orchestra.cli.require_binary", autospec=True)
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
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
                                "title": "Claude Agent",
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
                "agent": "",
                "agent_status": "",
                "agent_tmux_session": "",
                "agent_transcript": "",
            },
            {
                "worktree": "/work/ui",
                "repo_root": "/work",
                "branch": "feat/ui",
                "agent": "frontend-agent",
                "agent_status": "running",
                "agent_tmux_session": "ccm-frontend-agent-1234",
                "agent_transcript": "/tmp/demo.jsonl",
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
                    "agent": "",
                    "agent_status": "",
                    "agent_tmux_session": "",
                    "agent_transcript": "",
                },
                {
                    "title": "Claude Agent",
                    "window_id": "21",
                    "cwd": "/work/ui",
                    "cmdline": "claude",
                    "branch": "feat/ui",
                    "repo_root": "/work",
                    "agent": "frontend-agent",
                    "agent_status": "running",
                    "agent_tmux_session": "ccm-frontend-agent-1234",
                    "agent_transcript": "/tmp/demo.jsonl",
                },
            ],
        )

    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.workspace_identity", autospec=True)
    @mock.patch("ccm_orchestra.cli.require_binary", autospec=True)
    @mock.patch("ccm_orchestra.cli.submit_kitty_window", autospec=True)
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
    def test_send_message_to_kitty_tab_injects_text_and_enter(
        self,
        run_command,
        submit_kitty_window,
        require_binary,
        workspace_identity,
        time_sleep,
    ):
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
                                            "cmdline": ["zsh"],
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
            "agent": "frontend-agent",
            "agent_status": "running",
            "agent_tmux_session": "ccm-frontend-agent-1234",
            "agent_transcript": "/tmp/demo.jsonl",
        }

        payload = ccm.send_message_to_kitty_tab(
            "scheduled-tasks",
            "Please review the frontend.",
            "unix:/tmp/mykitty",
        )

        self.assertEqual(payload["title"], "scheduled-tasks")
        self.assertEqual(payload["window_id"], "31")
        self.assertEqual(payload["agent_tmux_session"], "ccm-frontend-agent-1234")
        self.assertEqual(run_command.call_args_list[1].args[0][:5], ["kitty", "@", "--to", "unix:/tmp/mykitty", "send-text"])
        submit_kitty_window.assert_called_once_with("unix:/tmp/mykitty", "31", codex_retry=False)
        time_sleep.assert_not_called()

    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.workspace_identity", autospec=True)
    @mock.patch("ccm_orchestra.cli.require_binary", autospec=True)
    @mock.patch("ccm_orchestra.cli.submit_kitty_window", autospec=True)
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
    def test_send_message_to_kitty_tab_retries_enter_for_codex_tabs(
        self,
        run_command,
        submit_kitty_window,
        require_binary,
        workspace_identity,
        time_sleep,
    ):
        run_command.side_effect = [
            mock.Mock(
                stdout=json.dumps(
                    [
                        {
                            "tabs": [
                                {
                                    "title": "relay-codex-lab",
                                    "windows": [
                                        {
                                            "id": 31,
                                            "is_active": True,
                                            "cwd": "/work/tasks",
                                            "cmdline": ["codex", "--dangerously-bypass-approvals-and-sandbox"],
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
            mock.Mock(stdout=""),
        ]
        workspace_identity.return_value = {
            "worktree": "/work/tasks",
            "repo_root": "/work",
            "branch": "relay-codex-lab",
            "agent": "",
            "agent_status": "",
            "agent_tmux_session": "",
            "agent_transcript": "",
        }

        payload = ccm.send_message_to_kitty_tab(
            "relay-codex-lab",
            "Please review the frontend.",
            "unix:/tmp/mykitty",
        )

        self.assertEqual(payload["title"], "relay-codex-lab")
        self.assertEqual(run_command.call_args_list[1].args[0][:5], ["kitty", "@", "--to", "unix:/tmp/mykitty", "send-text"])
        submit_kitty_window.assert_called_once_with("unix:/tmp/mykitty", "31", codex_retry=True)
        time_sleep.assert_not_called()

    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.require_binary", autospec=True)
    @mock.patch("ccm_orchestra.cli.submit_kitty_window", autospec=True)
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
    def test_send_message_to_kitty_window_waits_a_beat_before_enter(
        self,
        run_command,
        submit_kitty_window,
        require_binary,
        time_sleep,
    ):
        run_command.side_effect = [
            mock.Mock(
                stdout=json.dumps(
                    [
                        {
                            "tabs": [
                                {
                                    "title": "plain-window",
                                    "windows": [
                                        {
                                            "id": 31,
                                            "is_active": True,
                                            "cwd": "/work/tasks",
                                            "env": {"PWD": "/work/tasks"},
                                            "cmdline": ["zsh"],
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

        payload = ccm.send_message_to_kitty_window(
            "31",
            "Please review the frontend.",
            "unix:/tmp/mykitty",
        )

        self.assertEqual(payload["window_id"], "31")
        self.assertEqual(run_command.call_args_list[1].args[0][:5], ["kitty", "@", "--to", "unix:/tmp/mykitty", "send-text"])
        submit_kitty_window.assert_called_once_with("unix:/tmp/mykitty", "31", codex_retry=False)
        time_sleep.assert_not_called()

    @mock.patch("ccm_orchestra.cli.time.sleep", autospec=True)
    @mock.patch("ccm_orchestra.cli.require_binary", autospec=True)
    @mock.patch("ccm_orchestra.cli.submit_kitty_window", autospec=True)
    @mock.patch("ccm_orchestra.cli.run_command", autospec=True)
    def test_send_message_to_kitty_window_retries_enter_for_codex_windows(
        self,
        run_command,
        submit_kitty_window,
        require_binary,
        time_sleep,
    ):
        run_command.side_effect = [
            mock.Mock(
                stdout=json.dumps(
                    [
                        {
                            "tabs": [
                                {
                                    "title": "relay-codex-window",
                                    "windows": [
                                        {
                                            "id": 31,
                                            "is_active": True,
                                            "cwd": "/work/tasks",
                                            "env": {"PWD": "/work/tasks"},
                                            "cmdline": ["codex", "--dangerously-bypass-approvals-and-sandbox"],
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
            mock.Mock(stdout=""),
        ]

        payload = ccm.send_message_to_kitty_window(
            "31",
            "Please review the frontend.",
            "unix:/tmp/mykitty",
        )

        self.assertEqual(payload["window_id"], "31")
        self.assertEqual(run_command.call_args_list[1].args[0][:5], ["kitty", "@", "--to", "unix:/tmp/mykitty", "send-text"])
        submit_kitty_window.assert_called_once_with("unix:/tmp/mykitty", "31", codex_retry=True)
        time_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
