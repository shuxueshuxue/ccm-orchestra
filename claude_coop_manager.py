#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


STATE_VERSION = 1
DEFAULT_HOME_ROOT = Path("~/.claude-codex-manager").expanduser()
DEFAULT_CLAUDE_PROJECTS_ROOT = Path(
    os.environ.get("CCM_CLAUDE_PROJECTS_ROOT", "~/.claude/projects")
).expanduser()
READY_DELAY_SECONDS = 2.0
DEFAULT_READY_TIMEOUT_SECONDS = 300.0


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


def sanitize_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    normalized = normalized.strip("-")
    if not normalized:
        raise ValueError("Session name becomes empty after normalization")
    return normalized


def resolve_claude_executable() -> str:
    path = shutil.which("claude")
    if path is None:
        raise CCMError("Missing required binary: claude")
    return path


def launch_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("CLAUDE_CONFIG_DIR",):
        value = os.environ.get(key)
        if value:
            env[key] = value
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
        rest.append(arg)
        index += 1
    return front + rest


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


def save_state(state: State, state_path: Path | None = None) -> None:
    state_path = state_path or default_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": state.version,
        "sessions": {name: asdict(record) for name, record in state.sessions.items()},
    }
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


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
        re.match(r"^[✻✽✶·✢✳]\s+.+…(?:\s+\(\d+s\))?$", line)
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


def ensure_session_ready(
    record: SessionRecord,
    *,
    retries: int | None = None,
    delay_seconds: float = READY_DELAY_SECONDS,
) -> str:
    retries = ready_retry_budget(delay_seconds) if retries is None else retries
    last_pane = ""
    for _ in range(retries):
        last_pane = tmux_capture(record.tmux_session)
        if pane_needs_trust_acceptance(last_pane):
            tmux_send_enter(record.tmux_session)
            time.sleep(delay_seconds)
            continue
        if pane_is_ready_for_input(last_pane):
            return last_pane
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
            "helper": "",
            "helper_status": "",
            "helper_tmux_session": "",
            "helper_transcript": "",
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
        "helper": session.name if session is not None else "",
        "helper_status": session_status(session) if session is not None else "",
        "helper_tmux_session": session.tmux_session if session is not None else "",
        "helper_transcript": transcript,
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
        "tmux_command": build_tmux_claude_command("frontend-helper") if shutil.which("claude") else "",
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
) -> list[dict[str, str]]:
    if name not in state.sessions:
        raise CCMError(f"Unknown managed session: {name}")
    record = state.sessions[name]
    deadline = time.time() + max(wait_seconds, 0.0)

    while True:
        transcript = resolve_transcript(record)
        if transcript is None:
            if time.time() >= deadline:
                raise CCMError(f"No transcript resolved yet for session {name}. Send a prompt first.")
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
                    "helper": identity["helper"],
                    "helper_status": identity["helper_status"],
                    "helper_tmux_session": identity["helper_tmux_session"],
                    "helper_transcript": identity["helper_transcript"],
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


def resolve_current_sender_context(cwd: str, listen_on: str | None) -> dict[str, str]:
    resolved_cwd = namespace_cwd(cwd)
    context = workspace_identity(resolved_cwd)
    context["title"] = context["branch"] or Path(resolved_cwd).name or "unknown"
    current_window_id = os.environ.get("KITTY_WINDOW_ID")
    if not current_window_id:
        return context

    for tab in list_kitty_tabs(listen_on):
        if tab["window_id"] == current_window_id:
            context["title"] = tab["title"] or context["title"]
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
        ("helper", sender.get("helper", "")),
        ("tmux", sender.get("helper_tmux_session", "")),
        ("transcript", sender.get("helper_transcript", "")),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage interactive Claude Code sessions for Codex",
        epilog=(
            "Daily loop: start -> send -> read. Use 'open' only when the transcript "
            "is not enough: debugging a stuck helper, live observation, or deliberate "
            "visible-tab collaboration."
        ),
    )
    parser.add_argument("--cwd", help="Select the session namespace directory; for start, also use it as the Claude cwd")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Start a managed Claude session")
    start_parser.add_argument("name")

    subparsers.add_parser("list", help="List managed Claude sessions")

    send_parser = subparsers.add_parser("send", help="Send a prompt to a Claude session")
    send_parser.add_argument("name")
    send_parser.add_argument("prompt")

    read_parser = subparsers.add_parser("read", help="Read unread transcript events")
    read_parser.add_argument("name")
    read_parser.add_argument("--include-user", action="store_true")
    read_parser.add_argument("--include-thinking", action="store_true")
    read_parser.add_argument("--wait-seconds", type=float, default=0.0)
    read_parser.add_argument("--poll-interval", type=float, default=2.0)

    kill_parser = subparsers.add_parser("kill", help="Kill managed sessions")
    kill_parser.add_argument("names", nargs="*")
    kill_parser.add_argument("--all", action="store_true")

    cleanup_parser = subparsers.add_parser("cleanup", help="Remove dead sessions from state and optionally kill live ones")
    cleanup_parser.add_argument("--kill-live", action="store_true")

    open_parser = subparsers.add_parser(
        "open",
        help="Open a visible kitty tab for a managed helper when debugging or observing live output",
        description=(
            "Open is an exception tool, not part of the everyday loop. Prefer "
            "'start -> send -> read' for normal work. Use 'open' only when you need "
            "live observation, visible-tab collaboration, or to debug a stuck helper."
        ),
    )
    open_parser.add_argument("name")
    open_parser.add_argument("--listen-on")

    tabs_parser = subparsers.add_parser("tabs", help="List visible kitty tabs that can receive messages")
    tabs_parser.add_argument("--listen-on")

    tell_parser = subparsers.add_parser("tell", help="Send a message to a visible kitty tab by title")
    tell_parser.add_argument("title")
    tell_parser.add_argument("message")
    tell_parser.add_argument("--listen-on")

    relay_parser = subparsers.add_parser(
        "relay",
        help="Preferred for agents in kitty: send a message with sender context and reply hint",
        description=(
            "Prefer 'relay' over 'tell' when you are an agent inside kitty and expect a "
            "useful reply. Relay wraps the message with sender identity and a reply hint. "
            "Use 'tell' only for raw fire-and-forget text with no receipt convention."
        ),
    )
    relay_parser.add_argument("title")
    relay_parser.add_argument("message")
    relay_parser.add_argument("--listen-on")
    relay_parser.add_argument("--task", default="")
    relay_parser.add_argument("--scene", default="")
    relay_parser.add_argument("--ports", default="")

    subparsers.add_parser("doctor", help="Report current environment and session namespace health")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = normalize_global_args(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    cwd = namespace_cwd(args.cwd)
    state_path = default_state_path(cwd)
    state = load_state(state_path)

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
            emit_list(list(state.sessions.values()), as_json=args.json)
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
            )
            save_state(state, state_path)
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

        if args.command == "doctor":
            emit(doctor_report(state, cwd, state_path), as_json=args.json)
            return 0

        raise CCMError(f"Unsupported command: {args.command}")
    except CCMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
