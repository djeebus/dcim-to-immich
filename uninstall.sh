#!/bin/sh
# Remove dcim-to-immich. Mirrors install.sh:
#
#   ./uninstall.sh                   As a user: turn it off for yourself.
#   sudo ./uninstall.sh              Remove the system part (it stops working for everyone).
#   sudo ./uninstall.sh --user NAME  Both: the system part, and NAME's setup.
#
# Leaves each user's config (with the API keys) in ~/.config/dcim-to-immich/.
set -eu

WANTS=graphical-session.target.wants

target_user=""
while [ $# -gt 0 ]; do
    case "$1" in
        --user) target_user=${2:?--user needs a user name}; shift 2 ;;
        *) sed -n '2,8s/^# \{0,1\}//p' "$0" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    [ -z "$target_user" ] || { echo "error: --user needs root" >&2; exit 1; }
    systemctl --user disable --now dcim-to-immich.service 2>/dev/null || true
    rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/dcim-to-immich"
    echo "Turned off for $(id -un). Config is still in ${XDG_CONFIG_HOME:-$HOME/.config}/dcim-to-immich/."
    exit 0
fi

if [ -n "$target_user" ]; then
    home=$(getent passwd "$target_user" | cut -d: -f6)
    [ -n "$home" ] || { echo "error: no such user: $target_user" >&2; exit 1; }
    systemctl --user -M "$target_user@" stop dcim-to-immich.service 2>/dev/null || true
    rm -f "$home/.config/systemd/user/$WANTS/dcim-to-immich.service"
    rm -rf "$home/.cache/dcim-to-immich"
    echo "Turned off for $target_user."
fi

rm -f /etc/systemd/user/dcim-to-immich.service
rm -rf /usr/local/lib/dcim-to-immich
rm -f /etc/udev/rules.d/72-dcim-to-immich.rules
udevadm control --reload-rules
echo "Removed the system part. Users' configs are still in ~/.config/dcim-to-immich/."
