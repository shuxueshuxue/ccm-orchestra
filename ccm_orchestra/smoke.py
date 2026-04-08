from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any


DEFAULT_AGENT_NAME = "smoke-agent"
DEFAULT_READ_WAIT_SECONDS = 20.0


def build_parser(*, prog: str = "ccm-smoke") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run a live ccm smoke check against the current environment.",
    )
    parser.add_argument("--cwd", default=str(Path.cwd()), help="Namespace/worktree to smoke check.")
    parser.add_argument("--agent-name", default=DEFAULT_AGENT_NAME)
    parser.add_argument("--read-wait-seconds", type=float, default=DEFAULT_READ_WAIT_SECONDS)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def run_cli(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def parse_json_output(result: subprocess.CompletedProcess[str]) -> Any:
    if not result.stdout.strip():
        raise RuntimeError("Command produced no stdout to parse as JSON")
    return json.loads(result.stdout)


def heartbeat_status() -> dict[str, Any]:
    result = run_cli(["ccm", "heartbeat", "status"], check=False)
    raw = result.stdout.strip()
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip() or raw or "ccm heartbeat status failed")
    return {"running": result.returncode == 0, "raw": raw}


def smoke_prompt(token: str) -> str:
    return f"Reply with exactly {token} and nothing else."


def assistant_event_texts(events: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("kind") == "assistant" and isinstance(event.get("text"), str):
            texts.append(event["text"])
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message", {})
        if message.get("role") != "assistant":
            continue
        content = message.get("content", [])
        if isinstance(content, str):
            texts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
    return texts


def events_include_token(events: list[dict[str, Any]], token: str) -> bool:
    return any(token in text for text in assistant_event_texts(events))


def first_terminal_failure(events: list[dict[str, Any]]) -> str:
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("error") == "rate_limit":
            return "Claude usage limit blocked the smoke agent before it could echo the probe token."
        if event.get("error"):
            return f"Smoke agent returned terminal error: {event.get('error')}"
    return ""


def read_probe_events(
    *,
    cwd: str,
    agent_name: str,
    total_wait_seconds: float,
    poll_interval: float = 1.0,
) -> list[dict[str, Any]]:
    remaining_wait = max(total_wait_seconds, 0.0)
    events: list[dict[str, Any]] = []

    while True:
        chunk_wait = min(poll_interval, remaining_wait)
        chunk = parse_json_output(
            run_cli(
                [
                    "ccm",
                    "--json",
                    "--cwd",
                    cwd,
                    "read",
                    agent_name,
                    "--wait-seconds",
                    str(chunk_wait),
                    "--raw",
                ]
            )
        )
        events.extend(chunk)
        remaining_wait = max(0.0, remaining_wait - chunk_wait)

        if assistant_event_texts(events):
            return events
        if first_terminal_failure(events):
            return events
        if remaining_wait <= 0.0:
            return events


def run_smoke(
    *,
    cwd: str,
    agent_name: str,
    read_wait_seconds: float,
    probe_token: str,
) -> dict[str, Any]:
    doctor = parse_json_output(run_cli(["ccm", "--json", "--cwd", cwd, "doctor"]))
    heartbeat = heartbeat_status()
    start = parse_json_output(run_cli(["ccm", "--json", "--cwd", cwd, "start", agent_name]))
    killed = False
    try:
        sessions = parse_json_output(run_cli(["ccm", "--json", "--cwd", cwd, "list"]))
        send = parse_json_output(
            run_cli(["ccm", "--json", "--cwd", cwd, "send", agent_name, smoke_prompt(probe_token)])
        )
        events = read_probe_events(
            cwd=cwd,
            agent_name=agent_name,
            total_wait_seconds=read_wait_seconds,
        )
        terminal_failure = first_terminal_failure(events)
        if terminal_failure:
            raise RuntimeError(terminal_failure)
        if not events_include_token(events, probe_token):
            raise RuntimeError(f"Smoke read completed but did not contain probe token: {probe_token}")
        kill = parse_json_output(run_cli(["ccm", "--json", "--cwd", cwd, "kill", agent_name]))
        killed = True
        cleanup = parse_json_output(run_cli(["ccm", "--json", "--cwd", cwd, "cleanup"]))
        return {
            "ok": True,
            "cwd": cwd,
            "agent_name": agent_name,
            "probe_token": probe_token,
            "doctor": doctor,
            "heartbeat": heartbeat,
            "start": start,
            "sessions": sessions,
            "send": send,
            "events": events,
            "kill": kill,
            "cleanup": cleanup,
        }
    finally:
        if not killed:
            run_cli(["ccm", "--json", "--cwd", cwd, "kill", agent_name], check=False)
            run_cli(["ccm", "--json", "--cwd", cwd, "cleanup"], check=False)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    probe_token = f"CCM_SMOKE_ACK_{int(time.time())}"
    payload = run_smoke(
        cwd=args.cwd,
        agent_name=args.agent_name,
        read_wait_seconds=args.read_wait_seconds,
        probe_token=probe_token,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"ok: {payload['ok']}")
        print(f"cwd: {payload['cwd']}")
        print(f"agent_name: {payload['agent_name']}")
        print(f"probe_token: {payload['probe_token']}")
        print(f"heartbeat_running: {payload['heartbeat']['running']}")
        print(f"events: {len(payload['events'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
