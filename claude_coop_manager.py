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
CLAUDE_PROJECTS_ROOT = Path(
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


def build_claude_command(display_name: str) -> list[str]:
    return ["claude", "--dangerously-skip-permissions", "-n", display_name]


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
    return find_transcript_file(
        projects_root=CLAUDE_PROJECTS_ROOT,
        display_name=record.display_name,
        cwd=record.cwd,
        started_after=record.started_at,
    )


def session_status(record: SessionRecord) -> str:
    return "running" if tmux_has_session(record.tmux_session) else "dead"


def doctor_report(state: State, cwd: str, state_path: Path) -> dict[str, Any]:
    endpoint = os.environ.get("KITTY_LISTEN_ON")
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
    }


def start_session(state: State, name: str, cwd: str) -> SessionRecord:
    require_binary("tmux")
    require_binary("claude")

    if name in state.sessions:
        raise CCMError(f"Managed session already exists: {name}")

    tmux_session = build_tmux_session_name(name, cwd)
    if tmux_has_session(tmux_session):
        raise CCMError(f"tmux session already exists: {tmux_session}")

    command = shlex.join(build_claude_command(name))
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
    parser = argparse.ArgumentParser(description="Manage interactive Claude Code sessions for Codex")
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

    open_parser = subparsers.add_parser("open", help="Open a kitty tab attached to the tmux session")
    open_parser.add_argument("name")
    open_parser.add_argument("--listen-on")

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

        if args.command == "open":
            payload = open_in_kitty(state, args.name, args.listen_on)
            emit(payload, as_json=args.json)
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
