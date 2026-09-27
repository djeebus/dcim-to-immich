#!/bin/sh
# Install dcim-to-immich for the current user. Run as yourself (not root); asks for sudo.
set -eu

cd "$(dirname "$0")"
dest="$HOME/.local/share/dcim-to-immich"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "==> Installing packages"
sudo apt-get install -y python3-gi gir1.2-gtk-3.0 python3-gphoto2 python3-pyudev

echo "==> Installing udev rule"
sudo install -m 644 udev/72-dcim-to-immich.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules

echo "==> Installing to $dest"
rm -rf "$dest"
mkdir -p "$dest"
cp -r dcim_to_immich "$dest/"
find "$dest" -name __pycache__ -prune -exec rm -rf {} +

echo "==> Enabling user service"
mkdir -p "$unit_dir"
install -m 644 systemd/dcim-to-immich.service "$unit_dir/"
systemctl --user daemon-reload
systemctl --user enable dcim-to-immich.service
systemctl --user restart dcim-to-immich.service

config="${XDG_CONFIG_HOME:-$HOME/.config}/dcim-to-immich/config.json"
if [ ! -e "$config" ]; then
    echo "==> Creating starter config at $config"
    mkdir -p "$(dirname "$config")"
    (umask 077 && cat > "$config" <<'EOF'
{
  "server": "https://photos.example.com",
  "users": {
    "Bob": "paste-bobs-immich-api-key-here",
    "Sue": "paste-sues-immich-api-key-here"
  },
  "cameras": {}
}
EOF
    )
fi

echo
echo "Done. Put the Immich server and each user's API key in:"
echo "  $config"
echo "Logs: journalctl --user -u dcim-to-immich -f"
