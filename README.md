# gfff
git fetch and forward

Fetches from remote and returns true or false.
```
gfff && echo "Updates available!" || echo "No updates"
```
See ```rsb.sh``` for a rust/cargo build example.

## Python buildbot (`pueue`)

`gfff-buildbot` reads active entries from `gfff.yaml` and schedules one recurring
`pueue` group per project. Each queued task does:

1. `git fetch`
2. compare local head with configured `git-remote-ref` (default `@{u}`)
3. if changed: run configured `git-pull` (default `git pull --ff-only`), optional `cleanup`, optional `pre-build`, `build`, optional `post-build`

Useful per-job git options:

- `git-pull`: custom pull command per job, for example `git pull origin main --ff-only`
- `git-remote-ref`: revision used for update detection, for example `origin/main`
- `git-strict`: when `false`, fetch/pull failures skip that run instead of failing the task

Example:

```yaml
- name: my-repo
	active: true
	path: ~/dev/my-repo
	git-remote-ref: origin/release
	git-pull: git pull origin release --ff-only
	build: make
	interval: 3600
```

### Requirements

- `pueue`
- `git`
- Python 3

### Install Shell Scripts

Use `inst.sh` when you only want the helper shell scripts used by other workflows:

```bash
bash inst.sh
```

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
