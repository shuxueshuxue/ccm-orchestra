# Heartbeat Start And Status Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `codex-heartbeat start` reliably launch its background loop from the shipped CLI and make `status` stop reporting false `not-running` immediately after a successful start.

**Architecture:** Keep the existing heartbeat model, but fix the launch seam and the readiness seam. Launch the child in a way that does not depend on an ambient import path, then wait for the child-owned pid file to become visible and alive before returning success.

**Tech Stack:** Python 3.11+, `unittest`, `subprocess`, `pathlib`

---

### Task 1: Lock In The Failing Lifecycle Cases

**Files:**
- Modify: `tests/test_heartbeat.py`
- Test: `tests/test_heartbeat.py`

- [ ] **Step 1: Write a failing test for the launch command path**

Add a lifecycle test that asserts `start_background()` launches the child by executing the heartbeat file directly instead of `python -m ccm_orchestra.heartbeat`.

```python
    @mock.patch("ccm_orchestra.heartbeat.subprocess.Popen", autospec=True)
    @mock.patch("ccm_orchestra.heartbeat.resolve_tab_window_id", autospec=True, return_value=777)
    def test_start_background_launches_current_heartbeat_file(self, resolve_tab_window_id, popen):
        process = mock.Mock()
        process.pid = 42424
        popen.return_value = process

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(heartbeat, "STATE_DIR", Path(tmp)), \
             mock.patch("ccm_orchestra.heartbeat.time.sleep"), \
             mock.patch("ccm_orchestra.heartbeat.wait_for_heartbeat_ready", return_value=42424):
            exit_code = heartbeat.start_background("unix:/tmp/mykitty", 30, "hello", "Feature Main")

        self.assertEqual(exit_code, 0)
        launched = popen.call_args.args[0]
        self.assertEqual(launched[0], heartbeat.sys.executable)
        self.assertEqual(Path(launched[1]), Path(heartbeat.__file__).resolve())
        self.assertEqual(launched[2], "run")
```

- [ ] **Step 2: Write a failing test for the ready/status race**

Add a test for a new helper that waits for the pid file written by the child loop. The test should prove `start_background()` does not report success until the pid file points to a live process.

```python
    def test_wait_for_heartbeat_ready_reads_child_pid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "feature-main.pid"
            alive = {98765}

            def fake_sleep(_seconds):
                pid_path.write_text("98765\n")

            with mock.patch("ccm_orchestra.heartbeat.time.sleep", side_effect=fake_sleep), \
                 mock.patch("ccm_orchestra.heartbeat.pid_is_alive", side_effect=lambda pid: pid in alive):
                pid = heartbeat.wait_for_heartbeat_ready(pid_path, startup_pid=42424, timeout_seconds=2.0, poll_interval=0.1)

        self.assertEqual(pid, 98765)
```

- [ ] **Step 3: Run the heartbeat tests to verify RED**

Run:

```bash
cd /Users/lexicalmathical/Codebase/ccm-orchestra
python3 -m unittest tests/test_heartbeat.py -v
```

Expected: new heartbeat lifecycle tests fail because the helper does not exist and `start_background()` still launches via `-m ccm_orchestra.heartbeat`.

### Task 2: Implement The Minimal Heartbeat Fix

**Files:**
- Modify: `ccm_orchestra/heartbeat.py`
- Test: `tests/test_heartbeat.py`

- [ ] **Step 1: Add a helper for direct script launch**

Add a helper that resolves the current heartbeat file path for child launches.

```python
def heartbeat_entrypoint_path() -> Path:
    return Path(__file__).resolve()
```

- [ ] **Step 2: Add a helper that waits for the child-owned pid file**

Implement a small polling helper that waits until the pid file exists, contains an integer pid, and that pid is alive. If the startup pid dies first, fail loudly.

```python
def wait_for_heartbeat_ready(
    pid_path: Path,
    *,
    startup_pid: int,
    timeout_seconds: float = 2.0,
    poll_interval: float = 0.1,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        child_pid = read_pid(pid_path)
        if pid_is_alive(child_pid):
            return child_pid
        if not pid_is_alive(startup_pid):
            raise RuntimeError("Heartbeat process exited before becoming ready.")
        time.sleep(poll_interval)
    raise RuntimeError("Heartbeat process did not become ready in time.")
```

- [ ] **Step 3: Update `start_background()` to use the direct file path and ready helper**

Replace the `-m ccm_orchestra.heartbeat` launch path with the direct script path, clear stale log contents before launch, and wait on the child-owned pid file before reporting success.

```python
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [
                sys.executable,
                str(heartbeat_entrypoint_path()),
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

    ready_pid = wait_for_heartbeat_ready(pid_path, startup_pid=process.pid)
    print(f"started pid={ready_pid} interval_seconds={interval_seconds} tab_title={tab_title} log={log_path}")
```

- [ ] **Step 4: Run the heartbeat tests to verify GREEN**

Run:

```bash
cd /Users/lexicalmathical/Codebase/ccm-orchestra
python3 -m unittest tests/test_heartbeat.py -v
```

Expected: heartbeat tests pass.

### Task 3: Verify The User-Facing Behavior

**Files:**
- Modify: `tests/test_heartbeat.py`
- Test: `tests/test_heartbeat.py`

- [ ] **Step 1: Add one more regression test for status output after ready**

Add a small regression test showing that `status_background()` reports the pid written by the child-owned pid file.

```python
    def test_status_background_reports_ready_child_pid(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(heartbeat, "STATE_DIR", Path(tmp)):
            pid_path = heartbeat.heartbeat_pid_path("Feature Main")
            pid_path.write_text("98765\n")

            with mock.patch("ccm_orchestra.heartbeat.pid_is_alive", side_effect=lambda pid: pid == 98765), \
                 mock.patch("sys.stdout.write") as write:
                exit_code = heartbeat.status_background(tab_title="Feature Main")

        self.assertEqual(exit_code, 0)
        self.assertIn("running pid=98765", "".join(call.args[0] for call in write.call_args_list))
```

- [ ] **Step 2: Run the focused suite again**

Run:

```bash
cd /Users/lexicalmathical/Codebase/ccm-orchestra
python3 -m unittest tests/test_heartbeat.py tests/test_smoke.py -v
```

Expected: all tests pass, including smoke coverage that still reads `codex-heartbeat status`.

- [ ] **Step 3: Manual proof on the visible kitty path**

Run:

```bash
cd /Users/lexicalmathical/Codebase/ccm-orchestra
python3 -m pip install -e .
codex-heartbeat start --tab-title heartbeat-lab --interval-seconds 10 --message HB_MANUAL
codex-heartbeat status --tab-title heartbeat-lab
codex-heartbeat stop --tab-title heartbeat-lab
```

Expected:
- `start` prints `started pid=<child-pid>`
- immediate `status` prints `running pid=<same-child-pid>`
- the target tab receives `HB_MANUAL`
- `stop` prints `stopped pid=<child-pid>`

## Execution Notes

- The shipped `codex-heartbeat` seam was two-layered, not one bug. First, detached `start` used `python -m ccm_orchestra.heartbeat`, which fails when the child process does not inherit an importable `ccm_orchestra` path. Second, even when launch works, `status` could report `not-running` because the old start path returned before the child-owned pid file became visible.
- The minimal fix stayed narrow. `start_background()` now launches the current heartbeat file directly, truncates the scoped log file so stale stack traces do not masquerade as current failures, and waits for the child-owned pid file to contain a live pid before reporting success.
- Fresh focused proof on the repo tree is green: `python3 -m unittest tests/test_heartbeat.py tests/test_smoke.py -v` passed with 16 tests.
- Fresh installed-runtime proof is also green. A disposable tab `heartbeat-final-20260408` running a simple stdin echo loop was used as the visible target; installed `codex-heartbeat start --tab-title heartbeat-final-20260408 --interval-seconds 2 --message HB_READY_20260408` reported `started pid=13143`, immediate `status` reported `running pid=13143`, `kitty get-text` on the target window showed two delivered `HB_READY_20260408` lines, and `stop` reported `stopped pid=13143`.
- Stopline for this slice is now explicit: the repo implementation, repo tests, and the installed runtime are aligned, and live proof now covers the real detached `start -> status -> stop` path instead of only mocked lifecycle tests.
