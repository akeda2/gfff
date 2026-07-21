#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${HOME}/.local/share/gfff-buildbot/.venv"
SERVICE_SRC="${SCRIPT_DIR}/gfff-buildbot.service"
SERVICE_DST_DIR="${HOME}/.config/systemd/user"
SERVICE_DST="${SERVICE_DST_DIR}/gfff-buildbot.service"

cd "${SCRIPT_DIR}"

if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "ERROR: python3 venv support is missing. Install the OS package for venv support (e.g. python3-venv) and retry."
    exit 1
fi

python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel
"${VENV_DIR}/bin/python" -m pip install --upgrade .
echo "gfff-buildbot venv package install SUCCESS!"

mkdir -p "${SERVICE_DST_DIR}"
install -m 644 "${SERVICE_SRC}" "${SERVICE_DST}"
echo "gfff-buildbot.service install/update SUCCESS!"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    if systemctl --user is-enabled --quiet gfff-buildbot.service; then
        systemctl --user restart gfff-buildbot.service
        echo "gfff-buildbot.service restart SUCCESS!"
    else
        systemctl --user enable --now gfff-buildbot.service
        echo "gfff-buildbot.service enable/start SUCCESS!"
    fi
else
    echo "WARNING: systemctl not found. Service file was installed, but service was not reloaded or started."
fi

echo "Done. Check status with: systemctl --user status gfff-buildbot.service"
