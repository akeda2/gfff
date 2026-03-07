# gfff
git fetch and forward

Fetches from remote and returns true or false.
```
gfff && echo "Updates available!" || echo "No updates"
```
See ```rsb.sh``` for a rust/cargo build example.

## Python buildbot (`pueue`)

`buildbot.py` reads active entries from `gfff.yaml` and schedules one recurring
`pueue` group per project. Each queued task does:

1. `git fetch`
2. compare local head with upstream (`@{u}`)
3. if changed: `git pull --ff-only`, optional `cleanup`, optional `pre-build`, `build`, optional `post-build`

### Requirements

- `pueue`
- `git`
- Python 3
- `PyYAML` (`pip install pyyaml`)

### Run

```bash
python3 buildbot.py
```

Useful flags:

```bash
# Queue all active jobs once, then exit
python3 buildbot.py --once

# Preview pueue commands without running them
python3 buildbot.py --dry-run
```

## Run As A `systemd --user` Service

This repo includes `gfff-buildbot.service` for user-level systemd.

1. Install and enable the user service:

```bash
mkdir -p ~/.config/systemd/user
cp gfff-buildbot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gfff-buildbot.service
```

2. Check status and logs:

```bash
systemctl --user status gfff-buildbot.service
journalctl --user -u gfff-buildbot.service -f
```

Notes:

- The unit expects this repo at `%h/dev/gfff`.
- The service runs `/usr/bin/python3 %h/dev/gfff/buildbot.py` directly.
