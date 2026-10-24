#!/bin/sh
# Run once with sudo, after install.sh, in the folder it installed. It puts the firewall rules in
# place now and after every start of Docker. The files root runs are copied to a folder only root
# can write.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
# The prefix, which names the folder and the unit (install.sh writes it).
. "$here/pool-settings.sh"
installed=/usr/local/lib/$prefix-runner
unit=$prefix-runner-firewall.service

install -d -o root -g root -m 755 "$installed"
install -o root -g root -m 644 "$here/settings.sh" "$installed/settings.sh"
install -o root -g root -m 755 "$here/firewall.sh" "$installed/firewall.sh"
install -o root -g root -m 644 "$here/systemd/$unit" "/etc/systemd/system/$unit"

systemctl daemon-reload
systemctl enable "$unit"
# Restart, not only start: a service that already ran would keep the rules of the old files.
systemctl restart "$unit"
echo "The firewall for CI containers is in place."
