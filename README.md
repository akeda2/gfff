# gfff

Git-aware build scheduler driven by `gfff.yaml` and `pueue`.

## Python buildbot (`pueue`)

`gfff-buildbot` reads active entries from `gfff.yaml` and schedules one recurring
shared `pueue` group for all projects.

Before queueing a build, the scheduler process (not `pueue`) does:

1. `git fetch`
2. compare local head with configured `git-remote-ref` (default `@{u}`)
3. if changed: run configured `git-pull` (default `git pull --ff-only`)

Only the build phase is queued in `pueue`:

1. optional `cleanup`
2. optional `pre-build`
3. optional `test`
4. optional `build`
5. optional `post-build`

`test` runs before `build`. Since the script uses `set -e`, `build` only runs when `test` succeeds.
If a repo is test-only, omit `build` and set only `test`.

Useful per-job git options:

- `git-pull`: custom pull command per job, for example `git pull origin main --ff-only`
- `git-remote-ref`: revision used for update detection, for example `origin/main`
- `git-strict`: when `false`, fetch/pull failures skip that run instead of failing the task
- `manual-install-cmd`: optional manual install command (for example sudo install steps) that is logged as an explicit action after a successful build task

Logging now includes:

- when no git updates are found for a job
- git/pull related errors
- when a job is added to `pueue` (including task id when available)
- queued task outcome (`Done` is success only with explicit `result: Success`; other `Done` results are logged as errors)
- required manual action line with the exact `manual-install-cmd`

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

At least one of `test` or `build` must be set for an active job.

### Requirements

- `pueue`
- `git`
- Python 3

### Install Buildbot (Package + Service)

Use `install-buildbot.sh` to install or upgrade the Python package and install/update the user service in one step:

```bash
bash install-buildbot.sh
```

This script will:

1. create/update a dedicated venv at `~/.local/share/gfff-buildbot/.venv`
2. install/upgrade the `gfff-buildbot` package (including `PyYAML`) into that venv
3. install/update `~/.config/systemd/user/gfff-buildbot.service`
4. reload user systemd and enable/start (or restart) the service

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

Use the venv-installed command:

```bash
~/.local/share/gfff-buildbot/.venv/bin/gfff-buildbot
```

Useful flags:

```bash
# Queue all active jobs once, then exit
~/.local/share/gfff-buildbot/.venv/bin/gfff-buildbot --once

# Preview pueue commands without running them
~/.local/share/gfff-buildbot/.venv/bin/gfff-buildbot --dry-run
```

### Tests

Run the buildbot unit tests:

```bash
python3 -m unittest discover -s tests -q
```

In CI or other isolated environments, run with an explicit interpreter path, for example:

```bash
./.venv/bin/python -m unittest discover -s tests -q
```

### Legacy User-Site Install (may fail on modern Ubuntu)

```bash
python3 -m pip install --user --upgrade .
```

This installs the `gfff-buildbot` command and its Python dependency (`PyYAML`).
If `~/.local/bin` is not in your `PATH`, add it first.

### Manual Service Setup (if you prefer)

Make sure `ExecStart` in [gfff-buildbot.service](gfff-buildbot.service#L9) points at your chosen install location.

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

- The unit expects this repo at `%h/dev/gfff`.
- The service runs `%h/.local/share/gfff-buildbot/.venv/bin/gfff-buildbot`.

## Legacy Shell Scripts

The shell helpers remain available for older workflows.

### gfff Script

Fetches from remote and returns true or false:

```bash
gfff && echo "Updates available!" || echo "No updates"
```

### Rust Build Example

See `rsb.sh` for a rust/cargo build example.

### Install Legacy Shell Scripts

Use `inst.sh` when you only want the helper shell scripts used by legacy workflows:

```bash
bash inst.sh
```
