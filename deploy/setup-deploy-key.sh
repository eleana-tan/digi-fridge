#!/usr/bin/env bash
# One-time: create a read-only SSH deploy key for private-repo updates.
#
# Run on the VPS as root (or with sudo):
#   sudo bash /opt/digi-fridge/deploy/setup-deploy-key.sh
#
# Then paste the printed public key into GitHub:
#   Repo → Settings → Deploy keys → Add deploy key (read-only)

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/digi-fridge}"
SERVICE_USER="${SERVICE_USER:-digifridge}"
KEY_DIR="${APP_DIR}/.ssh"
KEY_FILE="${KEY_DIR}/deploy_key"
SSH_CONFIG="${KEY_DIR}/config"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

if [[ ! -d "${APP_DIR}" ]]; then
  echo "App not found at ${APP_DIR}. Install the bot first." >&2
  exit 1
fi

mkdir -p "${KEY_DIR}"
chmod 700 "${KEY_DIR}"

if [[ ! -f "${KEY_FILE}" ]]; then
  echo "==> Generating ed25519 deploy key"
  ssh-keygen -t ed25519 -N "" -C "digi-fridge-deploy@$(hostname)" -f "${KEY_FILE}"
else
  echo "==> Deploy key already exists at ${KEY_FILE}"
fi

cat > "${SSH_CONFIG}" <<EOF
Host github.com
  HostName github.com
  User git
  IdentityFile ${KEY_FILE}
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
chmod 600 "${SSH_CONFIG}" "${KEY_FILE}"
chmod 644 "${KEY_FILE}.pub"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${KEY_DIR}"

# Point git at SSH and make the service user use this SSH config.
GIT_SSH_COMMAND="ssh -F ${SSH_CONFIG}"
sudo -u "${SERVICE_USER}" git -C "${APP_DIR}" remote set-url origin git@github.com:eleana-tan/digi-fridge.git

# Persist GIT_SSH_COMMAND for the digifridge user via a tiny wrapper env file
# used by update.sh (and optional for interactive pulls).
echo "export GIT_SSH_COMMAND='ssh -F ${SSH_CONFIG}'" > "${APP_DIR}/.git-ssh-env"
chown "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}/.git-ssh-env"
chmod 600 "${APP_DIR}/.git-ssh-env"

echo
echo "============================================================"
echo " Copy EVERYTHING below this line into GitHub:"
echo "   digi-fridge → Settings → Deploy keys → Add deploy key"
echo "   Title: hetzner-vps"
echo "   Allow write access: NO (leave unchecked)"
echo "============================================================"
echo
cat "${KEY_FILE}.pub"
echo
echo "============================================================"
echo " After saving the key on GitHub, test with:"
echo "   sudo -u ${SERVICE_USER} bash -lc 'source ${APP_DIR}/.git-ssh-env && git -C ${APP_DIR} fetch origin'"
echo " Then updates are:"
echo "   sudo bash ${APP_DIR}/deploy/update.sh"
echo "============================================================"
