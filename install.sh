#!/bin/sh
# Install dcim-to-immich.
#
#   sudo ./install.sh              System part: packages, udev rule, the program and its
#                                  service, for every user. Doesn't need to know who the
#                                  kiosk user is.
#   sudo ./install.sh --user NAME  The system part, then turn it on for NAME as well
#                                  (starter config + enable). NAME doesn't need to be
#                                  logged in, and never needs sudo.
#   ./install.sh                   As the kiosk user, without sudo, after the system part:
#                                  turn it on for yourself.
#
# Re-run the same way to update. DESTDIR=<dir> stages the system files under <dir>.
set -eu

cd "$(dirname "$0")"

PREFIX=/usr/local/lib/dcim-to-immich
UNIT=/etc/systemd/user/dcim-to-immich.service
RULE=/etc/udev/rules.d/72-dcim-to-immich.rules
WANTS=graphical-session.target.wants

die() { echo "error: $*" >&2; exit 1; }

usage() { sed -n '2,14s/^# \{0,1\}//p' "$0"; }

starter_config() {
    cat <<'EOF'
{
  "server": "https://photos.example.com",
  "album": "Camera uploads",
  "users": {
    "Bob": "paste-bobs-immich-api-key-here",
    "Sue": "paste-sues-immich-api-key-here"
  },
  "cameras": {}
}
EOF
}

install_system() {
    [ -d dcim_to_immich ] || die "run this from the dcim-to-immich source folder"

    echo "==> Installing packages"
    apt-get install -y python3-gi gir1.2-gtk-3.0 python3-gphoto2 python3-pyudev

    echo "==> Installing udev rule"
    install -D -m 644 udev/72-dcim-to-immich.rules "${DESTDIR:-}$RULE"
    udevadm control --reload-rules

    echo "==> Installing program to $PREFIX"
    dest="${DESTDIR:-}$PREFIX"
    rm -rf "$dest"
    mkdir -p "$dest"
    cp -r dcim_to_immich "$dest/"
    find "$dest" -name __pycache__ -prune -exec rm -rf {} +
    chmod -R a+rX "$dest"

    echo "==> Installing user service $UNIT"
    install -D -m 644 systemd/dcim-to-immich.service "${DESTDIR:-}$UNIT"
}

# Root setting up another user: do everything as that user (so the files are theirs),
# without needing them logged in. Equivalent to them running `./install.sh` themselves.
setup_user_as_root() {
    user=$1
    home=$(getent passwd "$user" | cut -d: -f6)
    [ -n "$home" ] || die "no such user: $user"
    config="$home/.config/dcim-to-immich/config.json"

    echo "==> Setting up $user"
    runuser -u "$user" -- mkdir -p "$(dirname "$config")" "$home/.config/systemd/user/$WANTS"
    if [ ! -e "$config" ]; then
        starter_config | runuser -u "$user" -- sh -c 'umask 077 && cat > "$1"' sh "$config"
        echo "    created $config"
    fi
    # What `systemctl --user enable` does, but works while they're logged out.
    runuser -u "$user" -- ln -sfn "$UNIT" "$home/.config/systemd/user/$WANTS/dcim-to-immich.service"

    # If they're logged in right now, (re)start it in their session.
    if systemctl --user -M "$user@" --quiet is-active graphical-session.target 2>/dev/null; then
        systemctl --user -M "$user@" daemon-reload
        systemctl --user -M "$user@" restart dcim-to-immich.service
        echo "    restarted in $user's session"
    else
        echo "    starts next time $user logs in to the desktop"
    fi
    echo
    echo "Put the Immich server and each kid's API key in $config"
    echo "(e.g. sudo -u $user nano $config)"
}

setup_self() {
    [ -f "${DESTDIR:-}$UNIT" ] || die "the system part isn't installed yet; ask someone with sudo to run: sudo ./install.sh"
    config="${XDG_CONFIG_HOME:-$HOME/.config}/dcim-to-immich/config.json"

    if [ ! -e "$config" ]; then
        mkdir -p "$(dirname "$config")"
        (umask 077 && starter_config > "$config")
        echo "==> Created $config"
    fi

    echo "==> Enabling the service for $(id -un)"
    systemctl --user daemon-reload
    systemctl --user enable dcim-to-immich.service
    if systemctl --user --quiet is-active graphical-session.target; then
        systemctl --user restart dcim-to-immich.service
    else
        echo "    starts next time you log in to the desktop"
    fi
    echo
    echo "Put the Immich server and each kid's API key in $config"
    echo "Logs: journalctl --user -u dcim-to-immich -f"
}

target_user=""
while [ $# -gt 0 ]; do
    case "$1" in
        --user) target_user=${2:?--user needs a user name}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    install_system
    [ -z "$target_user" ] || setup_user_as_root "$target_user"
    [ -n "$target_user" ] || echo "
Done. Now either:
  sudo $0 --user <kiosk-user>    set it up for them from here, or
  $0                             run as the kiosk user (no sudo needed)"
else
    [ -z "$target_user" ] || die "--user needs root: sudo $0 --user $target_user"
    setup_self
fi
