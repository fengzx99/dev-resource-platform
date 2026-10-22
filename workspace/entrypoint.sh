#!/bin/bash
set -euo pipefail
mkdir -p /run/sshd
# Keep login authorization outside the user-writable persistent home.
printf '%s\n' "${DRP_SSH_PUBLIC_KEY:?SSH public key required}" > /run/sshd/authorized_keys
chmod 755 /run/sshd
chmod 644 /run/sshd/authorized_keys
mkdir -p /root/.drp/ssh-hostkeys
chmod 700 /root/.drp/ssh-hostkeys
if [ ! -f /root/.drp/ssh-hostkeys/ssh_host_ed25519_key ]; then
    ssh-keygen -q -t ed25519 -N '' -f /root/.drp/ssh-hostkeys/ssh_host_ed25519_key
fi
cat > /run/drp-sshd.conf <<'EOF'
Port 22
ListenAddress 0.0.0.0
HostKey /root/.drp/ssh-hostkeys/ssh_host_ed25519_key
AuthorizedKeysFile /run/sshd/authorized_keys
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
UsePAM no
AllowUsers root
AllowTcpForwarding yes
GatewayPorts no
X11Forwarding no
PermitTunnel no
Subsystem sftp internal-sftp
PidFile /run/sshd.pid
EOF
echo 'Root workspace SSH ready; persistent files belong in /data and /root.'
exec /usr/sbin/sshd -D -e -f /run/drp-sshd.conf
