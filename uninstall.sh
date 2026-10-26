#!/usr/bin/env bash
set -uo pipefail
systemctl --user stop polaroids 2>/dev/null
rm -f ~/.config/systemd/user/polaroids.service ~/.config/autostart/polaroids.desktop
systemctl --user daemon-reload
echo "removed service and autostart entry (your photos in ~/.local/share/polaroids are untouched)"
