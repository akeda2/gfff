#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${HOME}/.local/share/gfff-buildbot/.venv"
USER_BIN_DIR="${HOME}/.local/bin"
BOT_CMD_SRC="${VENV_DIR}/bin/gfff-buildbot"
BOT_CMD_LINK="${USER_BIN_DIR}/gfff-buildbot"
BOT_ALIAS_SRC="${VENV_DIR}/bin/gb"
BOT_ALIAS_LINK="${USER_BIN_DIR}/gb"
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

mkdir -p "${USER_BIN_DIR}"
ln -sfn "${BOT_CMD_SRC}" "${BOT_CMD_LINK}"
ln -sfn "${BOT_ALIAS_SRC}" "${BOT_ALIAS_LINK}"
echo "gfff-buildbot command link install/update SUCCESS! (${BOT_CMD_LINK} -> ${BOT_CMD_SRC})"
echo "gb command link install/update SUCCESS! (${BOT_ALIAS_LINK} -> ${BOT_ALIAS_SRC})"

case ":${PATH}:" in
    *":${USER_BIN_DIR}:"*) ;;
    *)
        echo "WARNING: ${USER_BIN_DIR} is not in PATH for this shell."
        echo "         Add it to your shell profile to run 'gfff-buildbot' directly."
        ;;
esac

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
