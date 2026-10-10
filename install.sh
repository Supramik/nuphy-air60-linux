#!/usr/bin/env bash
# Sets up the tools in this repo for the current user.
#
#   ./install.sh viewer      the keymap overlay: launcher, Meta+Shift+K and F19 shortcuts,
#                            and the udev rule that lets you open the keyboard over USB
#   ./install.sh usb-hook    optional: refresh the keymap by itself while plugged in by USB
#   ./install.sh toggle      laptop keyboard off/on with Meta+K, without a password prompt
#
# Add --remove to undo a step. Steps that need root ask for your password (sudo in a
# terminal, the desktop's password dialog otherwise) and are skipped when the installed
# files already match. The viewer runs from this folder, so keep the folder where it is.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="$repo/bin"
data="${NUPHY_KEYMAP_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/nuphy-air60}"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
rules=/etc/udev/rules.d
usb_id='^HID_ID=0003:000019F5:00003255'  # the Air60 V2 over USB: vendor 19f5, product 3255

as_root() { if [ -t 0 ]; then sudo "$@"; else pkexec "$@"; fi; }

# True when the installed udev rule says the same thing, comments aside.
same_rule() { [ -f "$2" ] && diff -q <(grep -v '^#' "$1") <(grep -v '^#' "$2") >/dev/null; }

# Fill in a template from system/ with the path of this checkout's bin/.
render() { sed "s|@BIN@|$bin|g" "$repo/system/$1" > "$2"; }

refresh_menu() { if command -v kbuildsycoca6 >/dev/null; then kbuildsycoca6 >/dev/null 2>&1 || true; fi; }

plugged_in() { grep -qsi "$usb_id" /sys/class/hidraw/hidraw*/device/uevent; }

install_viewer() {
    mkdir -p "$data/via" "$data/snapshots" "$apps"
    render nuphy-keymap.desktop "$apps/nuphy-keymap.desktop"
    refresh_menu
    if ! same_rule "$repo/system/50-nuphy.rules" "$rules/50-nuphy.rules"; then
        echo "Installing the udev rule that lets you (and VIA) open the keyboard over USB."
        as_root /bin/sh -c 'install -m644 "$1" "$2" && udevadm control --reload &&
            udevadm trigger --subsystem-match=hidraw' sh "$repo/system/50-nuphy.rules" "$rules/50-nuphy.rules"
    fi
    echo "Viewer installed: Meta+Shift+K (or F19) shows the keymap."
    if [ ! -f "$data/via/definition.json" ]; then
        echo "Next: download NuPhy's VIA file for your keyboard from"
        echo "  https://nuphy.com/pages/json-files-for-nuphy-keyboards"
        echo "and save it as $data/via/definition.json"
    fi
}

remove_viewer() {
    rm -f "$apps/nuphy-keymap.desktop"
    refresh_menu
    echo "Viewer removed. Your keymap data in $data stays, and so does"
    echo "$rules/50-nuphy.rules, which VIA needs as well."
}

install_usb_hook() {
    mkdir -p "$units"
    render nuphy-keymap-sync.service "$units/nuphy-keymap-sync.service"
    systemctl --user daemon-reload
    if ! same_rule "$repo/system/51-nuphy-keymap.rules" "$rules/51-nuphy-keymap.rules"; then
        # A copy, never a symlink: udev rules run as root, so they must stay root-owned.
        # If the keyboard is plugged in right now, replay its "add" event so the hook starts.
        as_root /bin/sh -c 'install -m644 "$1" "$2" && udevadm control --reload &&
            for d in /sys/class/hidraw/hidraw*; do
                grep -qi "$3" "$d/device/uevent" && udevadm trigger --action=add "$d"
            done; true' sh "$repo/system/51-nuphy-keymap.rules" "$rules/51-nuphy-keymap.rules" "$usb_id"
    elif plugged_in; then
        systemctl --user restart nuphy-keymap-sync.service  # run it from this checkout
    fi
    echo "USB hook installed. It runs whenever the keyboard is plugged in by USB."
}

remove_usb_hook() {
    systemctl --user stop nuphy-keymap-sync.service 2>/dev/null || true
    if [ -f "$rules/51-nuphy-keymap.rules" ]; then
        as_root /bin/sh -c 'rm -f "$1" && udevadm control --reload' sh "$rules/51-nuphy-keymap.rules"
    fi
    rm -f "$units/nuphy-keymap-sync.service"
    systemctl --user daemon-reload
    echo "USB hook removed."
}

install_toggle() {
    local sudoers
    sudoers="$(mktemp)"
    printf '%s ALL=(root) NOPASSWD: /usr/local/bin/toggle-internal-keyboard\n' "$(id -un)" > "$sudoers"
    as_root /bin/sh -c 'visudo -cqf "$2" &&
        install -m755 "$1/bin/toggle-internal-keyboard" /usr/local/bin/toggle-internal-keyboard &&
        install -m440 "$2" /etc/sudoers.d/toggle-internal-keyboard &&
        install -m644 "$1/system/internal-keyboard-restore.service" /etc/systemd/system/ &&
        systemctl daemon-reload && systemctl enable internal-keyboard-restore.service' sh "$repo" "$sudoers"
    rm -f "$sudoers"
    mkdir -p "$apps"
    install -m644 "$repo/system/toggle-internal-keyboard.desktop" "$apps/"
    refresh_menu
    echo "Meta+K now turns the laptop keyboard off and on."
}

remove_toggle() {
    rm -f "$apps/toggle-internal-keyboard.desktop"
    refresh_menu
    as_root /bin/sh -c '/usr/local/bin/toggle-internal-keyboard on >/dev/null 2>&1 || true
        systemctl disable internal-keyboard-restore.service 2>/dev/null || true
        rm -f /etc/systemd/system/internal-keyboard-restore.service \
              /etc/sudoers.d/toggle-internal-keyboard /usr/local/bin/toggle-internal-keyboard
        systemctl daemon-reload'
    echo "Toggle removed. The laptop keyboard is on."
}

case "${1:-}" in
    viewer | usb-hook | toggle) ;;
    *) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
step="${1//-/_}"
if [ "${2:-}" = "--remove" ]; then "remove_$step"; else "install_$step"; fi
