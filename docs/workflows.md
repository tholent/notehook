# Workflow automation

`notehook workflows` runs Python scripts automatically when notes are
created, updated, or deleted in a watched folder. This is the user guide —
for the design and the frozen contracts, see
[workflow-spec.md](workflow-spec.md).

## How it fits together

```
notehook sync / notehook daemon   →   events.db   →   notehook workflows serve   →   uv run <your script>
   (records what changed)             (durable log)      (matches + schedules)         (one subprocess per run)
```

A workflow only ever sees `(event, config)` — it doesn't know or care
whether the change came from the device, from editing a file locally, or
from `notehook workflows backfill`.

## Quick start

```bash
# 1. Write a workflow (see "Writing a workflow" below), or use the example
#    at the bottom of this guide.

# 2. Install it, binding it to the folder(s) it should react to
uv run notehook workflows install ./my-workflow --paths "Note/ToReader/**"

# install prints a disclosure block (declared inputs/secrets, dependencies,
# and a reminder that workflows run unsandboxed) and prompts for anything
# required that you didn't pass on the command line.

# 3. Run the scheduler
uv run notehook workflows serve
```

`serve` needs `notehook sync`/`notehook daemon` to actually be producing
events — workflows react to sync activity, they don't trigger it.

## Writing a workflow

A workflow is a single `.py` file with a
[PEP 723](https://peps.python.org/pep-0723/) inline metadata block, or a
small package with a `pyproject.toml`. Either way, the contract is the same:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
#
# [tool.notehook]
# name = "note-to-pdf"
# description = "Convert a new/updated note to PDF"
# suggested_paths = ["Note/**"]
#
# [tool.notehook.inputs]
# output_dir = { default = "PDFs" }
# ///
from notehook_workflow import workflow, RetryLater

@workflow(on=["created", "updated"])
def run(event, config):
    # event.path       — absolute path to the file (pathlib.Path)
    # event.rel_path    — posix path relative to the sync root
    # event.content_hash — md5 hex; use it as an idempotency key
    # event.type        — "created" / "updated" / "deleted"
    # config["output_dir"] — resolved from the manifest default / install config
    ...
```

Points worth knowing before you write one:

- **`on` in the decorator** picks which event types call this function
  (default `["created", "updated"]`). A file can define several
  `@workflow`-decorated functions; every one whose `on` matches the event
  fires, in the order they're defined.
- **Where it runs**: each run is a fresh `uv run` subprocess — a single-file
  workflow gets its own ephemeral venv from its PEP 723 block; a package
  workflow runs against its own `pyproject.toml`/`uv.lock`. Your workflow
  never shares a process or an import with `notehook` itself.
- **Idempotency is on you.** `event.content_hash` is the recommended key —
  check whether you've already produced output for this exact content
  before doing the work again. Retries, backfills, and loop-guard fallout
  all replay through the same handler.
- **Secrets never touch `config`.** Declare them under
  `[tool.notehook.secrets]` and read them with `secret("name")` — they
  arrive as an environment variable, never in the JSON payload, so logging
  `config` can't leak them.
- **Outcomes are exit codes**, not return values: return normally for
  success; raise `RetryLater("why")` for a transient failure (the canonical
  case is "the other device is offline right now") — the runner reschedules
  it with backoff; raise anything else for a permanent failure — no retry.

See [workflow-spec.md](workflow-spec.md) §2 for the full frozen field list,
and §3 for every manifest key.

## Installing and configuring

```bash
notehook workflows install <git-url-or-local-path> [--as ALIAS] \
    --paths GLOB [--paths GLOB ...] \
    [--input name=value ...] [--secret name=value ...] [--yes]
```

- `<git-url-or-local-path>` — a git URL (cloned, `--depth 1`), a local
  directory (copied — a package-form install), or a local `.py` file
  (copied — a single-file install).
- `--as ALIAS` — defaults to the workflow's manifest `name`. The alias, not
  the workflow name, is the unit of installation: you can install the same
  workflow twice under two aliases with different configs (e.g. two X4s).
- `--paths` is **required** and is the authoritative trigger binding — the
  manifest's `suggested_paths` is only ever an offered default, never
  binding on its own.
- `--yes` skips interactive prompts (needed for scripting/CI); anything
  required that isn't supplied via `--input`/`--secret`/`--paths` then
  fails clearly instead of hanging.

Other commands, all under `notehook workflows`:

| Command | What it does |
|---|---|
| `configure <alias>` | Re-prompt/re-set inputs, secrets, or paths |
| `enable` / `disable <alias>` | Toggle without uninstalling |
| `update <alias>` | `git pull` (git-sourced installs only) + re-validate + prompt for anything newly required |
| `remove <alias>` | Delete the install's code and config (run history in `events.db` is kept regardless) |
| `list` | Table of installs: name, version, enabled, paths, health, last run |
| `run <alias> --path FILE [--wait]` | Manually trigger one install on one file, bypassing its path/type filters. Without `--wait` it just queues the event; with `--wait` it runs immediately and exits nonzero on failure — handy for testing a workflow or for CI |
| `backfill <alias> [--glob G]` | Queue a `created` event for every existing file the install would already match — the replay story for "I just installed this, run it against what's already there" |
| `logs [--alias A] [--failed] [--follow]` | Tail the run log |
| `serve` | The scheduler — run this as a long-lived process |

## Running `serve` as a service

`serve` holds an exclusive lock for as long as it runs — a second `serve` on
the same config directory fails immediately with a clear error instead of
racing the first one.

### systemd (Linux)

```ini
# ~/.config/systemd/user/notehook-workflows.service
[Unit]
Description=notehook workflow runner
After=network-online.target

[Service]
ExecStart=%h/.local/bin/notehook workflows serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now notehook-workflows.service
systemctl --user enable --now notehook-daemon.service   # see below — workflows need sync events to react to
```

You'll usually want `notehook daemon` running too, as its own unit, so
there's something producing events:

```ini
# ~/.config/systemd/user/notehook-daemon.service
[Unit]
Description=notehook sync daemon
After=network-online.target

[Service]
ExecStart=%h/.local/bin/notehook daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

### launchd (macOS)

```xml
<!-- ~/Library/LaunchAgents/com.notehook.workflows.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.notehook.workflows</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/notehook</string>
    <string>workflows</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/notehook-workflows.log</string>
  <key>StandardErrorPath</key><string>/tmp/notehook-workflows.error.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.notehook.workflows.plist
```

Duplicate the plist (a different `Label` and `ProgramArguments` ending in
`daemon` instead of `workflows serve`) for the sync daemon, same as the
systemd example above.

### Tuning

`~/.config/notehook/config.toml`:

```toml
[workflows]
poll_interval_seconds = 2   # how often serve checks for new events
max_parallel = 2            # concurrent jobs; same workflow on different files can run in parallel
retention_days = 90         # how long run/event history is kept
```

## Avoiding trigger loops

A workflow that writes its output *into* a watched folder will see its own
output as a new sync event. In order of preference:

1. Set `skip_own_changes = true` in the install config (or pass it via
   `configure`) — drops events this client itself originated, so the
   workflow only reacts to changes made elsewhere (typically the device).
2. Bind a narrow `--paths` glob that doesn't cover the output location.
3. Write output outside the sync root entirely.
4. As a last resort, the idempotency rule (hash-keyed skip, see "Writing a
   workflow" above) makes any remaining loop converge after one extra
   round-trip instead of running away.

## Worked example: push to an Xteink X4

The X4 (CrossPoint firmware) exposes HTTP REST on port 80 on the LAN. This
is the full workflow from [workflow-spec.md](workflow-spec.md) §9:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "websockets"]
#
# [tool.notehook]
# name = "push-to-x4"
# suggested_paths = ["Note/ToReader/**"]
# [tool.notehook.inputs]
# device_ip = { required = true }
# ///
from notehook_workflow import workflow, RetryLater
import requests

@workflow(on=["created", "updated"])
def run(event, config):
    marker = event.path.with_suffix(event.path.suffix + f".{event.content_hash[:8]}.sent")
    if marker.exists():          # idempotency: this exact content already pushed
        return
    try:
        upload_to_x4(event.path, config["device_ip"])   # REST/WS per api.html
    except (requests.ConnectionError, requests.Timeout) as e:
        raise RetryLater(f"X4 unreachable: {e}") from e
    marker.write_text("")
```

```bash
notehook workflows install ./push-to-x4.py \
    --paths "Note/ToReader/**" \
    --input device_ip=192.168.1.50
notehook workflows serve
```

(A real implementation would keep the `.sent` markers out of the sync root
— see "Avoiding trigger loops" above — this sketch just shows the API
shape.)
