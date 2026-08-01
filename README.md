# gfff

Simple build scheduler

## Quick Start

1. Install/update package + user service:

```bash
bash install-buildbot.sh
```

2. Put job configs in `~/.config/gfff/` (or keep `./gfff.yaml` for local runs).

3. Validate config:

```bash
gb --check ~/.config/gfff/gfff.yaml
```

4. Queue one run immediately:

```bash
gb --once --force
```

5. Check service and logs:

```bash
systemctl --user status gfff-buildbot.service
journalctl --user -u gfff-buildbot.service -f
```

## Python Buildbot

`gfff-buildbot` reads active entries from `gfff.yaml` and schedules one recurring
shared `pueue` group for all projects.

On startup, `gfff-buildbot` sets the `gfff` pueue group parallelism to the
detected CPU thread count.

You can still adjust concurrency with pueue commands (globally or for the
selected group) based on your machine capacity.

Example pueue concurrency commands:

```bash
# Show current parallelism (global and groups)
pueue parallel

# Set parallelism for the shared gfff group
pueue parallel -g gfff 4

# Set global default parallelism
pueue parallel 8
```

Before queueing a build, the internal scheduler process does:

1. `git fetch`
2. compare local head with configured `git-remote-ref` (default `@{u}`)
3. if changed: run configured `git-pull` (default `git pull --ff-only`)

Exception: jobs configured with both `run-mode: scheduled` and `at` are treated
as clock-driven actions and are queued at the configured time without git update
checks.

The build phase is queued in `pueue`:

1. optional `cleanup`
2. optional `pre-build`
3. optional `test`
4. optional `build`
5. optional `post-build`

`test` runs before `build`. Since the script uses `set -e`, `build` only runs when `test` succeeds.
If a repo is test-only, omit `build` and set only `test`.

`cleanup`, `pre-build`, `test`, `build`, and `post-build` accept either:

- a single command string
- a YAML list of commands (run in listed order)

Scheduling supports two modes per active job:

- `interval`: run every N seconds
- `at`: run once daily at a fixed time in local time, for example `05:00`

Tip: quote `at` values in YAML (for example `at: "11:25"`) to avoid YAML parser
time/sexagesimal coercion on some systems.

Useful per-job git options:

- `git-pull`: custom pull command per job, for example `git pull origin main --ff-only`
- `git-remote-ref`: revision used for update detection, for example `origin/main`
- `git-strict`: when `false`, fetch/pull failures skip that run instead of failing the task
- `manual-install-cmd`: optional manual install command (for example sudo install steps) that is logged as an explicit action after a successful build task

Optional per-job run mode:

- `run-mode: normal` (default when omitted): run in both scheduler mode and `--once`
- `run-mode: manual`: run only when invoked manually with `--once`
- `run-mode: scheduled`: run in scheduler mode (`at`/`interval` loop). It is skipped by plain `--once`, but allowed with `--once <job-name>` when explicitly targeted.

Optional per-job one-shot deactivation:

- `disable-when-run: true`: before running `test`/`build`, flip that job's `active: true` to `active: false` in the source config file where the job was loaded from.

Logging now includes:

- when no git updates are found for a job
- git/pull related errors
- when a job is added to `pueue` (including task id when available)
- queued task outcome (`Done` is success only with explicit `result: Success`; other `Done` results are logged as errors)
- required manual action line with the exact `manual-install-cmd`
- startup/reload config visibility (`currently loaded configs`)
- next-run timestamps for `at` jobs during startup/reload scheduling

Example:

```yaml
- name: my-repo
	active: true
	path: ~/dev/my-repo
	git-remote-ref: origin/release
	git-pull: git pull origin release --ff-only
	test: pytest -q --tb=short
	build: make
	manual-install-cmd: sudo make install
	interval: 3600
```

Multi-step hook example:

```yaml
- name: hook-heavy-repo
	active: true
	path: ~/dev/hook-heavy-repo
	cleanup:
	  - git clean -fdx
	  - rm -rf .pytest_cache
	pre-build:
	  - ./scripts/bootstrap.sh
	  - ./scripts/generate-config.sh
	test: pytest -q
	build: make release
	post-build:
	  - ./scripts/publish-artifacts.sh
	  - ./scripts/notify.sh
	interval: 3600
```

Daily schedule example:

```yaml
- name: morning-test
	active: true
	path: ~/dev/my-repo
	test: pytest -q
	at: 05:00
```

Manual-only example (skip normal scheduler loop):

```yaml
- name: on-demand-rebuild
	active: true
	path: ~/dev/my-repo
	build: make clean all
	run-mode: manual
	interval: 3600
```

Scheduled-only example (skipped by plain `--once`, but can be targeted with `gb -o pueue-restart`):

```yaml
- name: pueue-restart
	active: true
	disable-when-run: true
	path: ~/dev/pueue
	test: systemctl --user restart pueued.service
	run-mode: scheduled
	at: 04:00
```

At least one of `test` or `build` must be set for an active job.
Exactly one of `interval` or `at` must be set for an active job.
If `run-mode` is omitted, behavior is unchanged from previous versions.

### Requirements

- `pueue`. https://github.com/Nukesor/pueue or an unofficial fork: https://github.com/akeda2/pueue.git with some additions for the status command. The the official version is the recommended.
- `git`
- Python 3

If `pueue` works in your shell but the user service logs `pueue is not installed or not in PATH`,
the service environment is usually missing user-level PATH entries.
This project now checks common fallback locations as well:

- `~/.cargo/bin/pueue`
- `~/.local/bin/pueue`
- `/usr/local/bin/pueue`
- `/usr/bin/pueue`

The provided [gfff-buildbot.service](gfff-buildbot.service) also sets an explicit PATH
that includes `~/.cargo/bin` and `~/.local/bin`.

### Install Buildbot (Package + Service)

Use `install-buildbot.sh` to install or upgrade the Python package and install/update the user service in one step:

```bash
bash install-buildbot.sh
```

This script will:

1. create/update a dedicated venv at `~/.local/share/gfff-buildbot/.venv`
2. install/upgrade the `gfff-buildbot` package (including `PyYAML`) into that venv
3. create/update `~/.local/bin/gfff-buildbot` as a symlink to the venv command
4. create/update `~/.local/bin/gb` as a symlink to the short command alias
5. create `~/.config/gfff/` if it does not exist
6. install/update `~/.config/systemd/user/gfff-buildbot.service`
7. reload user systemd and enable/start (or restart) the service

This avoids `--break-system-packages` on modern Ubuntu and other PEP 668 environments.

### Manual Package Install

If you want a manual install without touching system Python, use a venv:

```bash
python3 -m venv ~/.local/share/gfff-buildbot/.venv
~/.local/share/gfff-buildbot/.venv/bin/python -m pip install --upgrade pip setuptools wheel
~/.local/share/gfff-buildbot/.venv/bin/python -m pip install --upgrade .
```

### Manual Dev Install

```bash
python3 -m venv ~/.local/share/gfff-buildbot/.venv
~/.local/share/gfff-buildbot/.venv/bin/python -m pip install --upgrade pip setuptools wheel
~/.local/share/gfff-buildbot/.venv/bin/python -m pip install --upgrade -e .
```

### Run

Primary command (recommended):

```bash
gb
```

Equivalent full command:

```bash
gfff-buildbot
```

### Config Discovery

If `--config` is not provided, `gfff-buildbot` searches and merges configs in this order:

1. `./gfff.yaml` (current directory)
2. local user config directory `~/.config/gfff/`:
	first `gfff.yaml`, then other `*.yaml` files in lexical order (for example `10firstlist.yaml`, `30secondlist.yaml`)
3. development fallback config from user service `ExecStart --config` (if available)
4. `~/dev/gfff/gfff.yaml` (final fallback if service does not define a config path)

The shipped user service intentionally starts in `%h` (home) and does not pass
`--config`, so `~/.config/gfff/*.yaml` is used by default while `~/dev/gfff/gfff.yaml`
remains only a fallback source.

In scheduler mode, merged config files are reloaded periodically (default every 60 seconds)
so changes are picked up without restarting the service. Use
`--reload-config-seconds N` to change this interval, or `0` to disable periodic reload.

For `at: HH:MM` jobs, the scheduler applies a small catch-up window equal to
`reload-config-seconds`: if the job is first seen shortly after today's target minute,
it runs immediately instead of waiting until tomorrow.

If a daily `at` job hits a runtime error during that run (for example DNS/network not
yet ready right after boot), the scheduler retries after a short delay instead of
waiting until the next day. The default delay is 300 seconds and can be configured
with `--at-error-retry-seconds` (`0` disables this fast retry).

Interval jobs get similar runtime-error fallback behavior: after a job-level runtime
error, the scheduler retries after `--error-retry-seconds` (default 300) instead of
always waiting the full interval.

Important behavior:

- If the current directory is `~/dev/gfff`, the dev config is still treated as the last source.
- All found configs are merged in order.
- If a later config contains a job with the same `name` as an earlier config, the later one is ignored.

This makes `~/.config/gfff/` the recommended place for user-local defaults and layered config files.

Optional flags for discovery behavior:

- `--no-dev-fallback`: ignore the development fallback config in auto-discovery.
- `--dev-fallback-config /path/to/gfff.yaml`: use a custom development fallback config path instead of `~/dev/gfff/gfff.yaml`.

Direct venv path also works:

```bash
~/.local/share/gfff-buildbot/.venv/bin/gfff-buildbot
```

Useful flags:

```bash
# Queue all active jobs once, then exit
gb --once

# Queue a specific scheduled job once (explicit manual override for run-mode: scheduled)
gb --once pueue-restart

# Queue all jobs once (including active: false) even if git has no updates
gb --once --force

# Disable each queued job in its config during this run
gb --once --disable-when-run

# Preview pueue commands without running them
gb --dry-run

# Short flag aliases
gb -n -o -f

# Reload merged config files every 30 seconds in scheduler mode
gb --reload-config-seconds 30

# Retry failed daily at-jobs after 5 minutes (default is 300)
gb --at-error-retry-seconds 300

# Retry failed interval jobs quickly (default is 300)
gb --error-retry-seconds 300

# Validate a config file
gb --check /path/to/configfile.yaml
gb -C /path/to/configfile.yaml

# Validate and import into ~/.config/gfff/
gb --import /path/to/configfile.yaml
gb -I /path/to/configfile.yaml

# Overwrite existing target file during import
gb --import /path/to/configfile.yaml --overwrite
gb -I /path/to/configfile.yaml -w

# Import and rewrite all job path fields to the source config directory
gb --import /path/to/configfile.yaml --import-adjust-paths
gb -I /path/to/configfile.yaml --import-adjust-paths
gb -I /path/to/configfile.yaml -a

# Full command works the same way
gfff-buildbot --once --force
```

Common day-to-day commands:

```bash
# Run scheduler in foreground
gb

# Queue one named job now
gb --once name-of-list-entry

# Queue one named job now, even if no git updates were detected
gb --once --force name-of-list-entry

# Preview actions only
gb --dry-run --once
```

Run only one specific job by exact `name` (config discovery order is unchanged):

```bash
gb name-of-list-entry
gb -o name-of-list-entry
gb -o -f name-of-list-entry
```

Short option aliases:

- `-c` for `--config`
- `-g` for `--group-prefix`
- `-t` for `--tick`
- `-o` for `--once`
- `-n` for `--dry-run`
- `-f` for `--force`

Additional scheduler option:

- `--reload-config-seconds` controls periodic config reload interval in scheduler mode (default: `60`, `0` disables reload)
- `--error-retry-seconds` controls fast retry delay for failed interval jobs (default: `300`, `0` disables fast retry)
- `--at-error-retry-seconds` controls fast retry delay for failed daily `at` jobs (default: `300`, `0` disables fast retry)

`--force` bypasses update-detection gating: it still runs `git fetch` and `git pull`,
then queues the run even when no updates were found.
When used with `--once`, it also bypasses the `active: false` filter and includes
inactive config entries in that one-shot run.
This is intended for interactive/manual triggering.

### Tests

Run the buildbot unit tests:

```bash
python3 -m unittest discover -s tests -q
```

In CI or other isolated environments, run with an explicit interpreter path, for example:

```bash
./.venv/bin/python -m unittest discover -s tests -q
```

### Optional User-Site Install (may fail on PEP 668 distros)

```bash
python3 -m pip install --user --upgrade .
```

This installs the `gfff-buildbot` command and its Python dependency (`PyYAML`).
If `~/.local/bin` is not in your `PATH`, add it first.

### Manual Service Setup (if you prefer)

Make sure `ExecStart` in [gfff-buildbot.service](gfff-buildbot.service) points at your chosen install location.

```bash
mkdir -p ~/.config/systemd/user
cp gfff-buildbot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gfff-buildbot.service
```

### Check Status and Logs

```bash
systemctl --user status gfff-buildbot.service
journalctl --user -u gfff-buildbot.service -f
```

Notes:

- The unit runs with `WorkingDirectory=%h` and starts `gfff-buildbot` from the venv path shown in `ExecStart`.
- The provided service does not pass `--config`, so default config discovery applies (`~/.config/gfff/*.yaml` is the primary source).
- The unit orders startup after `network-online.target` to reduce boot-time DNS/network race conditions.
