#!/usr/bin/env bash
# Provision a fresh Hetzner Cloud server for Indi. Ubuntu 24.04, run as root.
#
#   ssh root@<server-ip> 'bash -s' < setup-hetzner.sh
#
# Idempotent: safe to re-run.

set -euo pipefail

INDI_USER="${INDI_USER:-indi}"
INDI_HOME="/home/${INDI_USER}"
HARNESS_DIR="${INDI_HOME}/harness"
WORKSPACE_DIR="${INDI_HOME}/workspace"
WORKSPACE_OUT="${INDI_HOME}/workspace-out"

echo "==> Base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -yqq python3 python3-venv python3-pip git curl ca-certificates \
    unattended-upgrades fail2ban ufw

echo "==> Automatic security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> Firewall: outbound only, plus SSH"
# Indi makes outbound connections (Gmail, Sheets, Anthropic). Nothing needs
# to reach it from outside except you, over SSH. A box that listens on
# nothing is a box with almost no attack surface.
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'ssh'
ufw --force enable

echo "==> Docker (execution sandbox)"
if ! command -v docker >/dev/null; then
    curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

echo "==> User ${INDI_USER}"
if ! id -u "${INDI_USER}" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "Indi Artificial" "${INDI_USER}"
fi
usermod -aG docker "${INDI_USER}"

# Give indi the same SSH keys root was provisioned with, so you can scp and
# rsync straight into its home instead of copying via /root and chowning
# afterwards. Deliberately NOT adding indi to the sudo group: it only needs
# to run Python in its own home and talk to the Docker socket, and an
# account that can't escalate is worth the small inconvenience.
if [ -f /root/.ssh/authorized_keys ]; then
    install -d -o "${INDI_USER}" -g "${INDI_USER}" -m 700 "${INDI_HOME}/.ssh"
    install -o "${INDI_USER}" -g "${INDI_USER}" -m 600 \
        /root/.ssh/authorized_keys "${INDI_HOME}/.ssh/authorized_keys"
    echo "    SSH keys copied to ${INDI_USER}; 'ssh ${INDI_USER}@<ip>' will work."
else
    echo "    WARNING: no /root/.ssh/authorized_keys found, so ${INDI_USER} has"
    echo "    no SSH access. Log in with: sudo -iu ${INDI_USER}"
fi

# Indi's own user, separate from yours. Its files are its own, and a
# mistake on its side stays on its side.
install -d -o "${INDI_USER}" -g "${INDI_USER}" -m 700 "${HARNESS_DIR}"
install -d -o "${INDI_USER}" -g "${INDI_USER}" -m 700 "${WORKSPACE_DIR}"
install -d -o "${INDI_USER}" -g "${INDI_USER}" -m 700 "${WORKSPACE_OUT}"

echo "==> Python environment"
sudo -u "${INDI_USER}" python3 -m venv "${INDI_HOME}/venv"
sudo -u "${INDI_USER}" "${INDI_HOME}/venv/bin/pip" install --quiet --upgrade pip
sudo -u "${INDI_USER}" "${INDI_HOME}/venv/bin/pip" install --quiet \
    anthropic google-api-python-client google-auth-oauthlib python-dotenv pytest

echo "==> Pre-pulling the sandbox image"
docker pull -q python:3.11-slim || true

echo "==> systemd units"
cp indi.service indi.timer indi-heartbeat.service indi-heartbeat.timer \
   /etc/systemd/system/ 2>/dev/null || \
   echo "    (run this from the deploy/ directory to install the units)"
systemctl daemon-reload

cat <<EOF

==> Base system ready.

Next, from your own machine:

  1. Copy the harness up:
       rsync -av --exclude '.git' --exclude '__pycache__' \\
             ./indi/ ${INDI_USER}@<server-ip>:${HARNESS_DIR}/

  2. Complete the Google OAuth flow LOCALLY (it needs a browser), then
     copy the token up. Do not try to run the flow on the server.
       scp token.json credentials.json ${INDI_USER}@<server-ip>:${HARNESS_DIR}/

  3. Put the API key in place:
       ssh ${INDI_USER}@<server-ip>
       echo 'sk-ant-...' > ${HARNESS_DIR}/api_key.txt
       chmod 600 ${HARNESS_DIR}/api_key.txt ${HARNESS_DIR}/token.json \\
                 ${HARNESS_DIR}/credentials.json

  4. Create .env from .env.example and edit it.

  5. Dry run first:
       ${INDI_HOME}/venv/bin/python ${HARNESS_DIR}/check_and_reply.py --dry-run

  6. Start the timers (as root — ${INDI_USER} has no sudo, by design):
       ssh root@<server-ip> 'systemctl enable --now indi.timer indi-heartbeat.timer'
       ssh root@<server-ip> "systemctl list-timers 'indi*'"

  Logs:  journalctl -u indi -f
         tail -f ${HARNESS_DIR}/log.txt

EOF
