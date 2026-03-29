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
PID_PATH = STATE_DIR / "main.pid"
LOG_PATH = STATE_DIR / "main.log"
DEFAULT_ENDPOINT = "unix:/tmp/mykitty"
DEFAULT_INTERVAL_SECONDS = 1500
DEFAULT_MESSAGE = (
    "Heartbeat check. Review active Codex or Claude-coop work only if there is still "
    "unfinished supervised work. Otherwise do nothing."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Keep the Main Codex tab awake via kitty.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    start.add_argument("--endpoint", default=os.environ.get("KITTY_LISTEN_ON", DEFAULT_ENDPOINT))
    start.add_argument("--message", default=DEFAULT_MESSAGE)

    run = subparsers.add_parser("run")
    run.add_argument("--interval-seconds", type=int, required=True)
    run.add_argument("--endpoint", required=True)
    run.add_argument("--message", required=True)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--pid-file", default=str(PID_PATH))

    status = subparsers.add_parser("status")
    status.add_argument("--pid-file", default=str(PID_PATH))

    return parser.parse_args()


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


def resolve_main_window_id(endpoint: str) -> int:
    payload = kitty_ls(endpoint)
    for os_window in payload:
        for tab in os_window.get("tabs", []):
            if tab.get("title", "").lower() != "main":
                continue
            windows = tab.get("windows", [])
            if not windows:
                continue
            active = next((window for window in windows if window.get("is_active")), windows[0])
            return int(active["id"])
    raise RuntimeError("No kitty tab titled 'main' was found.")


def send_heartbeat(endpoint: str, message: str) -> None:
    window_id = resolve_main_window_id(endpoint)
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


def run_loop(endpoint: str, interval_seconds: int, message: str) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(f"{os.getpid()}\n")
    try:
        while True:
            time.sleep(interval_seconds)
            send_heartbeat(endpoint, message)
    finally:
        PID_PATH.unlink(missing_ok=True)


def start_background(endpoint: str, interval_seconds: int, message: str) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    existing = read_pid(PID_PATH)
    if pid_is_alive(existing):
        print(f"already-running pid={existing}")
        return 0

    resolve_main_window_id(endpoint)

    with LOG_PATH.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "codex_heartbeat",
                "run",
                "--interval-seconds",
                str(interval_seconds),
                "--endpoint",
                endpoint,
                "--message",
                message,
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    time.sleep(1)
    if not pid_is_alive(process.pid):
        raise RuntimeError("Heartbeat process exited immediately.")

    print(f"started pid={process.pid} interval_seconds={interval_seconds} log={LOG_PATH}")
    return 0


def stop_background(pid_path: Path) -> int:
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


def status_background(pid_path: Path) -> int:
    pid = read_pid(pid_path)
    if pid_is_alive(pid):
        print(f"running pid={pid} log={LOG_PATH}")
        return 0
    print("not-running")
    return 1


def main() -> int:
    args = parse_args()
    if args.command == "start":
        return start_background(args.endpoint, args.interval_seconds, args.message)
    if args.command == "run":
        return run_loop(args.endpoint, args.interval_seconds, args.message)
    if args.command == "stop":
        return stop_background(Path(args.pid_file).expanduser())
    if args.command == "status":
        return status_background(Path(args.pid_file).expanduser())
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
