#!/bin/sh
# Run once with sudo, after install.sh. It puts the firewall rules in place now and after every
# start of Docker. The files root runs are copied to a folder only root can write.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
installed=/usr/local/lib/example-app-runner

install -d -o root -g root -m 755 "$installed"
install -o root -g root -m 644 "$here/settings.sh" "$installed/settings.sh"
install -o root -g root -m 755 "$here/firewall.sh" "$installed/firewall.sh"
install -o root -g root -m 644 "$here/systemd/example-app-runner-firewall.service" \
  /etc/systemd/system/example-app-runner-firewall.service

systemctl daemon-reload
systemctl enable example-app-runner-firewall.service
# Restart, not only start: a service that already ran would keep the rules of the old files.
systemctl restart example-app-runner-firewall.service
echo "The firewall for CI containers is in place."
