#!/bin/sh
# Remove dcim-to-immich. Leaves ~/.config/dcim-to-immich (camera API keys) in place.
set -eu

systemctl --user disable --now dcim-to-immich.service 2>/dev/null || true
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/dcim-to-immich.service"
systemctl --user daemon-reload
rm -rf "$HOME/.local/share/dcim-to-immich" "${XDG_CACHE_HOME:-$HOME/.cache}/dcim-to-immich"
sudo rm -f /etc/udev/rules.d/72-dcim-to-immich.rules
sudo udevadm control --reload-rules
echo "Removed. Your camera config is still in ${XDG_CONFIG_HOME:-$HOME/.config}/dcim-to-immich/."
