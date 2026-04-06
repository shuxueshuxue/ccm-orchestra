#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any


STATE_VERSION = 1
DEFAULT_HOME_ROOT = Path("~/.claude-codex-manager").expanduser()
DEFAULT_CLAUDE_PROJECTS_ROOT = Path(
    os.environ.get("CCM_CLAUDE_PROJECTS_ROOT", "~/.claude/projects")
).expanduser()
READY_DELAY_SECONDS = 2.0
DEFAULT_READY_TIMEOUT_SECONDS = 300.0
TMUX_PASTE_SUBMIT_DELAY_SECONDS = 0.2
READY_CONFIRMATION_PASSES = 2
WECHAT_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
WECHAT_BOT_TYPE = "3"
WECHAT_CHANNEL_VERSION = "0.1.0"
WECHAT_LONG_POLL_TIMEOUT_SECONDS = 35.0
WECHAT_SEND_TIMEOUT_SECONDS = 15.0
WECHAT_MSG_TYPE_USER = 1
WECHAT_MSG_TYPE_BOT = 2
WECHAT_MSG_ITEM_TEXT = 1
WECHAT_MSG_STATE_FINISH = 2


class CCMError(RuntimeError):
    pass


@dataclass
class SessionRecord:
    name: str
    tmux_session: str
    display_name: str
    cwd: str
    started_at: float
    transcript_path: str | None = None
    transcript_offset: int = 0
    transcript_buffer: str = ""


@dataclass
class State:
    version: int = STATE_VERSION
    sessions: dict[str, SessionRecord] = field(default_factory=dict)


@dataclass
class WeChatTargetSpec:
    kind: str
    value: str


@dataclass
class WeChatTargetRecord:
    target: str
    kind: str
    title: str
    window_id: str
    worktree: str
    repo_root: str
    branch: str
    tmux_session: str
    agent: str
    agent_status: str
    agent_transcript: str
    runtime: str


@dataclass
class WeChatTransportState:
    token: str
    base_url: str = WECHAT_DEFAULT_BASE_URL
    account_id: str = ""
    user_id: str = ""
    saved_at: str = ""
    sync_buf: str = ""
    context_tokens: dict[str, str] = field(default_factory=dict)
    bound_target: str = ""
    pending_replies: list[dict[str, str]] = field(default_factory=list)


def sanitize_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    normalized = normalized.strip("-")
    if not normalized:
        raise ValueError("Session name becomes empty after normalization")
    return normalized


def resolve_claude_executable() -> str:
    cac_wrapper = Path.home() / ".cac" / "bin" / "claude"
    if cac_wrapper.exists():
        return str(cac_wrapper)
    cac_details = current_cac_claude_details()
    if cac_details.get("actual_path"):
        return cac_details["actual_path"]
    path = shutil.which("claude")
    if path is None:
        raise CCMError("Missing required binary: claude")
    return path


def launch_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        config_dir = current_cac_claude_details().get("config_dir", "")
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def build_claude_command(display_name: str) -> list[str]:
    return [resolve_claude_executable(), "--dangerously-skip-permissions", "-n", display_name]


def build_tmux_claude_command(display_name: str) -> str:
    command = build_claude_command(display_name)
    env_prefix = [f"{key}={value}" for key, value in launch_environment().items()]
    if env_prefix:
        # @@@tmux-launch-env - tmux sessions do not reliably inherit the caller's Claude
        # wrapper environment, so the launch command pins both the resolved binary path
        # and the active Claude config root instead of trusting tmux's stale PATH.
        command = ["env", *env_prefix, *command]
    return shlex.join(command)


def _parse_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def namespace_suffix(cwd: str) -> str:
    return hashlib.sha1(str(Path(cwd).resolve()).encode("utf-8")).hexdigest()[:8]


def default_state_path(cwd: str | None = None) -> Path:
    if "CCM_HOME" in os.environ:
        return Path(os.environ["CCM_HOME"]).expanduser() / "state.json"
    target_cwd = cwd or os.getcwd()
    return DEFAULT_HOME_ROOT / namespace_suffix(target_cwd) / "state.json"


def discover_state_paths(home_root: Path | None = None) -> list[Path]:
    home_root = home_root or DEFAULT_HOME_ROOT
    if not home_root.exists():
        return []
    return sorted(path for path in home_root.glob("*/state.json") if path.is_file())


def wechat_transport_state_path() -> Path:
    if "CCM_WECHAT_TRANSPORT_PATH" in os.environ:
        return Path(os.environ["CCM_WECHAT_TRANSPORT_PATH"]).expanduser()
    if "CCM_WECHAT_AUTH_PATH" in os.environ:
        return Path(os.environ["CCM_WECHAT_AUTH_PATH"]).expanduser()
    return DEFAULT_HOME_ROOT / "wechat-transport.json"


def wechat_qr_output_path() -> Path:
    if "CCM_WECHAT_QR_PATH" in os.environ:
        return Path(os.environ["CCM_WECHAT_QR_PATH"]).expanduser()
    return DEFAULT_HOME_ROOT / "wechat" / "current-qr.png"


def wechat_watch_pid_path() -> Path:
    if "CCM_WECHAT_WATCH_PID_PATH" in os.environ:
        return Path(os.environ["CCM_WECHAT_WATCH_PID_PATH"]).expanduser()
    return DEFAULT_HOME_ROOT / "wechat-watch.pid"


def wechat_watch_log_path() -> Path:
    if "CCM_WECHAT_WATCH_LOG_PATH" in os.environ:
        return Path(os.environ["CCM_WECHAT_WATCH_LOG_PATH"]).expanduser()
    return DEFAULT_HOME_ROOT / "wechat-watch.log"


def wechat_watch_state_path() -> Path:
    if "CCM_WECHAT_WATCH_STATE_PATH" in os.environ:
        return Path(os.environ["CCM_WECHAT_WATCH_STATE_PATH"]).expanduser()
    return DEFAULT_HOME_ROOT / "wechat-watch.json"


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def load_pid_file(path: Path) -> int | None:
    if not path.exists():
        return None
    raw = path.read_text().strip()
    if not raw:
        return None
    return int(raw)


def build_tmux_session_name(name: str, cwd: str) -> str:
    return f"ccm-{sanitize_name(name)}-{namespace_suffix(cwd)}"


def candidate_projects_roots(home: Path | None = None) -> list[Path]:
    if "CCM_CLAUDE_PROJECTS_ROOT" in os.environ:
        return [Path(os.environ["CCM_CLAUDE_PROJECTS_ROOT"]).expanduser()]

    home = home or Path.home()
    roots: list[Path] = []

    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        roots.append(Path(config_dir).expanduser() / "projects")

    current_cac = home / ".cac" / "current"
    if current_cac.exists():
        current_env = current_cac.read_text().strip()
        if current_env:
            roots.append(home / ".cac" / "envs" / current_env / ".claude" / "projects")

    roots.append(home / ".claude" / "projects")

    deduped: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        resolved = root.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped


def namespace_cwd(explicit_cwd: str | None = None) -> str:
    return str(Path(explicit_cwd or os.getcwd()).resolve())


def normalize_global_args(argv: list[str] | None) -> list[str]:
    if not argv:
        return []

    front: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--json":
            front.append(arg)
            index += 1
            continue
        if arg == "--cwd":
            front.append(arg)
            if index + 1 < len(argv):
                front.append(argv[index + 1])
                index += 2
                continue
        if arg == "--state-path":
            front.append(arg)
            if index + 1 < len(argv):
                front.append(argv[index + 1])
                index += 2
                continue
        rest.append(arg)
        index += 1
    return front + rest


def resolve_state_path(cwd: str, explicit_state_path: str | None = None) -> Path:
    if explicit_state_path:
        return Path(explicit_state_path).expanduser()
    return default_state_path(cwd)


def load_state(state_path: Path | None = None) -> State:
    state_path = state_path or default_state_path()
    if not state_path.exists():
        return State()
    data = json.loads(state_path.read_text())
    sessions = {
        name: SessionRecord(**record)
        for name, record in data.get("sessions", {}).items()
    }
    return State(version=data.get("version", STATE_VERSION), sessions=sessions)


def load_wechat_transport_state(path: Path | None = None) -> WeChatTransportState | None:
    path = path or wechat_transport_state_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if "bound_alias" in data and "bound_target" not in data:
        data["bound_target"] = f"kitty:{data.pop('bound_alias')}"
    return WeChatTransportState(**data)


def save_state(state: State, state_path: Path | None = None) -> None:
    state_path = state_path or default_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": state.version,
        "sessions": {name: asdict(record) for name, record in state.sessions.items()},
    }
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def save_wechat_transport_state(state: WeChatTransportState, path: Path | None = None) -> None:
    path = path or wechat_transport_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state.saved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n")


def save_wechat_transport_state_guarded(state: WeChatTransportState, path: Path | None = None) -> None:
    path = path or wechat_transport_state_path()
    guard_wechat_transport_state(state, path)
    save_wechat_transport_state(state, path)


def clear_wechat_transport_state(path: Path | None = None) -> None:
    path = path or wechat_transport_state_path()
    if path.exists():
        path.unlink()


def save_wechat_watch_state(
    *,
    pid: int,
    status: str,
    heartbeat_at: str = "",
    last_error: str = "",
    last_poll_at: str = "",
    last_delivery_at: str = "",
    last_flush_at: str = "",
    path: Path | None = None,
) -> None:
    path = path or wechat_watch_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "status": status,
                "heartbeat_at": heartbeat_at,
                "last_error": last_error,
                "last_poll_at": last_poll_at,
                "last_delivery_at": last_delivery_at,
                "last_flush_at": last_flush_at,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def load_wechat_watch_state(path: Path | None = None) -> dict[str, Any]:
    path = path or wechat_watch_state_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def read_incremental_jsonl(path: Path, offset: int, buffer: str) -> tuple[list[dict[str, Any]], int, str]:
    if not path.exists():
        raise CCMError(f"Transcript file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        try:
            handle.seek(offset)
        except ValueError:
            handle.seek(0)
            offset = 0
            buffer = ""
        chunk = handle.read()
        new_offset = handle.tell()

    data = buffer + chunk
    if not data:
        return [], new_offset, ""

    complete_lines: list[str] = []
    next_buffer = ""
    for line in data.splitlines(keepends=True):
        if line.endswith("\n"):
            complete_lines.append(line)
        else:
            next_buffer = line

    events: list[dict[str, Any]] = []
    for line in complete_lines:
        stripped = line.strip()
        if not stripped:
            continue
        events.append(json.loads(stripped))

    return events, new_offset, next_buffer


def render_event(event: dict[str, Any], include_user: bool = False, include_thinking: bool = False) -> dict[str, str] | None:
    event_type = event.get("type")
    if event_type == "assistant":
        blocks = event.get("message", {}).get("content", [])
        text_blocks = [block.get("text", "") for block in blocks if block.get("type") == "text"]
        if text_blocks:
            return {"kind": "assistant", "text": "\n".join(part for part in text_blocks if part).strip()}
        if include_thinking:
            thoughts = [block.get("thinking", "") for block in blocks if block.get("type") == "thinking"]
            if thoughts:
                return {"kind": "assistant-thinking", "text": "\n".join(part for part in thoughts if part).strip()}
        return None

    if event_type == "system":
        subtype = event.get("subtype", "system")
        status = event.get("error", {}).get("status")
        retry_attempt = event.get("retryAttempt")
        max_retries = event.get("maxRetries")
        parts = [subtype]
        if status is not None:
            parts.append(f"status={status}")
        if retry_attempt is not None and max_retries is not None:
            parts.append(f"retry={retry_attempt}/{max_retries}")
        return {"kind": "system", "text": " ".join(parts)}

    if include_user and event_type == "user":
        text = event.get("message", {}).get("content", "")
        return {"kind": "user", "text": text}

    return None


def find_transcript_file(
    projects_root: Path,
    display_name: str,
    cwd: str,
    started_after: float,
) -> Path | None:
    if not projects_root.exists():
        return None

    best_path: Path | None = None
    best_score = -1
    for path in sorted(projects_root.rglob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.stat().st_mtime < started_after - 600:
            continue

        score = 0
        saw_title = False
        saw_cwd = False
        try:
            with path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index > 40:
                        break
                    payload = json.loads(line)
                    if payload.get("type") == "custom-title" and payload.get("customTitle") == display_name:
                        saw_title = True
                        score += 10
                    if payload.get("cwd") == cwd:
                        saw_cwd = True
                        score += 5
                    if _parse_timestamp(payload.get("timestamp")) >= started_after - 600:
                        score += 1
        except (OSError, json.JSONDecodeError):
            continue

        if saw_title and saw_cwd and score > best_score:
            best_path = path
            best_score = score

    return best_path


def pane_needs_trust_acceptance(text: str) -> bool:
    return "Yes, I trust this folder" in text and "Accessing workspace:" in text


def pane_tail_lines(text: str, limit: int = 20) -> list[str]:
    return [line.rstrip() for line in text.splitlines()[-limit:]]


def pane_has_prompt(text: str) -> bool:
    return any(line.replace("\xa0", " ").strip() == "❯" for line in pane_tail_lines(text))


def pane_has_active_work(text: str) -> bool:
    # @@@busy-pane - Claude keeps the prompt visible while still working, so readiness
    # has to exclude spinner/status rows near the bottom instead of just checking for `❯`.
    return any(
        re.match(r"^[✻✽✶·✢✳]\s+.+…(?:\s+\([^)]*\))?$", line)
        for line in pane_tail_lines(text)
    )


def pane_has_queued_messages(text: str) -> bool:
    return any("Press up to edit queued messages" in line for line in pane_tail_lines(text))


def pane_is_ready_for_input(text: str) -> bool:
    return (
        pane_has_prompt(text)
        and not pane_has_active_work(text)
        and not pane_has_queued_messages(text)
    )


def pane_excerpt(text: str, lines: int = 30) -> str:
    return "\n".join(text.splitlines()[-lines:])


def run_command(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
    )


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise CCMError(f"Missing required binary: {name}")


def ready_retry_budget(delay_seconds: float) -> int:
    timeout_seconds = float(
        os.environ.get("CCM_READY_TIMEOUT_SECONDS", str(DEFAULT_READY_TIMEOUT_SECONDS))
    )
    return max(1, int(timeout_seconds / delay_seconds))


def tmux_has_session(session_name: str) -> bool:
    result = run_command(["tmux", "has-session", "-t", session_name], check=False)
    return result.returncode == 0


def tmux_capture(session_name: str, history_lines: int = 160) -> str:
    result = run_command(["tmux", "capture-pane", "-p", "-t", session_name, "-S", f"-{history_lines}"])
    return result.stdout


def tmux_send_enter(session_name: str) -> None:
    run_command(["tmux", "send-keys", "-t", session_name, "Enter"])


def current_tmux_session_name() -> str:
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return ""
    result = run_command(
        ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def tmux_session_cwd(session_name: str) -> str:
    result = run_command(["tmux", "display-message", "-p", "-t", session_name, "#{pane_current_path}"])
    return result.stdout.strip()


def ensure_session_ready(
    record: SessionRecord,
    *,
    retries: int | None = None,
    delay_seconds: float = READY_DELAY_SECONDS,
) -> str:
    return ensure_tmux_session_ready(
        record.tmux_session,
        retries=retries,
        delay_seconds=delay_seconds,
    )


def ensure_tmux_session_ready(
    session_name: str,
    *,
    retries: int | None = None,
    delay_seconds: float = READY_DELAY_SECONDS,
) -> str:
    retries = ready_retry_budget(delay_seconds) if retries is None else retries
    last_pane = ""
    ready_streak = 0
    for _ in range(retries):
        last_pane = tmux_capture(session_name)
        if pane_needs_trust_acceptance(last_pane):
            ready_streak = 0
            tmux_send_enter(session_name)
            time.sleep(delay_seconds)
            continue
        if pane_is_ready_for_input(last_pane):
            ready_streak += 1
            if ready_streak >= READY_CONFIRMATION_PASSES:
                return last_pane
        else:
            ready_streak = 0
        time.sleep(delay_seconds)
    raise CCMError(
        "Claude session did not become ready.\n\n"
        f"{pane_excerpt(last_pane)}"
    )


def tmux_paste(session_name: str, text: str) -> None:
    buffer_name = f"ccm-buffer-{sanitize_name(session_name)}-{int(time.time() * 1000)}"
    run_command(["tmux", "load-buffer", "-b", buffer_name, "-"], input_text=text)
    try:
        run_command(["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", session_name])
    finally:
        run_command(["tmux", "delete-buffer", "-b", buffer_name], check=False)


def resolve_transcript(record: SessionRecord) -> Path | None:
    if record.transcript_path:
        path = Path(record.transcript_path)
        if path.exists():
            return path
    for projects_root in candidate_projects_roots():
        match = find_transcript_file(
            projects_root=projects_root,
            display_name=record.display_name,
            cwd=record.cwd,
            started_after=record.started_at,
        )
        if match is not None:
            return match
    return None


def describe_transcript_search(record: SessionRecord) -> dict[str, Any]:
    roots = [str(root) for root in candidate_projects_roots()]
    return {
        "display_name": record.display_name,
        "cwd": record.cwd,
        "started_after": record.started_at,
        "projects_roots": roots,
        "existing_transcript_path": record.transcript_path or "",
    }


def format_transcript_search_failure(record: SessionRecord) -> str:
    payload = describe_transcript_search(record)
    roots = ", ".join(payload["projects_roots"]) if payload["projects_roots"] else "(none)"
    return (
        f"No transcript resolved yet for session {record.name}. Send a prompt first.\n"
        f"Transcript search roots: {roots}\n"
        f"Display name: {payload['display_name']}\n"
        f"CWD: {payload['cwd']}"
    )


def session_status(record: SessionRecord) -> str:
    return "running" if tmux_has_session(record.tmux_session) else "dead"


def git_stdout(cwd: str, *args: str) -> str:
    result = run_command(["git", "-C", cwd, *args], check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_repo_root(cwd: str) -> str:
    return git_stdout(cwd, "rev-parse", "--show-toplevel")


def git_branch(cwd: str) -> str:
    return git_stdout(cwd, "rev-parse", "--abbrev-ref", "HEAD")


def canonical_session(records: list[SessionRecord]) -> SessionRecord | None:
    if not records:
        return None
    live = [record for record in records if session_status(record) == "running"]
    pool = live or records
    return max(pool, key=lambda record: record.started_at)


def workspace_identity(cwd: str) -> dict[str, str]:
    if not cwd:
        return {
            "worktree": "",
            "repo_root": "",
            "branch": "",
            "agent": "",
            "agent_status": "",
            "agent_tmux_session": "",
            "agent_transcript": "",
        }

    resolved_cwd = namespace_cwd(cwd)
    repo_root = git_repo_root(resolved_cwd) or resolved_cwd
    branch = git_branch(resolved_cwd)
    state = load_state(default_state_path(resolved_cwd))
    session = canonical_session(list(state.sessions.values()))
    transcript = ""
    if session is not None:
        resolved_transcript = resolve_transcript(session)
        transcript = session.transcript_path or (str(resolved_transcript) if resolved_transcript else "")
    return {
        "worktree": resolved_cwd,
        "repo_root": repo_root,
        "branch": branch,
        "agent": session.name if session is not None else "",
        "agent_status": session_status(session) if session is not None else "",
        "agent_tmux_session": session.tmux_session if session is not None else "",
        "agent_transcript": transcript,
    }


def inspect_session(state: State, name: str, state_path: Path) -> dict[str, Any]:
    record = state.sessions.get(name)
    if record is None:
        raise CCMError(f"Unknown managed session: {name}")
    transcript = resolve_transcript(record)
    pane_tail = ""
    if tmux_has_session(record.tmux_session):
        pane_tail = pane_excerpt(tmux_capture(record.tmux_session))
    return {
        "name": record.name,
        "state_path": str(state_path),
        "cwd": record.cwd,
        "display_name": record.display_name,
        "tmux_session": record.tmux_session,
        "status": session_status(record),
        "transcript_path": record.transcript_path or (str(transcript) if transcript else ""),
        "transcript_search": describe_transcript_search(record),
        "pane_tail": pane_tail,
    }


def current_cac_name(home: Path | None = None) -> str:
    home = home or Path.home()
    current = home / ".cac" / "current"
    if not current.exists():
        return ""
    return current.read_text().strip()


def current_cac_claude_details(home: Path | None = None) -> dict[str, str]:
    home = home or Path.home()
    env_name = current_cac_name(home)
    if not env_name:
        return {}
    env_dir = home / ".cac" / "envs" / env_name
    version = ""
    version_file = env_dir / "version"
    if version_file.exists():
        version = version_file.read_text().strip()
    actual_path = ""
    if version:
        candidate = home / ".cac" / "versions" / version / "claude"
        if candidate.exists():
            actual_path = str(candidate)
    if not actual_path:
        real = home / ".cac" / "real_claude"
        if real.exists():
            actual_path = real.read_text().strip()
    return {
        "env_name": env_name,
        "actual_path": actual_path,
        "version": version,
        "config_dir": str(env_dir / ".claude"),
    }


def claude_version_from_binary(path: str) -> str:
    if not path:
        return ""
    result = run_command([path, "--version"], check=False)
    return result.stdout.strip() or result.stderr.strip()


def command_probe(*, env: dict[str, str] | None = None) -> dict[str, str]:
    target_env = env or os.environ
    path = shutil.which("claude", path=target_env.get("PATH")) or ""
    version = ""
    actual_path = path
    config_dir = target_env.get("CLAUDE_CONFIG_DIR", "")
    cac_details = current_cac_claude_details()
    if path == str(Path.home() / ".cac" / "bin" / "claude") and cac_details:
        actual_path = cac_details.get("actual_path", path)
        config_dir = config_dir or cac_details.get("config_dir", "")
        if cac_details.get("version"):
            version = f"{cac_details['version']} (Claude Code via CAC)"
    if not version and actual_path:
        version = claude_version_from_binary(actual_path)
    return {
        "claude_path": path,
        "actual_claude_path": actual_path,
        "claude_version": version,
        "claude_config_dir": config_dir,
    }


def tmux_global_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    result = run_command(["tmux", "show-environment", "-g"], check=False)
    if result.returncode != 0:
        return env
    for line in result.stdout.splitlines():
        if not line or line.startswith("-") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def doctor_report(state: State, cwd: str, state_path: Path) -> dict[str, Any]:
    endpoint = os.environ.get("KITTY_LISTEN_ON")
    shell_probe = command_probe()
    tmux_env = tmux_global_environment()
    tmux_probe = command_probe(env={**os.environ, **tmux_env})
    launch_probe = {
        "claude_path": resolve_claude_executable() if shutil.which("claude") else "",
        "claude_config_dir": launch_environment().get("CLAUDE_CONFIG_DIR", ""),
        "tmux_command": build_tmux_claude_command("frontend-agent") if shutil.which("claude") else "",
    }
    warnings: list[str] = []
    if tmux_probe["claude_path"] and tmux_probe["claude_path"] != shell_probe["claude_path"]:
        warnings.append(
            "@@@claude-path-mismatch - tmux global PATH resolves a different claude binary than the current shell."
        )
    if tmux_probe["claude_version"] and tmux_probe["claude_version"] != shell_probe["claude_version"]:
        warnings.append(
            "@@@claude-version-mismatch - tmux global PATH resolves a different claude version than the current shell."
        )
    if not tmux_probe["claude_config_dir"] and (Path.home() / ".cac" / "current").exists():
        warnings.append(
            "@@@missing-config-root - tmux global environment does not export CLAUDE_CONFIG_DIR, so stale launchers can fall back to ~/.claude."
        )
    return {
        "cwd": cwd,
        "state_path": str(state_path),
        "state_exists": state_path.exists(),
        "sessions": sorted(state.sessions),
        "kitty_listen_on": endpoint or "",
        "binaries": {
            "tmux": shutil.which("tmux") is not None,
            "claude": shutil.which("claude") is not None,
            "kitty": shutil.which("kitty") is not None,
        },
        "shell": shell_probe,
        "tmux_global": tmux_probe,
        "launch": launch_probe,
        "warnings": warnings,
    }


def start_session(state: State, name: str, cwd: str) -> SessionRecord:
    require_binary("tmux")
    require_binary("claude")

    if name in state.sessions:
        raise CCMError(f"Managed session already exists: {name}")

    tmux_session = build_tmux_session_name(name, cwd)
    if tmux_has_session(tmux_session):
        raise CCMError(f"tmux session already exists: {tmux_session}")

    command = build_tmux_claude_command(name)
    run_command(["tmux", "new-session", "-d", "-s", tmux_session, "-c", cwd, command])

    record = SessionRecord(
        name=name,
        tmux_session=tmux_session,
        display_name=name,
        cwd=str(Path(cwd).resolve()),
        started_at=time.time(),
    )

    try:
        time.sleep(4)
        ensure_session_ready(record)
    except Exception:
        run_command(["tmux", "kill-session", "-t", tmux_session], check=False)
        raise

    state.sessions[name] = record
    return record


def send_prompt(state: State, name: str, prompt: str) -> SessionRecord:
    if name not in state.sessions:
        raise CCMError(f"Unknown managed session: {name}")
    record = state.sessions[name]
    if not tmux_has_session(record.tmux_session):
        raise CCMError(f"tmux session is not running: {record.tmux_session}")

    ensure_session_ready(record)
    tmux_paste(record.tmux_session, prompt)
    time.sleep(TMUX_PASTE_SUBMIT_DELAY_SECONDS)
    tmux_send_enter(record.tmux_session)
    time.sleep(1)

    transcript = resolve_transcript(record)
    if transcript is not None:
        record.transcript_path = str(transcript)

    return record


def read_updates(
    state: State,
    name: str,
    *,
    include_user: bool = False,
    include_thinking: bool = False,
    wait_seconds: float = 0.0,
    poll_interval: float = 2.0,
    raw: bool = False,
) -> list[dict[str, Any]]:
    if name not in state.sessions:
        raise CCMError(f"Unknown managed session: {name}")
    record = state.sessions[name]
    deadline = time.time() + max(wait_seconds, 0.0)

    while True:
        transcript = resolve_transcript(record)
        if transcript is None:
            if time.time() >= deadline:
                raise CCMError(format_transcript_search_failure(record))
            time.sleep(poll_interval)
            continue

        record.transcript_path = str(transcript)
        events, next_offset, next_buffer = read_incremental_jsonl(
            transcript,
            record.transcript_offset,
            record.transcript_buffer,
        )
        record.transcript_offset = next_offset
        record.transcript_buffer = next_buffer

        if raw:
            if events or time.time() >= deadline:
                return events
            time.sleep(poll_interval)
            continue

        rendered = []
        for event in events:
            item = render_event(event, include_user=include_user, include_thinking=include_thinking)
            if item is not None:
                rendered.append(item)

        if rendered or time.time() >= deadline:
            return rendered
        time.sleep(poll_interval)


def kill_sessions(state: State, names: list[str]) -> list[str]:
    killed: list[str] = []
    for name in names:
        record = state.sessions.get(name)
        if record is None:
            raise CCMError(f"Unknown managed session: {name}")
        run_command(["tmux", "kill-session", "-t", record.tmux_session], check=False)
        killed.append(name)
        del state.sessions[name]
    return killed


def cleanup_sessions(state: State, kill_live: bool = False) -> dict[str, list[str]]:
    removed_dead: list[str] = []
    killed_live: list[str] = []
    for name, record in list(state.sessions.items()):
        is_live = tmux_has_session(record.tmux_session)
        if is_live and kill_live:
            run_command(["tmux", "kill-session", "-t", record.tmux_session], check=False)
            killed_live.append(name)
            del state.sessions[name]
            continue
        if not is_live:
            removed_dead.append(name)
            del state.sessions[name]
    return {"removed_dead": removed_dead, "killed_live": killed_live}


def open_in_kitty(state: State, name: str, listen_on: str | None) -> dict[str, str]:
    if name not in state.sessions:
        raise CCMError(f"Unknown managed session: {name}")
    record = state.sessions[name]
    if not tmux_has_session(record.tmux_session):
        raise CCMError(f"tmux session is not running: {record.tmux_session}")

    require_binary("kitty")
    endpoint = listen_on or os.environ.get("KITTY_LISTEN_ON")
    if not endpoint:
        raise CCMError("KITTY_LISTEN_ON is required for open")

    title = f"[ccm:{name}]"
    command = f"tmux attach-session -t {shlex.quote(record.tmux_session)}"
    result = run_command(
        [
            "kitty",
            "@",
            "--to",
            endpoint,
            "launch",
            "--type=tab",
            "--tab-title",
            title,
            "--cwd",
            record.cwd,
            "zsh",
            "-lic",
            command,
        ]
    )
    return {"title": title, "endpoint": endpoint, "raw": result.stdout.strip()}


def resolve_kitty_endpoint(listen_on: str | None) -> str:
    endpoint = listen_on or os.environ.get("KITTY_LISTEN_ON")
    if not endpoint:
        raise CCMError("KITTY_LISTEN_ON is required")
    return endpoint


def kitty_window_worktree(window: dict[str, Any]) -> str:
    env_pwd = str(window.get("env", {}).get("PWD", "")).strip()
    if env_pwd:
        # @@@kitty-pwd-source - kitty's `cwd` can lag behind for long-lived Codex shells,
        # while `env.PWD` reflects the worktree the tab was launched for. Prefer that
        # identity signal so cross-tab routing follows the visible branch/worktree.
        return env_pwd
    return str(window.get("cwd", ""))


def list_kitty_tabs(listen_on: str | None) -> list[dict[str, str]]:
    require_binary("kitty")
    endpoint = resolve_kitty_endpoint(listen_on)
    result = run_command(["kitty", "@", "--to", endpoint, "ls"])
    payload = json.loads(result.stdout)

    tabs: list[dict[str, str]] = []
    for os_window in payload:
        for tab in os_window.get("tabs", []):
            windows = tab.get("windows", [])
            if not windows:
                continue
            active_window = next((window for window in windows if window.get("is_active")), windows[0])
            active_cwd = kitty_window_worktree(active_window)
            identity = workspace_identity(active_cwd)
            tabs.append(
                {
                    "title": str(tab.get("title", "")),
                    "window_id": str(active_window["id"]),
                    "cwd": active_cwd,
                    "cmdline": " ".join(active_window.get("cmdline", [])),
                    "branch": identity["branch"],
                    "repo_root": identity["repo_root"],
                    "agent": identity["agent"],
                    "agent_status": identity["agent_status"],
                    "agent_tmux_session": identity["agent_tmux_session"],
                    "agent_transcript": identity["agent_transcript"],
                }
            )
    return tabs


def send_message_to_kitty_tab(title: str, message: str, listen_on: str | None) -> dict[str, str]:
    endpoint = resolve_kitty_endpoint(listen_on)
    matches = [tab for tab in list_kitty_tabs(endpoint) if tab["title"] == title]
    if not matches:
        raise CCMError(f"No kitty tab found with title: {title}")
    if len(matches) > 1:
        raise CCMError(f"Multiple kitty tabs found with title: {title}")

    tab = matches[0]
    run_command(
        [
            "kitty",
            "@",
            "--to",
            endpoint,
            "send-text",
            "--match",
            f"id:{tab['window_id']}",
            message,
        ]
    )
    run_command(
        [
            "kitty",
            "@",
            "--to",
            endpoint,
            "send-key",
            "--match",
            f"id:{tab['window_id']}",
            "enter",
        ]
    )
    return {"title": tab["title"], "window_id": tab["window_id"], "endpoint": endpoint}


def send_message_to_kitty_window(window_id: str, message: str, listen_on: str | None) -> dict[str, str]:
    endpoint = resolve_kitty_endpoint(listen_on)
    run_command(
        [
            "kitty",
            "@",
            "--to",
            endpoint,
            "send-text",
            "--match",
            f"id:{window_id}",
            message,
        ]
    )
    run_command(
        [
            "kitty",
            "@",
            "--to",
            endpoint,
            "send-key",
            "--match",
            f"id:{window_id}",
            "enter",
        ]
    )
    return {"window_id": window_id, "endpoint": endpoint}


def deliver_message_to_target(target: WeChatTargetRecord, message: str, listen_on: str | None) -> dict[str, str]:
    if target.runtime == "claude" and target.tmux_session:
        ensure_tmux_session_ready(target.tmux_session)
    if not target.window_id:
        if not target.tmux_session:
            raise CCMError(f"WeChat target {target.target} has no visible kitty window or tmux session")
        # @@@headless-peer-delivery - headless claude/tmux peers must remain reachable
        # even when no visible kitty tab exists, otherwise phone routing and peer handoff
        # quietly depend on the UI layer. Claude can also drop an immediate Enter
        # after a large paste, so the submit key waits a beat for the paste to land.
        tmux_paste(target.tmux_session, message)
        time.sleep(TMUX_PASTE_SUBMIT_DELAY_SECONDS)
        tmux_send_enter(target.tmux_session)
        return {"window_id": "", "tmux_session": target.tmux_session}
    payload = send_message_to_kitty_window(target.window_id, message, listen_on)
    if target.runtime == "claude" and target.tmux_session:
        tmux_send_enter(target.tmux_session)
    return payload


def resolve_current_sender_context(cwd: str, listen_on: str | None) -> dict[str, str]:
    resolved_cwd = namespace_cwd(cwd)
    context = workspace_identity(resolved_cwd)
    context["title"] = context["branch"] or Path(resolved_cwd).name or "unknown"
    context["window_id"] = ""
    context["cmdline"] = ""
    current_window_id = os.environ.get("KITTY_WINDOW_ID")
    if not current_window_id:
        return context

    for tab in list_kitty_tabs(listen_on):
        if tab["window_id"] == current_window_id:
            context["title"] = tab["title"] or context["title"]
            context["window_id"] = tab["window_id"]
            context["cmdline"] = tab["cmdline"]
            break
    return context


def format_relay_message(
    message: str,
    sender: dict[str, str],
    *,
    task: str = "",
    scene: str = "",
    ports: str = "",
) -> str:
    reply_target = sender.get("title", "unknown")
    fields = [
        ("from", sender.get("title", "")),
        ("worktree", sender.get("worktree", "")),
        ("branch", sender.get("branch", "")),
        ("repo", sender.get("repo_root", "")),
        ("task", task or sender.get("branch", "") or sender.get("title", "")),
        ("agent", sender.get("agent", "")),
        ("tmux", sender.get("agent_tmux_session", "")),
        ("transcript", sender.get("agent_transcript", "")),
        ("ports", ports),
        ("scene", scene),
        ("reply-via", f'ccm relay {shlex.quote(reply_target)} "..."'),
    ]
    rendered = " | ".join(f"{key}: {value}" for key, value in fields if value)
    return f"[{rendered}] {message}"


def relay_message_to_kitty_tab(
    title: str,
    message: str,
    listen_on: str | None,
    *,
    cwd: str,
    task: str = "",
    scene: str = "",
    ports: str = "",
) -> dict[str, str]:
    sender = resolve_current_sender_context(cwd, listen_on)
    relay_message = format_relay_message(
        message,
        sender,
        task=task,
        scene=scene,
        ports=ports,
    )
    payload = send_message_to_kitty_tab(title, relay_message, listen_on)
    payload["from"] = sender["title"]
    payload["reply_via"] = f'ccm relay {shlex.quote(sender["title"])} "..."'
    return payload


def normalize_base_url(base_url: str) -> str:
    normalized = base_url.strip()
    if not normalized:
        raise CCMError("Base URL cannot be empty")
    return normalized.rstrip("/")


def _random_wechat_uin() -> str:
    return hashlib.sha1(os.urandom(16)).hexdigest()[:12]


def wechat_headers(token: str = "", *, body: str = "") -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
    }
    if body:
        headers["Content-Length"] = str(len(body.encode("utf-8")))
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def wechat_http_json(
    method: str,
    url: str,
    *,
    token: str = "",
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
    timeout_returns_wait: bool = False,
) -> dict[str, Any]:
    raw_body = "" if body is None else json.dumps(body)
    encoded_body = None if body is None else raw_body.encode("utf-8")
    headers = wechat_headers(token, body=raw_body)
    request = urllib.request.Request(url, data=encoded_body, headers=headers, method=method.upper())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except TimeoutError as exc:
        if timeout_returns_wait:
            return {"status": "wait"}
        raise CCMError(f"WeChat transport timed out after {timeout:.1f}s") from exc
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise CCMError(f"WeChat transport HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        if timeout_returns_wait and "timed out" in str(exc.reason).lower():
            return {"status": "wait"}
        raise CCMError(f"Failed to reach WeChat transport endpoint: {exc.reason}") from exc
    try:
        return json.loads(payload) if payload else {}
    except json.JSONDecodeError as exc:
        raise CCMError(f"Invalid JSON from WeChat transport: {payload[:200]}") from exc


def render_qr_png(content: str, output_path: Path | None = None) -> Path:
    output_path = output_path or wechat_qr_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    script = textwrap.dedent(
        f"""
        import Foundation
        import CoreImage
        import AppKit

        let content = {json.dumps(content)}
        let outputPath = {json.dumps(str(output_path))}
        guard let data = content.data(using: .utf8) else {{
            fputs("Failed to encode QR content\\n", stderr)
            Foundation.exit(1)
        }}
        let filter = CIFilter(name: "CIQRCodeGenerator")!
        filter.setValue(data, forKey: "inputMessage")
        filter.setValue("M", forKey: "inputCorrectionLevel")
        guard let image = filter.outputImage else {{
            fputs("CIQRCodeGenerator returned no output\\n", stderr)
            Foundation.exit(1)
        }}
        let scaled = image.transformed(by: CGAffineTransform(scaleX: 12, y: 12))
        let rep = NSCIImageRep(ciImage: scaled)
        let nsImage = NSImage(size: rep.size)
        nsImage.addRepresentation(rep)
        guard let tiff = nsImage.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiff),
              let png = bitmap.representation(using: .png, properties: [:]) else {{
            fputs("Failed to convert QR image to PNG\\n", stderr)
            Foundation.exit(1)
        }}
        try png.write(to: URL(fileURLWithPath: outputPath))
        print(outputPath)
        """
    ).strip()
    # @@@macos-qr-render - backend returns a WeChat URL, not a ready-made bitmap.
    # CoreImage keeps the CLI dependency-light while still producing a real scannable code.
    result = subprocess.run(
        ["swift", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CCMError(f"Failed to render QR code: {(result.stderr or result.stdout).strip()}")
    if not output_path.exists():
        raise CCMError(f"QR render reported success but file is missing: {output_path}")
    return output_path


def open_qr_preview(path: Path) -> None:
    result = subprocess.run(["open", str(path)], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise CCMError(f"Failed to open QR preview: {(result.stderr or result.stdout).strip()}")


def require_wechat_transport_state(state: WeChatTransportState | None) -> WeChatTransportState:
    if state is None or not state.token:
        raise CCMError("No saved WeChat transport state. Run 'ccm wechat-connect' first.")
    return state


def wechat_status_payload(state: WeChatTransportState | None) -> dict[str, Any]:
    if state is None or not state.token:
        return {"connected": False}
    return {
        "connected": True,
        "base_url": state.base_url,
        "account_id": state.account_id or "-",
        "user_id": state.user_id or "-",
        "contact_count": len(state.context_tokens),
        "bound_target": state.bound_target or "-",
        "saved_at": state.saved_at or "-",
    }


def guard_wechat_transport_state(state: WeChatTransportState, state_path: Path | None) -> None:
    persisted = load_wechat_transport_state(state_path)
    if persisted is None:
        return
    if persisted.token and persisted.token != state.token:
        raise CCMError(
            "@@@wechat-transport-replaced - on-disk WeChat transport was replaced by a newer login; "
            "stop this watcher and restart it against the new connection."
        )
    if persisted.bound_target != state.bound_target:
        state.bound_target = persisted.bound_target


def wechat_connect(
    *,
    state_path: Path | None = None,
    open_preview: bool,
    poll_interval: float,
    wait_seconds: float,
    qrcode: str | None = None,
) -> dict[str, Any]:
    qr_content = ""
    qr_path: Path | None = None
    if qrcode:
        qrcode = str(qrcode)
    else:
        qr_payload = wechat_http_json(
            "GET",
            f"{WECHAT_DEFAULT_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type={WECHAT_BOT_TYPE}",
            timeout=10.0,
        )
        qrcode = str(qr_payload.get("qrcode", ""))
        qr_content = str(qr_payload.get("qrcode_img_content", ""))
        if not qrcode or not qr_content:
            raise CCMError(f"Unexpected WeChat QR response: {qr_payload}")
        qr_path = render_qr_png(qr_content)
        if open_preview:
            open_qr_preview(qr_path)
    deadline = time.time() + wait_seconds if wait_seconds > 0 else None
    history: list[str] = []
    last_status = "wait"
    account_id = ""
    while True:
        remaining_wait = None if deadline is None else max(0.0, deadline - time.time())
        request_timeout = WECHAT_LONG_POLL_TIMEOUT_SECONDS + 5.0
        if remaining_wait is not None:
            request_timeout = min(request_timeout, remaining_wait + 1.0)
        request_timeout = max(request_timeout, poll_interval + 1.0, 1.5)
        poll_payload = wechat_http_json(
            "GET",
            f"{WECHAT_DEFAULT_BASE_URL}/ilink/bot/get_qrcode_status?qrcode={qrcode}",
            timeout=request_timeout,
            timeout_returns_wait=True,
        )
        status = str(poll_payload.get("status", "wait"))
        last_status = status
        if not history or history[-1] != status:
            history.append(status)
        if status == "confirmed":
            token = str(poll_payload.get("bot_token", ""))
            account_id = str(poll_payload.get("ilink_bot_id", ""))
            if not token or not account_id:
                raise CCMError(f"Missing WeChat bot credentials in confirm response: {poll_payload}")
            save_wechat_transport_state(
                WeChatTransportState(
                    token=token,
                    base_url=str(poll_payload.get("baseurl") or WECHAT_DEFAULT_BASE_URL),
                    account_id=account_id,
                    user_id=str(poll_payload.get("ilink_user_id", "")),
                    saved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
                state_path,
            )
            break
        if status in {"expired", "error"}:
            break
        if deadline is not None and time.time() >= deadline:
            break
        if poll_interval > 0:
            time.sleep(poll_interval)
    return {
        "status": last_status,
        "account_id": account_id or str(poll_payload.get("ilink_bot_id", "")),
        "qrcode": qrcode,
        "qrcode_img_url": qr_content,
        "qr_path": str(qr_path) if qr_path is not None else "",
        "history": history,
        "opened": open_preview and qr_path is not None,
    }


def wechat_disconnect(state_path: Path | None = None) -> dict[str, Any]:
    clear_wechat_transport_state(state_path)
    return {"ok": True, "connected": False}


def wechat_bind(state: WeChatTransportState, target: str) -> WeChatTransportState:
    parse_target_spec(target)
    state.bound_target = target
    return state


def wechat_unbind(state: WeChatTransportState) -> WeChatTransportState:
    state.bound_target = ""
    return state


def wechat_users_payload(state: WeChatTransportState) -> list[dict[str, str]]:
    return [{"user_id": user_id} for user_id in sorted(state.context_tokens)]


def active_wechat_user_id(state: WeChatTransportState) -> str:
    if state.user_id and state.user_id in state.context_tokens:
        return state.user_id
    if len(state.context_tokens) == 1:
        return next(iter(state.context_tokens))
    raise CCMError("Cannot determine the active phone WeChat user for handoff notification")


def extract_wechat_text(message: dict[str, Any]) -> str:
    for item in message.get("item_list") or []:
        if item.get("type") == WECHAT_MSG_ITEM_TEXT:
            return str((item.get("text_item") or {}).get("text", ""))
    return ""


def format_incoming_wechat_prompt(
    *,
    user_id: str,
    text: str,
    bound_target: str,
    reply_command: str,
    runtime: str = "",
) -> str:
    if runtime == "claude":
        parts = [
            "Phone message for your currently bound ccm thread.",
            f"user_id: {user_id}",
            f"bound_target: {bound_target}",
            "Operator authorization: the human explicitly connected this phone thread to ccm.",
            "Replying through `ccm wechat-queue-reply` writes to the local ccm outbox first; ccm sends it afterwards.",
            f"Reply command: {reply_command}",
            'Handoff command: ccm wechat-shift <target> "..."',
            "Incoming message:",
            text,
        ]
        return "\n".join(parts)

    parts = [
        text,
        "<system-reminder>",
        "<ccm-wechat-incoming>",
        f"  <user-id>{escape(user_id)}</user-id>",
        f"  <bound-target>{escape(bound_target)}</bound-target>",
        "</ccm-wechat-incoming>",
        "Operator authorization: this phone thread was explicitly connected to ccm by the human operator.",
        "Using `ccm wechat-reply` and `ccm wechat-shift` on this thread is the intended and authorized workflow.",
        f"To reply, use {reply_command}.",
        "To hand work to another peer, use ccm wechat-shift <target> \"...\".",
        "</system-reminder>",
    ]
    return "\n".join(parts)


def wechat_get_updates(state: WeChatTransportState) -> dict[str, Any]:
    return wechat_http_json(
        "POST",
        f"{normalize_base_url(state.base_url)}/ilink/bot/getupdates",
        token=state.token,
        body={
            "get_updates_buf": state.sync_buf,
            "base_info": {"channel_version": WECHAT_CHANNEL_VERSION},
        },
        timeout=WECHAT_LONG_POLL_TIMEOUT_SECONDS + 5.0,
        timeout_returns_wait=True,
    )


def wechat_poll_once(
    state: WeChatTransportState,
    *,
    listen_on: str | None,
) -> dict[str, Any]:
    sent_replies = flush_pending_wechat_replies(state)
    payload = wechat_get_updates(state)
    if payload.get("status") == "wait":
        return {"delivered_count": 0, "messages": [], "sent_replies": sent_replies, "status": "wait"}
    if payload.get("ret", 0) != 0 or payload.get("errcode", 0) != 0:
        raise CCMError(f"WeChat getupdates failed: errcode={payload.get('errcode', 0)} {payload.get('errmsg', '')}")
    if payload.get("get_updates_buf"):
        state.sync_buf = str(payload["get_updates_buf"])
    delivered_count = 0
    messages: list[dict[str, str]] = []
    for msg in payload.get("msgs") or []:
        if msg.get("message_type") != WECHAT_MSG_TYPE_USER:
            continue
        text = extract_wechat_text(msg)
        if not text:
            continue
        user_id = str(msg.get("from_user_id", "unknown"))
        context_token = str(msg.get("context_token", ""))
        if context_token:
            state.context_tokens[user_id] = context_token
        messages.append({"user_id": user_id, "text": text})
        if not state.bound_target:
            continue
        target = resolve_target_spec(state.bound_target, listen_on=listen_on, cwd=os.getcwd())
        reply_command = f'ccm wechat-reply {shlex.quote(user_id)} "..."'
        if target.runtime == "claude":
            reply_command = f'ccm wechat-queue-reply {shlex.quote(user_id)} "..."'
        deliver_message_to_target(
            target,
            format_incoming_wechat_prompt(
                user_id=user_id,
                text=text,
                bound_target=state.bound_target,
                reply_command=reply_command,
                runtime=target.runtime,
            ),
            listen_on,
        )
        delivered_count += 1
    return {"delivered_count": delivered_count, "messages": messages, "sent_replies": sent_replies, "status": "ok"}


def wechat_reply(state: WeChatTransportState, *, user_id: str, text: str) -> dict[str, Any]:
    context_token = state.context_tokens.get(user_id)
    if not context_token:
        raise CCMError(f"No saved context token for {user_id}. The user must message the bot first.")
    client_id = f"ccm:{int(time.time())}"
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": user_id,
            "client_id": client_id,
            "message_type": WECHAT_MSG_TYPE_BOT,
            "message_state": WECHAT_MSG_STATE_FINISH,
            "item_list": [{"type": WECHAT_MSG_ITEM_TEXT, "text_item": {"text": text}}],
            "context_token": context_token,
        },
        "base_info": {"channel_version": WECHAT_CHANNEL_VERSION},
    }
    wechat_http_json(
        "POST",
        f"{normalize_base_url(state.base_url)}/ilink/bot/sendmessage",
        token=state.token,
        body=body,
        timeout=WECHAT_SEND_TIMEOUT_SECONDS,
    )
    return {"ok": True, "user_id": user_id, "client_id": client_id}


def wechat_queue_reply(state: WeChatTransportState, *, user_id: str, text: str) -> dict[str, Any]:
    if user_id not in state.context_tokens:
        raise CCMError(f"No saved context token for {user_id}. The user must message the bot first.")
    item = {"user_id": user_id, "text": text}
    state.pending_replies.append(item)
    return {"queued": True, "user_id": user_id, "pending_count": len(state.pending_replies)}


def queue_and_flush_wechat_reply(state: WeChatTransportState, *, user_id: str, text: str) -> dict[str, Any]:
    payload = wechat_queue_reply(state, user_id=user_id, text=text)
    sent = flush_pending_wechat_replies(state)
    payload["sent_count"] = len(sent)
    return payload


def flush_pending_wechat_replies(state: WeChatTransportState) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    while state.pending_replies:
        item = state.pending_replies[0]
        payload = wechat_reply(state, user_id=item["user_id"], text=item["text"])
        sent.append(payload)
        del state.pending_replies[0]
    return sent


def launch_wechat_watch_daemon(*, listen_on: str | None, poll_interval: float) -> dict[str, Any]:
    pid_path = wechat_watch_pid_path()
    log_path = wechat_watch_log_path()
    state_path = wechat_watch_state_path()
    existing_pid = load_pid_file(pid_path)
    if existing_pid and pid_is_running(existing_pid):
        raise CCMError(f"WeChat watch is already running with pid {existing_pid}")

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), "wechat-watch", "--poll-interval", str(poll_interval)]
    if listen_on:
        command.extend(["--listen-on", listen_on])

    with log_path.open("ab") as log_file:
        # @@@wechat-watch-daemon - the background watcher must survive the parent shell
        # and keep using the same installed manager code, so it launches a detached
        # Python process directly against this module file and records its pid.
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_path.write_text(f"{process.pid}\n")
    save_wechat_watch_state(
        pid=process.pid,
        status="starting",
        heartbeat_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        path=state_path,
    )
    return {
        "started": True,
        "pid": process.pid,
        "pid_path": str(pid_path),
        "log_path": str(log_path),
        "state_path": str(state_path),
    }


def wechat_watch_status() -> dict[str, Any]:
    pid_path = wechat_watch_pid_path()
    log_path = wechat_watch_log_path()
    state_path = wechat_watch_state_path()
    pid = load_pid_file(pid_path)
    state = load_wechat_watch_state(state_path)
    running = bool(pid and pid_is_running(pid))
    return {
        "running": running,
        "pid": pid or 0,
        "pid_path": str(pid_path),
        "log_path": str(log_path),
        "state_path": str(state_path),
        "watch_status": state.get("status", ""),
        "heartbeat_at": state.get("heartbeat_at", ""),
        "last_error": state.get("last_error", ""),
        "last_poll_at": state.get("last_poll_at", ""),
        "last_delivery_at": state.get("last_delivery_at", ""),
        "last_flush_at": state.get("last_flush_at", ""),
    }


def wechat_watch_stop() -> dict[str, Any]:
    pid_path = wechat_watch_pid_path()
    state_path = wechat_watch_state_path()
    pid = load_pid_file(pid_path)
    if not pid:
        return {"stopped": False, "reason": "not-running", "pid": 0}
    if pid_is_running(pid):
        os.kill(pid, signal.SIGTERM)
    pid_path.unlink(missing_ok=True)
    save_wechat_watch_state(pid=pid, status="stopped", path=state_path)
    return {"stopped": True, "pid": pid}


def infer_runtime_label(cmdline: str, agent: str, tmux_session: str) -> str:
    lowered = (cmdline or "").lower()
    if "codex" in lowered:
        return "codex"
    if "claude" in lowered or agent or tmux_session.startswith("ccm-"):
        return "claude"
    if tmux_session:
        return "tmux"
    return "kitty"


def parse_target_spec(value: str) -> WeChatTargetSpec:
    if ":" not in value:
        raise CCMError("Invalid target. Use 'kitty:<tab-title>' or 'tmux:<session-name>'.")
    kind, raw = value.split(":", 1)
    kind = kind.strip().lower()
    raw = raw.strip()
    if kind not in {"kitty", "tmux"} or not raw:
        raise CCMError("Invalid target. Use 'kitty:<tab-title>' or 'tmux:<session-name>'.")
    return WeChatTargetSpec(kind=kind, value=raw)


def resolve_target_spec(target: str, *, listen_on: str | None, cwd: str) -> WeChatTargetRecord:
    spec = parse_target_spec(target)
    if spec.kind == "kitty":
        matches = [tab for tab in list_kitty_tabs(listen_on) if tab["title"] == spec.value]
        if not matches:
            raise CCMError(f"No kitty tab found with title: {spec.value}")
        if len(matches) > 1:
            raise CCMError(f"Multiple kitty tabs found with title: {spec.value}")
        tab = matches[0]
        return WeChatTargetRecord(
            target=target,
            kind="kitty",
            title=tab["title"],
            window_id=tab["window_id"],
            worktree=tab["cwd"],
            repo_root=tab["repo_root"],
            branch=tab["branch"],
            tmux_session=tab["agent_tmux_session"],
            agent=tab["agent"],
            agent_status=tab["agent_status"],
            agent_transcript=tab["agent_transcript"],
            runtime=infer_runtime_label(tab["cmdline"], tab["agent"], tab["agent_tmux_session"]),
        )

    session_name = spec.value
    if not tmux_has_session(session_name):
        raise CCMError(f"tmux session is not live: {session_name}")
    session_cwd = tmux_session_cwd(session_name)
    identity = workspace_identity(session_cwd or namespace_cwd(cwd))
    return WeChatTargetRecord(
        target=target,
        kind="tmux",
        title=session_name,
        window_id="",
        worktree=session_cwd or namespace_cwd(cwd),
        repo_root=identity["repo_root"],
        branch=identity["branch"],
        tmux_session=session_name,
        agent=identity["agent"],
        agent_status=identity["agent_status"],
        agent_transcript=identity["agent_transcript"],
        runtime=infer_runtime_label("", identity["agent"], session_name),
    )


def resolve_sender_target(
    *,
    cwd: str,
    listen_on: str | None,
    explicit_target: str = "",
) -> WeChatTargetRecord:
    if explicit_target:
        return resolve_target_spec(explicit_target, listen_on=listen_on, cwd=cwd)

    current_tmux = current_tmux_session_name()
    if current_tmux:
        return resolve_target_spec(f"tmux:{current_tmux}", listen_on=listen_on, cwd=cwd)

    current = resolve_current_sender_context(cwd, listen_on)
    title = current.get("title", "").strip()
    if title:
        return resolve_target_spec(f"kitty:{title}", listen_on=listen_on, cwd=cwd)
    raise CCMError("Cannot resolve current sender target. Use --from-target explicitly.")


def format_wechat_prompt(
    message: str,
    sender: WeChatTargetRecord,
    *,
    mode: str,
    task: str = "",
    scene: str = "",
    compact: bool = False,
) -> str:
    parts = [
        f"{message}",
        "<system-reminder>",
        "<ccm-wechat-message>",
        f"  <mode>{escape(mode)}</mode>",
        f"  <from-target>{escape(sender.target)}</from-target>",
        f"  <title>{escape(sender.title)}</title>",
        f"  <worktree>{escape(sender.worktree)}</worktree>",
        f"  <branch>{escape(sender.branch)}</branch>",
        f"  <repo>{escape(sender.repo_root)}</repo>",
    ]
    if sender.tmux_session:
        parts.append(f"  <tmux-session>{escape(sender.tmux_session)}</tmux-session>")
    if sender.agent:
        parts.append(f"  <agent>{escape(sender.agent)}</agent>")
    if sender.agent_transcript:
        parts.append(f"  <transcript>{escape(sender.agent_transcript)}</transcript>")
    if task:
        parts.append(f"  <task>{escape(task)}</task>")
    if scene:
        parts.append(f"  <scene>{escape(scene)}</scene>")
    parts.extend(
        [
            "</ccm-wechat-message>",
            "Operator authorization: the human operator explicitly requested this ccm wechat handoff/message flow.",
            "Using `ccm wechat-send`, `ccm wechat-shift`, and, when instructed by the phone thread, `ccm wechat-reply` is authorized here.",
            f'To reply, use ccm wechat-send {shlex.quote(sender.target)} "...".',
            f'To hand off, use ccm wechat-shift {shlex.quote(sender.target)} "...".',
            "</system-reminder>",
        ]
    )
    separator = " " if compact else "\n"
    return separator.join(parts)


def wechat_targets_payload(*, cwd: str, listen_on: str | None, all_scopes: bool = False) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for tab in list_kitty_tabs(listen_on):
        payload.append(
            {
                "target": f"kitty:{tab['title']}",
                "kind": "kitty",
                "title": tab["title"],
                "runtime": infer_runtime_label(tab["cmdline"], tab["agent"], tab["agent_tmux_session"]),
                "worktree": tab["cwd"],
                "branch": tab["branch"],
                "tmux_session": tab["agent_tmux_session"],
            }
        )
    session_records: list[SessionRecord] = []
    if all_scopes:
        for item in list_sessions_all_scopes(DEFAULT_HOME_ROOT):
            session_records.append(
                SessionRecord(
                    name=item["name"],
                    tmux_session=item["tmux_session"],
                    display_name=item["name"],
                    cwd=item["cwd"],
                    started_at=0.0,
                    transcript_path=item.get("transcript", "") or None,
                )
            )
    else:
        session_records.extend(load_state(resolve_state_path(namespace_cwd(cwd))).sessions.values())
    for record in session_records:
        if not tmux_has_session(record.tmux_session):
            continue
        identity = workspace_identity(record.cwd)
        payload.append(
            {
                "target": f"tmux:{record.tmux_session}",
                "kind": "tmux",
                "title": record.display_name,
                "runtime": infer_runtime_label("", identity["agent"], record.tmux_session),
                "worktree": record.cwd,
                "branch": identity["branch"],
                "tmux_session": record.tmux_session,
            }
        )
    return payload


def render_wechat_guide(audience: str) -> str:
    if audience == "human":
        return textwrap.dedent(
            """
            CCM wechat guide

            This project uses two different things called "wechat":
            1. Real phone WeChat onboarding through ccm's direct iLink transport
            2. ccm's wechat-style peer layer for direct target-based handoff

            For phone onboarding:
            - ccm wechat-connect
            - Scan the QR code with your phone
            - ccm wechat-bind kitty:<tab-title>   (or tmux:<session-name> for a headless agent)
            - ccm wechat-watch --detach
            - ccm wechat-watch-status
            - ccm wechat-reply <user_id> "..."

            Handoff rule:
            - `ccm wechat-shift <target> "..."` is not just a note. If the sender currently owns the phone thread, shift also moves phone ownership to the target.

            For peer coordination after that:
            - ccm wechat-targets
            - ccm wechat-send kitty:<tab-title> "..."
            - ccm wechat-send tmux:<session-name> "..."
            """
        ).strip()

    if audience != "agent":
        raise CCMError(f"Unsupported wechat guide audience: {audience}")

    return textwrap.dedent(
        """
        CCM wechat guide for agents

        There are two layers here:
        - Phone WeChat onboarding now has a real direct ccm transport path.
        - ccm's wechat-style peer layer handles direct target-based messaging and handoff.

        If the user says "connect WeChat to you so I can message you from my phone", do this:
        1. Run `ccm wechat-connect` and let it render/open a QR code.
        2. Tell them to scan the QR code with their phone.
        3. Choose one direct target:
           - visible tab: `kitty:<tab-title>`
           - headless agent: `tmux:<session-name>`
        4. Run `ccm wechat-bind <target>`.
        5. Run `ccm wechat-watch --detach`.
        6. Check `ccm wechat-watch-status`.
        7. If the user scans again later, run `ccm wechat-bind <target>` again after the new `ccm wechat-connect`.

        Phone-layer commands:
        - `ccm wechat-connect` requests a real WeChat QR code and polls until confirmed.
        - `ccm wechat-status` shows the global transport state.
        - `ccm wechat-bind <target>` binds incoming phone messages to one direct target.
        - `ccm wechat-unbind` clears that binding.
        - `ccm wechat-users` lists known phone users who have messaged the bot.
        - `ccm wechat-reply <user_id> "..."` replies to a phone user.
        - `ccm wechat-poll-once` fetches and delivers one update batch for debugging or one-shot delivery.
        - `ccm wechat-watch --detach` starts the canonical background watcher managed by ccm itself.
        - `ccm wechat-watch-status` shows whether that watcher is still alive.
        - `ccm wechat-watch-stop` stops the background watcher.
        - `ccm wechat-disconnect` disconnects the phone-side WeChat session.

        Peer-layer commands:
        - `ccm wechat-targets` lists addressable targets such as `kitty:<tab-title>` and `tmux:<session-name>`.
        - `ccm wechat-send <target> "..."` sends a reply-friendly message.
        - `ccm wechat-shift <target> "..."` hands work off. If the sender currently owns the phone thread, shift also rebinds phone ownership to the target and sends a handoff notice back to the phone user.

        Routing principle:
        - Phone WeChat messages reach ccm through the direct transport and then get delivered to the bound target.
        - ccm wechat messages reach visible tabs directly through kitty.
        - A stale watcher must not overwrite a newer phone login; reconnect, re-bind, then restart the watcher.
        - Do not confuse those two paths.
        """
    ).strip()


def wechat_send_to_peer(
    *,
    target: str,
    message: str,
    listen_on: str | None,
    cwd: str,
    mode: str,
    task: str = "",
    scene: str = "",
    from_target: str = "",
    transport: WeChatTransportState | None = None,
) -> dict[str, str]:
    sender_target = from_target
    if mode == "shift" and not sender_target and transport is not None and transport.bound_target:
        sender_target = transport.bound_target
    sender = resolve_sender_target(
        cwd=cwd,
        listen_on=listen_on,
        explicit_target=sender_target,
    )
    target_record = resolve_target_spec(target, listen_on=listen_on, cwd=cwd)
    phone_handoff = False
    handoff_user_id = ""
    if transport is not None and mode == "shift" and transport.bound_target == sender.target:
        wechat_bind(transport, target_record.target)
        phone_handoff = True
        handoff_user_id = active_wechat_user_id(transport)
    rendered = format_wechat_prompt(
        message,
        sender,
        mode=mode,
        task=task or sender.branch,
        scene=scene,
        compact=target_record.runtime == "claude",
    )
    payload = deliver_message_to_target(target_record, rendered, listen_on)
    if phone_handoff:
        notice = f"Phone thread transferred to {target_record.target}. Keep messaging here as usual."
        wechat_reply(transport, user_id=handoff_user_id, text=notice)
    payload["from_target"] = sender.target
    payload["to_target"] = target_record.target
    payload["reply_via"] = f'ccm wechat-send {shlex.quote(sender.target)} "..."'
    payload["shift_via"] = f'ccm wechat-shift {shlex.quote(sender.target)} "..."'
    if phone_handoff:
        payload["phone_handoff"] = "true"
        payload["phone_bound_target"] = transport.bound_target
        payload["phone_notice_user_id"] = handoff_user_id
    return payload


def emit(data: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                print(" ".join(f"{key}={value}" for key, value in item.items()))
            else:
                print(item)
        return
    if isinstance(data, dict):
        for key, value in data.items():
            print(f"{key}: {value}")
        return
    print(data)


def emit_events(events: list[dict[str, str]], *, as_json: bool) -> None:
    if as_json:
        emit(events, as_json=True)
        return
    if not events:
        print("No unread events")
        return
    for event in events:
        print(f"[{event['kind']}]")
        print(event["text"])
        print()


def emit_raw_events(events: list[dict[str, Any]], *, as_json: bool) -> None:
    if as_json:
        emit(events, as_json=True)
        return
    if not events:
        print("No unread events")
        return
    for event in events:
        print(json.dumps(event, ensure_ascii=False))


def emit_list(records: list[SessionRecord], *, as_json: bool) -> None:
    payload = [
        {
            "name": record.name,
            "tmux_session": record.tmux_session,
            "cwd": record.cwd,
            "status": session_status(record),
            "transcript": record.transcript_path or "-",
        }
        for record in records
    ]
    if as_json:
        emit(payload, as_json=True)
        return

    if not payload:
        print("No managed Claude sessions")
        return

    name_width = max(len(item["name"]) for item in payload)
    tmux_width = max(len(item["tmux_session"]) for item in payload)
    status_width = max(len(item["status"]) for item in payload)
    transcript_width = max(len(item["transcript"]) for item in payload)
    for item in payload:
        print(
            f"{item['name']:<{name_width}}  "
            f"{item['tmux_session']:<{tmux_width}}  "
            f"{item['status']:<{status_width}}  "
            f"{item['cwd']}  "
            f"{item['transcript']:<{transcript_width}}"
        )


def list_sessions_all_scopes(home_root: Path | None = None) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for state_path in discover_state_paths(home_root):
        state = load_state(state_path)
        for record in state.sessions.values():
            payload.append(
                {
                    "name": record.name,
                    "tmux_session": record.tmux_session,
                    "cwd": record.cwd,
                    "status": session_status(record),
                    "transcript": record.transcript_path or "-",
                    "state_path": str(state_path),
                }
            )
    return payload


def render_guide(audience: str) -> str:
    if audience == "human":
        return textwrap.dedent(
            """
            CCM quick guide

            Daily loop: start -> send -> read

            1. ccm doctor --cwd "$PWD"
            2. ccm start frontend-agent --cwd "$PWD"
            3. ccm send frontend-agent "..." --cwd "$PWD"
            4. ccm read frontend-agent --wait-seconds 30 --cwd "$PWD"

            Keep the agent alive when the tab will keep collaborating with it.
            Do not kill and recreate the agent after every small task unless you are
            explicitly resetting the scene.

            Use ccm guide agent when you want the longer operating rules for agents and LLMs.
            """
        ).strip()

    if audience != "agent":
        raise CCMError(f"Unsupported guide audience: {audience}")

    return textwrap.dedent(
        """
        CCM agent guide

        Canonical rules:
        - Use global `ccm` only. Do not use repo-local launchers.
        - Run `ccm doctor --cwd "$PWD"` before blaming ccm.
        - If doctor reports `@@@claude-path-mismatch` or `@@@claude-version-mismatch`,
          restart the agent.
        - Use `--state-path ...` only when you deliberately want one explicit state file
          instead of the normal cwd-derived namespace.

        Core model:
        - tmux layer = the real agent/session layer.
        - kitty layer = the visible collaboration layer.
        - These are paired capabilities. tmux keeps the agent alive and reusable.
          kitty makes the collaboration visible and relay-capable.

        Wakeup model:
        - `ccm read` is poll-based. It waits on Claude transcript output from the tmux agent.
        - `ccm relay` is push-based. It wakes another visible tab and gives it enough sender
          context to reply later.
        - Do not expect `read` to wake another agent tab for you.
        - For visible-tab peer chat, `relay` is the primary path.
        - Reading raw tab text or tmux pane tail is a legacy debug path, not a normal
          collaboration path.

        Wechat-style peer layer:
        - Use direct targets such as `kitty:<tab-title>` and `tmux:<session-name>`.
        - Use `ccm wechat-targets` to inspect what is addressable right now.
        - Use `ccm wechat-send <target> "..."` for a reply-friendly message.
        - Use `ccm wechat-shift <target> "..."` when you want to hand work off.

        Recommended operating pattern:
        - Each visible Codex tab should usually keep one dedicated, trusted, long-lived Claude
          agent in tmux and reuse it over time.
        - Pick a specific agent name per job, such as `frontend-agent` or
          `docs-editor`. Avoid colliding with agent names that already exist in the
          current namespace.
        - Do not kill the agent after every small task. The persistent session is the point.
        - Claude is not just an advisor. It can directly edit the branch too, especially for
          frontend and documentation work.

        Normal loop:
        1. `ccm doctor --cwd "$PWD"`
        2. `ccm start frontend-agent --cwd "$PWD"`
        3. `ccm send frontend-agent "..." --cwd "$PWD"`
        4. `ccm read frontend-agent --wait-seconds 30 --cwd "$PWD"`

        If `read` is empty or transcript resolution looks wrong:
        - run `ccm inspect frontend-agent --cwd "$PWD"`
        - look at the transcript search roots and pane tail before guessing
        - treat pane tail as legacy debug evidence only; do not use raw tab text as the
          normal way tabs talk to each other

        Heartbeat notes:
        - `codex-heartbeat start --tab-title mycel` keeps one named visible tab awake.
        - `codex-heartbeat test --tab-title mycel` is the one-shot push check when you
          want to verify the kitty push path without starting a background loop.

        Use `ccm open` only when:
        - the agent looks stuck and transcript output is not enough
        - you want supervised live observation
        - you are deliberately doing visible-tab collaboration

        Use `ccm relay` instead of `ccm tell` when:
        - you are an agent inside kitty
        - you expect a useful reply or acknowledgment
        - the receiver needs sender identity, worktree, branch, agent, or scene context
        - tab-to-tab chat should follow one primary path instead of falling back to raw text

        Use `ccm tell` only when:
        - you intentionally want a legacy raw fire-and-forget injection
        - no sender envelope, reply hint, or receipt convention is needed
        """
    ).strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccm",
        description=(
            "Manage interactive Claude Code agents in tmux and coordinate visible "
            "collaboration between code agents in kitty."
        ),
        epilog=(
            "Daily loop: start -> send -> read. Use interactive Claude sessions in tmux, "
            "not non-interactive print mode. Use 'open' only when the transcript is not "
            "enough: debugging a stuck agent, live observation, or deliberate visible-tab "
            "collaboration. For agents/LLMs, run 'ccm guide agent' for the longer operating "
            "rules. For visible-tab peer chat, use 'relay' as the primary path and treat raw "
            "tab text/pane tail as legacy debug-only evidence."
        ),
    )
    parser.add_argument("--cwd", help="Select the session namespace directory; for start, also use it as the Claude cwd")
    parser.add_argument(
        "--state-path",
        help="Use one explicit state.json path instead of the cwd-derived namespace path.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser(
        "start",
        help="tmux layer: start a persistent interactive Claude session",
        description=(
            "Start an agent in the current namespace. Pick a specific agent name such as "
            "'frontend-agent' or 'docs-editor'. Avoid colliding with agent names that "
            "already exist in the current namespace."
        ),
    )
    start_parser.add_argument("name")

    list_parser = subparsers.add_parser(
        "list",
        help="List managed Claude sessions",
        description="List sessions in the current namespace, or use --all-scopes to flatten every saved namespace under ~/.claude-codex-manager.",
    )
    list_parser.add_argument("--all-scopes", action="store_true")

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="tmux layer: print session diagnostics for transcript-debug and pane capture",
        description=(
            "Inspect is the fallback when 'read' is empty or transcript resolution lags. "
            "It prints the session state path, tmux session, resolved transcript path, "
            "transcript search roots, and a recent tmux pane tail. Pane tail is legacy "
            "debug evidence only, not the normal collaboration path between tabs."
        ),
    )
    inspect_parser.add_argument("name")

    send_parser = subparsers.add_parser("send", help="tmux layer: send a prompt to a managed Claude session")
    send_parser.add_argument("name")
    send_parser.add_argument("prompt")

    read_parser = subparsers.add_parser(
        "read",
        help="tmux layer: poll unread transcript events from the agent",
        description=(
            "Read is a poll-based wait on Claude transcript output. It does not push a wakeup "
            "into another agent tab. Use it when you are waiting for agent output from the tmux "
            "session itself. If you need another visible tab to wake up and answer later, use "
            "'relay' or a heartbeat-style push mechanism instead. Use --raw when you need "
            "unrendered transcript events for MCP/tool-trace debugging."
        ),
    )
    read_parser.add_argument("name")
    read_parser.add_argument("--include-user", action="store_true")
    read_parser.add_argument("--include-thinking", action="store_true")
    read_parser.add_argument(
        "--wait-seconds",
        type=float,
        default=0.0,
        help="Total poll window for waiting on new transcript events.",
    )
    read_parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between transcript polling attempts while waiting.",
    )
    read_parser.add_argument(
        "--raw",
        action="store_true",
        help="Emit raw transcript events without render_event filtering, for transcript-debug and MCP/tool traces.",
    )

    kill_parser = subparsers.add_parser("kill", help="Kill managed sessions")
    kill_parser.add_argument("names", nargs="*")
    kill_parser.add_argument("--all", action="store_true")

    cleanup_parser = subparsers.add_parser("cleanup", help="tmux layer: remove dead sessions from state and optionally kill live ones")
    cleanup_parser.add_argument("--kill-live", action="store_true")

    open_parser = subparsers.add_parser(
        "open",
        help="kitty layer: open a visible tab for a managed agent when debugging or observing live output",
        description=(
            "Open is an exception tool, not part of the everyday loop. Prefer "
            "'start -> send -> read' for normal work. Use 'open' only when you need "
            "live observation, visible-tab collaboration, or to debug a stuck agent."
        ),
    )
    open_parser.add_argument("name")
    open_parser.add_argument("--listen-on")

    tabs_parser = subparsers.add_parser("tabs", help="kitty layer: list visible tabs and their resolved identity")
    tabs_parser.add_argument("--listen-on")

    tell_parser = subparsers.add_parser(
        "tell",
        help="kitty layer: legacy raw fire-and-forget text to a visible tab",
        description=(
            "Tell is a legacy raw path. Prefer 'relay' for normal tab-to-tab chat, sender "
            "context, and reply-friendly coordination. Use 'tell' only when you explicitly "
            "want raw fire-and-forget text with no receipt convention."
        ),
    )
    tell_parser.add_argument("title")
    tell_parser.add_argument("message")
    tell_parser.add_argument("--listen-on")

    relay_parser = subparsers.add_parser(
        "relay",
        help="kitty layer: preferred for agents, send a message with sender context and reply hint",
        description=(
            "Prefer 'relay' over 'tell' when you are an agent inside kitty and expect a "
            "useful reply. Relay is the primary path for visible-tab peer chat. Relay wraps "
            "the message with sender identity and a reply hint. Use 'tell' only for legacy "
            "raw fire-and-forget text with no receipt convention."
        ),
    )
    relay_parser.add_argument("title")
    relay_parser.add_argument("message")
    relay_parser.add_argument("--listen-on")
    relay_parser.add_argument("--task", default="")
    relay_parser.add_argument("--scene", default="")
    relay_parser.add_argument("--ports", default="")

    subparsers.add_parser(
        "wechat-status",
        help="phone wechat layer: show the global direct WeChat transport state",
    )

    wechat_connect_parser = subparsers.add_parser(
        "wechat-connect",
        help="phone wechat layer: request a QR code, render it, and poll for confirmation",
    )
    wechat_connect_parser.add_argument("--qrcode", help="Resume polling an already-issued WeChat QR token")
    wechat_connect_parser.add_argument("--no-open", action="store_true")
    wechat_connect_parser.add_argument("--wait-seconds", type=float, default=180.0)
    wechat_connect_parser.add_argument("--poll-interval", type=float, default=2.0)

    subparsers.add_parser(
        "wechat-disconnect",
        help="phone wechat layer: disconnect the current WeChat account",
    )

    wechat_bind_parser = subparsers.add_parser(
        "wechat-bind",
        help="phone wechat layer: bind incoming phone messages to one direct target",
    )
    wechat_bind_parser.add_argument("target")

    subparsers.add_parser(
        "wechat-unbind",
        help="phone wechat layer: clear the bound peer alias for incoming phone messages",
    )

    subparsers.add_parser(
        "wechat-users",
        help="phone wechat layer: list known phone users who have messaged the bot",
    )

    wechat_reply_parser = subparsers.add_parser(
        "wechat-reply",
        help="phone wechat layer: reply to a phone WeChat user using its saved context token",
    )
    wechat_reply_parser.add_argument("user_id")
    wechat_reply_parser.add_argument("message")

    wechat_queue_reply_parser = subparsers.add_parser(
        "wechat-queue-reply",
        help="phone wechat layer: queue a reply for ccm to send on the active phone thread",
    )
    wechat_queue_reply_parser.add_argument("user_id")
    wechat_queue_reply_parser.add_argument("message")

    wechat_poll_once_parser = subparsers.add_parser(
        "wechat-poll-once",
        help="phone wechat layer: fetch one update batch and deliver it to the bound peer",
    )
    wechat_poll_once_parser.add_argument("--listen-on")

    wechat_watch_parser = subparsers.add_parser(
        "wechat-watch",
        help="phone wechat layer: keep polling and delivering phone messages until interrupted",
    )
    wechat_watch_parser.add_argument("--detach", action="store_true")
    wechat_watch_parser.add_argument("--listen-on")
    wechat_watch_parser.add_argument("--poll-interval", type=float, default=1.0)

    subparsers.add_parser(
        "wechat-watch-status",
        help="phone wechat layer: show the background watcher status",
    )

    subparsers.add_parser(
        "wechat-watch-stop",
        help="phone wechat layer: stop the background watcher",
    )

    wechat_targets_parser = subparsers.add_parser(
        "wechat-targets",
        help="List direct wechat targets such as visible kitty tabs and managed tmux agents",
    )
    wechat_targets_parser.add_argument("--listen-on")
    wechat_targets_parser.add_argument("--all-scopes", action="store_true")

    wechat_send_parser = subparsers.add_parser(
        "wechat-send",
        help="Send a wechat-style message to a direct target with reply instructions",
    )
    wechat_send_parser.add_argument("target")
    wechat_send_parser.add_argument("message")
    wechat_send_parser.add_argument("--listen-on")
    wechat_send_parser.add_argument("--task", default="")
    wechat_send_parser.add_argument("--scene", default="")
    wechat_send_parser.add_argument("--from-target", default="")

    wechat_shift_parser = subparsers.add_parser(
        "wechat-shift",
        help="Hand work off to a direct target; if you currently own the phone thread, shift also rebinds phone ownership",
        description=(
            "Send a stronger handoff prompt to a direct target such as `kitty:<tab-title>` or `tmux:<session-name>`. "
            "If the current sender target also owns the phone WeChat thread, "
            "this command moves phone ownership to the target as part of the same shift "
            "and sends a handoff notice back to the phone user."
        ),
    )
    wechat_shift_parser.add_argument("target")
    wechat_shift_parser.add_argument("message")
    wechat_shift_parser.add_argument("--listen-on")
    wechat_shift_parser.add_argument("--task", default="")
    wechat_shift_parser.add_argument("--scene", default="")
    wechat_shift_parser.add_argument("--from-target", default="")

    wechat_guide_parser = subparsers.add_parser(
        "wechat-guide",
        help="Read long-form guidance for phone onboarding and wechat-style peer messaging",
    )
    wechat_guide_parser.add_argument(
        "audience",
        nargs="?",
        default="human",
        choices=("human", "agent"),
        help="Which wechat guide to render: human or agent",
    )

    guide_parser = subparsers.add_parser(
        "guide",
        help="Read long-form guidance for humans or agents",
        description=(
            "Read long-form guidance for humans, agents and LLMs. "
            "Use 'guide agent' for the full playbook."
        ),
    )
    guide_parser.add_argument(
        "audience",
        nargs="?",
        default="human",
        choices=("human", "agent"),
        help="Which guide to render: human or agent",
    )

    subparsers.add_parser("doctor", help="tmux layer: report environment and session namespace health")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = normalize_global_args(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    cwd = namespace_cwd(args.cwd)
    state_path = resolve_state_path(cwd, args.state_path)
    state = load_state(state_path)
    wechat_transport_path = wechat_transport_state_path()
    wechat_transport = load_wechat_transport_state(wechat_transport_path)

    try:
        if args.command == "start":
            record = start_session(state, args.name, cwd)
            save_state(state, state_path)
            emit(
                {
                    "name": record.name,
                    "tmux_session": record.tmux_session,
                    "cwd": record.cwd,
                    "status": session_status(record),
                },
                as_json=args.json,
            )
            return 0

        if args.command == "list":
            if args.all_scopes:
                emit(list_sessions_all_scopes(DEFAULT_HOME_ROOT), as_json=args.json)
                return 0
            emit_list(list(state.sessions.values()), as_json=args.json)
            return 0

        if args.command == "inspect":
            emit(inspect_session(state, args.name, state_path), as_json=args.json)
            return 0

        if args.command == "send":
            record = send_prompt(state, args.name, args.prompt)
            save_state(state, state_path)
            emit(
                {
                    "name": record.name,
                    "tmux_session": record.tmux_session,
                    "transcript": record.transcript_path or "-",
                },
                as_json=args.json,
            )
            return 0

        if args.command == "read":
            events = read_updates(
                state,
                args.name,
                include_user=args.include_user,
                include_thinking=args.include_thinking,
                wait_seconds=args.wait_seconds,
                poll_interval=args.poll_interval,
                raw=args.raw,
            )
            save_state(state, state_path)
            if args.raw:
                emit_raw_events(events, as_json=args.json)
            else:
                emit_events(events, as_json=args.json)
            return 0

        if args.command == "kill":
            if args.all:
                names = list(state.sessions)
            else:
                names = args.names
            if not names:
                raise CCMError("Provide session names or use --all")
            killed = kill_sessions(state, names)
            save_state(state, state_path)
            emit([{"name": name, "status": "killed"} for name in killed], as_json=args.json)
            return 0

        if args.command == "cleanup":
            payload = cleanup_sessions(state, kill_live=args.kill_live)
            save_state(state, state_path)
            emit(payload, as_json=args.json)
            return 0

        if args.command == "open":
            payload = open_in_kitty(state, args.name, args.listen_on)
            emit(payload, as_json=args.json)
            return 0

        if args.command == "tabs":
            emit(list_kitty_tabs(args.listen_on), as_json=args.json)
            return 0

        if args.command == "tell":
            emit(send_message_to_kitty_tab(args.title, args.message, args.listen_on), as_json=args.json)
            return 0

        if args.command == "relay":
            emit(
                relay_message_to_kitty_tab(
                    args.title,
                    args.message,
                    args.listen_on,
                    cwd=cwd,
                    task=args.task,
                    scene=args.scene,
                    ports=args.ports,
                ),
                as_json=args.json,
            )
            return 0

        if args.command == "wechat-status":
            emit(wechat_status_payload(wechat_transport), as_json=args.json)
            return 0

        if args.command == "wechat-connect":
            emit(
                wechat_connect(
                    state_path=wechat_transport_path,
                    open_preview=not args.no_open,
                    poll_interval=args.poll_interval,
                    wait_seconds=args.wait_seconds,
                    qrcode=args.qrcode,
                ),
                as_json=args.json,
            )
            return 0

        if args.command == "wechat-disconnect":
            emit(wechat_disconnect(wechat_transport_path), as_json=args.json)
            return 0

        if args.command == "wechat-bind":
            current_transport = wechat_bind(require_wechat_transport_state(wechat_transport), args.target)
            save_wechat_transport_state(current_transport, wechat_transport_path)
            emit(wechat_status_payload(current_transport), as_json=args.json)
            return 0

        if args.command == "wechat-unbind":
            current_transport = wechat_unbind(require_wechat_transport_state(wechat_transport))
            save_wechat_transport_state(current_transport, wechat_transport_path)
            emit(wechat_status_payload(current_transport), as_json=args.json)
            return 0

        if args.command == "wechat-users":
            emit(wechat_users_payload(require_wechat_transport_state(wechat_transport)), as_json=args.json)
            return 0

        if args.command == "wechat-reply":
            emit(
                wechat_reply(require_wechat_transport_state(wechat_transport), user_id=args.user_id, text=args.message),
                as_json=args.json,
            )
            return 0

        if args.command == "wechat-queue-reply":
            current_transport = require_wechat_transport_state(wechat_transport)
            payload = queue_and_flush_wechat_reply(current_transport, user_id=args.user_id, text=args.message)
            save_wechat_transport_state_guarded(current_transport, wechat_transport_path)
            emit(payload, as_json=args.json)
            return 0

        if args.command == "wechat-poll-once":
            current_transport = require_wechat_transport_state(wechat_transport)
            payload = wechat_poll_once(current_transport, listen_on=args.listen_on)
            save_wechat_transport_state_guarded(current_transport, wechat_transport_path)
            emit(payload, as_json=args.json)
            return 0

        if args.command == "wechat-watch":
            if args.detach:
                emit(
                    launch_wechat_watch_daemon(listen_on=args.listen_on, poll_interval=args.poll_interval),
                    as_json=args.json,
                )
                return 0
            current_transport = require_wechat_transport_state(wechat_transport)
            current_pid = os.getpid()
            while True:
                guard_wechat_transport_state(current_transport, wechat_transport_path)
                try:
                    poll_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    save_wechat_watch_state(
                        pid=current_pid,
                        status="running",
                        heartbeat_at=poll_at,
                    )
                    payload = wechat_poll_once(current_transport, listen_on=args.listen_on)
                    save_wechat_transport_state_guarded(current_transport, wechat_transport_path)
                    heartbeat_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    save_wechat_watch_state(
                        pid=current_pid,
                        status="running",
                        heartbeat_at=heartbeat_at,
                        last_poll_at=heartbeat_at,
                        last_delivery_at=heartbeat_at if payload["delivered_count"] else "",
                        last_flush_at=heartbeat_at if payload["sent_replies"] else "",
                    )
                except CCMError as exc:
                    save_wechat_watch_state(
                        pid=current_pid,
                        status="error",
                        heartbeat_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        last_error=str(exc),
                    )
                    raise
                if args.json:
                    emit(payload, as_json=True)
                elif payload["delivered_count"] or payload["messages"]:
                    emit(payload, as_json=False)
                time.sleep(args.poll_interval)
            return 0

        if args.command == "wechat-watch-status":
            emit(wechat_watch_status(), as_json=args.json)
            return 0

        if args.command == "wechat-watch-stop":
            emit(wechat_watch_stop(), as_json=args.json)
            return 0

        if args.command == "wechat-targets":
            emit(wechat_targets_payload(cwd=cwd, listen_on=args.listen_on, all_scopes=args.all_scopes), as_json=args.json)
            return 0

        if args.command == "wechat-send":
            emit(
                wechat_send_to_peer(
                    target=args.target,
                    message=args.message,
                    listen_on=args.listen_on,
                    cwd=cwd,
                    mode="send",
                    task=args.task,
                    scene=args.scene,
                    from_target=args.from_target,
                    transport=None,
                ),
                as_json=args.json,
            )
            return 0

        if args.command == "wechat-shift":
            current_transport = require_wechat_transport_state(wechat_transport) if wechat_transport is not None else None
            payload = wechat_send_to_peer(
                target=args.target,
                message=args.message,
                listen_on=args.listen_on,
                cwd=cwd,
                mode="shift",
                task=args.task,
                scene=args.scene,
                from_target=args.from_target,
                transport=current_transport,
            )
            if current_transport is not None and payload.get("phone_handoff") == "true":
                save_wechat_transport_state(current_transport, wechat_transport_path)
            emit(payload, as_json=args.json)
            return 0

        if args.command == "wechat-guide":
            print(render_wechat_guide(args.audience))
            return 0

        if args.command == "guide":
            print(render_guide(args.audience))
            return 0

        if args.command == "doctor":
            emit(doctor_report(state, cwd, state_path), as_json=args.json)
            return 0

        raise CCMError(f"Unsupported command: {args.command}")
    except CCMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
