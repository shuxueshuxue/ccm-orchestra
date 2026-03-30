from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


STATE_DIR = Path("~/.codex/heartbeat").expanduser()
DEFAULT_ENDPOINT = "unix:/tmp/mykitty"
DEFAULT_INTERVAL_SECONDS = 1500
DEFAULT_MESSAGE = (
    "Heartbeat check. Review active Codex or Claude-coop work only if there is still "
    "unfinished supervised work. Otherwise do nothing."
)
DEFAULT_TAB_TITLE = "main"


def slugify_tab_title(tab_title: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in tab_title.strip())
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or DEFAULT_TAB_TITLE


def heartbeat_pid_path(tab_title: str) -> Path:
    return STATE_DIR / f"{slugify_tab_title(tab_title)}.pid"


def heartbeat_log_path(tab_title: str) -> Path:
    return STATE_DIR / f"{slugify_tab_title(tab_title)}.log"


def build_parser(*, prog: str = "codex-heartbeat") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Keep a Codex kitty tab awake via kitty. "
            "Use --tab-title to target a non-main tab, or 'test' for a one-shot push."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start the background heartbeat loop for one tab title")
    start.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    start.add_argument("--endpoint", default=os.environ.get("KITTY_LISTEN_ON", DEFAULT_ENDPOINT))
    start.add_argument("--message", default=DEFAULT_MESSAGE)
    start.add_argument("--tab-title", default=DEFAULT_TAB_TITLE)

    run = subparsers.add_parser("run", help="Internal loop runner for one tab title")
    run.add_argument("--interval-seconds", type=int, required=True)
    run.add_argument("--endpoint", required=True)
    run.add_argument("--message", required=True)
    run.add_argument("--tab-title", required=True)

    test = subparsers.add_parser("test", help="Send one heartbeat immediately without starting a loop")
    test.add_argument("--endpoint", default=os.environ.get("KITTY_LISTEN_ON", DEFAULT_ENDPOINT))
    test.add_argument("--message", default=DEFAULT_MESSAGE)
    test.add_argument("--tab-title", default=DEFAULT_TAB_TITLE)

    stop = subparsers.add_parser("stop", help="Stop the background heartbeat loop for one tab title")
    stop.add_argument("--tab-title", default=DEFAULT_TAB_TITLE)
    stop.add_argument("--pid-file")

    status = subparsers.add_parser("status", help="Show heartbeat status for one tab title")
    status.add_argument("--tab-title", default=DEFAULT_TAB_TITLE)
    status.add_argument("--pid-file")

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except ValueError:
        return None


def pid_is_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kitty_ls(endpoint: str) -> list[dict]:
    result = subprocess.run(
        ["kitty", "@", "--to", endpoint, "ls"],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def resolve_tab_window_id(endpoint: str, tab_title: str) -> int:
    payload = kitty_ls(endpoint)
    for os_window in payload:
        for tab in os_window.get("tabs", []):
            if tab.get("title", "").lower() != tab_title.lower():
                continue
            windows = tab.get("windows", [])
            if not windows:
                continue
            active = next((window for window in windows if window.get("is_active")), windows[0])
            return int(active["id"])
    raise RuntimeError(f"No kitty tab titled '{tab_title}' was found.")


def send_heartbeat(endpoint: str, message: str, tab_title: str) -> int:
    window_id = resolve_tab_window_id(endpoint, tab_title)
    # @@@heartbeat-send - We resolve the target window fresh every cycle so the loop
    # survives tab recreation instead of pinning to a stale window id.
    subprocess.run(
        ["kitty", "@", "--to", endpoint, "send-text", "--match", f"id:{window_id}", message],
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["kitty", "@", "--to", endpoint, "send-key", "--match", f"id:{window_id}", "enter"],
        text=True,
        capture_output=True,
        check=True,
    )
    return window_id


def run_loop(endpoint: str, interval_seconds: int, message: str, tab_title: str) -> int:
    pid_path = heartbeat_pid_path(tab_title)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{os.getpid()}\n")
    try:
        while True:
            time.sleep(interval_seconds)
            send_heartbeat(endpoint, message, tab_title)
    finally:
        pid_path.unlink(missing_ok=True)


def start_background(endpoint: str, interval_seconds: int, message: str, tab_title: str) -> int:
    pid_path = heartbeat_pid_path(tab_title)
    log_path = heartbeat_log_path(tab_title)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    existing = read_pid(pid_path)
    if pid_is_alive(existing):
        print(f"already-running pid={existing}")
        return 0

    resolve_tab_window_id(endpoint, tab_title)

    with log_path.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ccm_orchestra.heartbeat",
                "run",
                "--interval-seconds",
                str(interval_seconds),
                "--endpoint",
                endpoint,
                "--message",
                message,
                "--tab-title",
                tab_title,
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    time.sleep(1)
    if not pid_is_alive(process.pid):
        raise RuntimeError("Heartbeat process exited immediately.")

    print(f"started pid={process.pid} interval_seconds={interval_seconds} tab_title={tab_title} log={log_path}")
    return 0


def stop_background(pid_path: Path | None = None, *, tab_title: str = DEFAULT_TAB_TITLE) -> int:
    pid_path = pid_path or heartbeat_pid_path(tab_title)
    pid = read_pid(pid_path)
    if not pid_is_alive(pid):
        pid_path.unlink(missing_ok=True)
        print("not-running")
        return 0

    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not pid_is_alive(pid):
            pid_path.unlink(missing_ok=True)
            print(f"stopped pid={pid}")
            return 0
        time.sleep(0.25)

    print(f"failed-to-stop pid={pid}", file=sys.stderr)
    return 1


def status_background(pid_path: Path | None = None, *, tab_title: str = DEFAULT_TAB_TITLE) -> int:
    pid_path = pid_path or heartbeat_pid_path(tab_title)
    log_path = heartbeat_log_path(tab_title)
    pid = read_pid(pid_path)
    if pid_is_alive(pid):
        print(f"running pid={pid} tab_title={tab_title} log={log_path}")
        return 0
    print("not-running")
    return 1


def test_once(endpoint: str, message: str, tab_title: str) -> int:
    window_id = send_heartbeat(endpoint, message, tab_title)
    print(f"sent tab_title={tab_title} window_id={window_id}")
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "start":
        return start_background(args.endpoint, args.interval_seconds, args.message, args.tab_title)
    if args.command == "run":
        return run_loop(args.endpoint, args.interval_seconds, args.message, args.tab_title)
    if args.command == "test":
        return test_once(args.endpoint, args.message, args.tab_title)
    if args.command == "stop":
        pid_path = Path(args.pid_file).expanduser() if args.pid_file else None
        return stop_background(pid_path, tab_title=args.tab_title)
    if args.command == "status":
        pid_path = Path(args.pid_file).expanduser() if args.pid_file else None
        return status_background(pid_path, tab_title=args.tab_title)
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
