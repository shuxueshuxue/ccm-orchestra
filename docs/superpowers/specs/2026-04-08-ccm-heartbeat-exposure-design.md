# CCM Heartbeat Exposure Design

## Goal

Expose the existing visible-tab heartbeat tool through the main `ccm` entry point so operators do not have to remember a separate binary name to discover or use it.

## Scope

In scope:

- add a `ccm heartbeat ...` alias that forwards to the existing heartbeat implementation
- keep `codex-heartbeat` working unchanged
- update the shortest-path docs and help text so they stop claiming heartbeat is only a separate tool

Out of scope:

- changing heartbeat behavior
- merging heartbeat code into `cli.py`
- removing `codex-heartbeat`
- adding a general scheduler or watcher abstraction

## Recommendation

Add a narrow forwarding subcommand in `ccm`:

- `ccm heartbeat start ...`
- `ccm heartbeat status ...`
- `ccm heartbeat test ...`
- `ccm heartbeat stop ...`

This keeps one codepath for behavior while improving discoverability. The alias should delegate directly to `ccm_orchestra.heartbeat.main(...)` and reuse the existing heartbeat parser and logic.

## Files

- Modify: `ccm_orchestra/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `AGENTS.md`
- Modify: `docs/codex-claude-visible-collab-playbook.md`

## Verification

- unit test: parser exposes `heartbeat`
- unit test: `ccm main(["heartbeat", ...])` delegates to heartbeat module
- focused suite: `python3 -m unittest tests/test_cli.py -v`
- heartbeat suite: `python3 -m unittest tests/test_heartbeat.py tests/test_smoke.py -v`
- manual check: `ccm heartbeat --help`
