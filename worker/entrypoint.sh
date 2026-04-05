#!/usr/bin/env bash
set -eu

# Create node user if it doesn't exist (Codex sandbox runs as node)
if ! id node &>/dev/null; then
    useradd -m -s /bin/bash node
fi

# Copy SSH keys for both root and node users
for home_dir in /root /home/node; do
    mkdir -p "${home_dir}/.ssh"
    if [ -f /tmp/ssh-keys/id_ed25519 ]; then
        cp /tmp/ssh-keys/id_ed25519 "${home_dir}/.ssh/id_ed25519"
        cp /tmp/ssh-keys/id_ed25519.pub "${home_dir}/.ssh/id_ed25519.pub" 2>/dev/null || true
        cp /tmp/ssh-keys/known_hosts "${home_dir}/.ssh/known_hosts" 2>/dev/null || true
        chmod 700 "${home_dir}/.ssh"
        chmod 600 "${home_dir}/.ssh/id_ed25519"
        # Write SSH config with correct path for each user
        cat > "${home_dir}/.ssh/config" <<SSHEOF
Host github.com
    IdentityFile ${home_dir}/.ssh/id_ed25519
    StrictHostKeyChecking accept-new
    User git
SSHEOF
        chmod 600 "${home_dir}/.ssh/config"
    fi
done

# Fix ownership for node user
chown -R node:node /home/node/.ssh 2>/dev/null || true

exec "$@"
