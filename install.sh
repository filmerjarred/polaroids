#!/usr/bin/env bash
# Install the user service and the login autostart entry, then start it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p ~/.config/systemd/user ~/.config/autostart
cp "$here/polaroids.service" ~/.config/systemd/user/polaroids.service
cp "$here/polaroids.desktop" ~/.config/autostart/polaroids.desktop
systemctl --user daemon-reload
if [[ -n "${DISPLAY:-}" ]]; then
    systemctl --user reset-failed polaroids 2>/dev/null || true
    systemctl --user restart polaroids
    sleep 1
    systemctl --user --no-pager status polaroids | head -5
else
    echo "installed; it will start at next login (no DISPLAY now, so not starting)"
fi
echo
echo "logs:      journalctl --user -u polaroids -f"
echo "uninstall: $here/uninstall.sh"
