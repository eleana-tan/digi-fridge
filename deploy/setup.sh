#!/usr/bin/env bash
# Install / update digi-fridge on a Hetzner (or any Debian/Ubuntu) VPS.
#
# Usage (as root):
#   curl -fsSL https://raw.githubusercontent.com/eleana-tan/digi-fridge/main/deploy/setup.sh | bash
# Or, from a cloned checkout:
#   sudo bash deploy/setup.sh
#
# Then edit /opt/digi-fridge/.env and: systemctl restart digi-fridge

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/eleana-tan/digi-fridge.git}"
APP_DIR="${APP_DIR:-/opt/digi-fridge}"
DATA_DIR="${DATA_DIR:-/var/lib/digi-fridge}"
SERVICE_USER="${SERVICE_USER:-digifridge}"
BRANCH="${BRANCH:-main}"
# For private repos: clone once as your user (git will prompt for username + PAT),
# then install with: sudo LOCAL_SRC=/tmp/digi-fridge-src bash deploy/setup.sh
LOCAL_SRC="${LOCAL_SRC:-}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script as root (sudo)." >&2
  exit 1
fi

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates

echo "==> Creating service user: ${SERVICE_USER}"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> Creating directories"
mkdir -p "${APP_DIR}" "${DATA_DIR}"
chown "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}"

if [[ -n "${LOCAL_SRC}" ]]; then
  echo "==> Installing from local source: ${LOCAL_SRC}"
  if [[ ! -d "${LOCAL_SRC}" ]]; then
    echo "LOCAL_SRC does not exist: ${LOCAL_SRC}" >&2
    exit 1
  fi
  # Preserve an existing .env across reinstalls.
  if [[ -f "${APP_DIR}/.env" ]]; then
    cp "${APP_DIR}/.env" /tmp/digi-fridge.env.bak
  fi
  rm -rf "${APP_DIR}"
  mkdir -p "${APP_DIR}"
  cp -a "${LOCAL_SRC}/." "${APP_DIR}/"
  if [[ -f /tmp/digi-fridge.env.bak ]]; then
    mv /tmp/digi-fridge.env.bak "${APP_DIR}/.env"
  fi
  # Drop any embedded credentials from the remote URL if present.
  if [[ -d "${APP_DIR}/.git" ]]; then
    git -C "${APP_DIR}" remote set-url origin "https://github.com/eleana-tan/digi-fridge.git" || true
  fi
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
elif [[ -d "${APP_DIR}/.git" ]]; then
  echo "==> Updating existing checkout"
  sudo -u "${SERVICE_USER}" git -C "${APP_DIR}" fetch origin
  sudo -u "${SERVICE_USER}" git -C "${APP_DIR}" checkout "${BRANCH}"
  sudo -u "${SERVICE_USER}" git -C "${APP_DIR}" pull --ff-only origin "${BRANCH}"
else
  echo "==> Cloning ${REPO_URL}"
  if [[ -z "$(ls -A "${APP_DIR}" 2>/dev/null || true)" ]]; then
    git clone --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
  else
    echo "APP_DIR ${APP_DIR} is not empty and not a git repo. Aborting." >&2
    echo "For a private repo, clone as your user then re-run with LOCAL_SRC=..." >&2
    exit 1
  fi
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
fi

echo "==> Python venv + dependencies"
sudo -u "${SERVICE_USER}" python3 -m venv "${APP_DIR}/.venv"
sudo -u "${SERVICE_USER}" "${APP_DIR}/.venv/bin/pip" install --upgrade pip -q
sudo -u "${SERVICE_USER}" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "==> Creating .env from example (YOU MUST EDIT THIS)"
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  # Point the DB at the persistent data dir
  if grep -q '^DB_PATH=' "${APP_DIR}/.env"; then
    sed -i "s|^DB_PATH=.*|DB_PATH=${DATA_DIR}/fridge.db|" "${APP_DIR}/.env"
  else
    echo "DB_PATH=${DATA_DIR}/fridge.db" >> "${APP_DIR}/.env"
  fi
  chown "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
else
  echo "==> Keeping existing .env"
fi

echo "==> Installing systemd unit"
cp "${APP_DIR}/deploy/digi-fridge.service" /etc/systemd/system/digi-fridge.service
systemctl daemon-reload
systemctl enable digi-fridge.service

echo
echo "============================================================"
echo " Almost done. Edit secrets, then start the bot:"
echo
echo "   nano ${APP_DIR}/.env"
echo "   # set TELEGRAM_BOT_TOKEN and OPENAI_API_KEY at minimum"
echo
echo "   systemctl restart digi-fridge"
echo "   systemctl status digi-fridge"
echo "   journalctl -u digi-fridge -f"
echo
echo " Stop any local laptop copy of the bot — only ONE process"
echo " should poll with the same TELEGRAM_BOT_TOKEN."
echo "============================================================"
