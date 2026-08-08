#!/usr/bin/env bash
# Update digi-fridge on the VPS from GitHub (no PAT needed after deploy key setup).
#
#   sudo bash /opt/digi-fridge/deploy/update.sh

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/digi-fridge}"
SERVICE_USER="${SERVICE_USER:-digifridge}"
BRANCH="${BRANCH:-main}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

if [[ ! -d "${APP_DIR}/.git" ]]; then
  echo "No git checkout at ${APP_DIR}." >&2
  exit 1
fi

if [[ -f "${APP_DIR}/.git-ssh-env" ]]; then
  # shellcheck disable=SC1091
  source "${APP_DIR}/.git-ssh-env"
fi

echo "==> Pulling ${BRANCH}"
sudo -u "${SERVICE_USER}" env GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-}" \
  git -C "${APP_DIR}" fetch origin
sudo -u "${SERVICE_USER}" env GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-}" \
  git -C "${APP_DIR}" checkout "${BRANCH}"
sudo -u "${SERVICE_USER}" env GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-}" \
  git -C "${APP_DIR}" pull --ff-only origin "${BRANCH}"

echo "==> Refreshing Python deps"
sudo -u "${SERVICE_USER}" "${APP_DIR}/.venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"

echo "==> Restarting service"
systemctl daemon-reload
systemctl restart digi-fridge
systemctl --no-pager --full status digi-fridge || true

echo
echo "Updated to: $(sudo -u "${SERVICE_USER}" git -C "${APP_DIR}" rev-parse --short HEAD)"
echo "Logs: journalctl -u digi-fridge -f"
