#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --needed \
        fcitx5 fcitx5-qt fcitx5-gtk fcitx5-configtool cmake gcc
elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y \
        fcitx5 fcitx5-frontend-qt6 fcitx5-frontend-gtk3 fcitx5-config-qt \
        cmake g++ libfcitx5core-dev python3-tk
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y \
        fcitx5 fcitx5-qt fcitx5-gtk fcitx5-configtool cmake gcc-c++ \
        fcitx5-devel python3-tkinter
else
    cat >&2 <<'EOF'
Unsupported package manager for automatic Fcitx5 setup.
Install Fcitx5 development files, CMake, and a C++ compiler with your system
package manager, then rerun this script.
EOF
    exit 2
fi

cmake -S "$ROOT_DIR/wayland/fcitx5" \
    -B "$ROOT_DIR/build/fcitx5" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/usr \
    -DBUILD_TESTING=ON
cmake --build "$ROOT_DIR/build/fcitx5" --parallel
ctest --test-dir "$ROOT_DIR/build/fcitx5" --output-on-failure
sudo rm -f /usr/share/fcitx5/addon/multilang-addon.conf /usr/share/fcitx5/inputmethod/multilang.conf
sudo cmake --install "$ROOT_DIR/build/fcitx5"

CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
XDG_DESKTOP="${XDG_CURRENT_DESKTOP:-}"
if [[ "${XDG_DESKTOP,,}" == *kde* ]] && command -v kwriteconfig6 >/dev/null 2>&1; then
    kwriteconfig6 --file kwinrc \
        --group Wayland --key InputMethod \
        /usr/share/applications/fcitx5-wayland-launcher.desktop --notify

    if command -v kreadconfig6 >/dev/null 2>&1; then
        LAYOUTS="$(kreadconfig6 --file "$CONFIG_HOME/kxkbrc" \
            --group Layout --key LayoutList --default us)"
        case ",$LAYOUTS," in
            *,us,*) ;;
            *) LAYOUTS="${LAYOUTS:+$LAYOUTS,}us" ;;
        esac
        case ",$LAYOUTS," in
            *,ara,*) ;;
            *) LAYOUTS="${LAYOUTS:+$LAYOUTS,}ara" ;;
        esac
        kwriteconfig6 --file "$CONFIG_HOME/kxkbrc" \
            --group Layout --key Use true
        kwriteconfig6 --file "$CONFIG_HOME/kxkbrc" \
            --group Layout --key LayoutList "$LAYOUTS"

        # Let Fcitx5 own Alt+Shift switching so KDE and Fcitx5 never fight.
        OPTIONS="$(kreadconfig6 --file "$CONFIG_HOME/kxkbrc" \
            --group Layout --key Options --default '')"
        NEW_OPTIONS="$(python3 -c '
import sys
opts = [o for o in sys.argv[1].split(",") if o and o != "grp:alt_shift_toggle"]
print(",".join(opts))
' "$OPTIONS")"
        kwriteconfig6 --file "$CONFIG_HOME/kxkbrc" \
            --group Layout --key Options "$NEW_OPTIONS"
    fi

    WAYLAND_CONFIG="$CONFIG_HOME/fcitx5/conf/wayland.conf"
    mkdir -p "$(dirname -- "$WAYLAND_CONFIG")"
    cat > "$WAYLAND_CONFIG" <<'EOF'
Allow Overriding System XKB Settings=False
EOF
fi

if [[ "${XDG_DESKTOP,,}" == *kde* ]] && [[ "${XDG_SESSION_TYPE:-}" == "wayland" ]] && \
    command -v kwriteconfig6 >/dev/null 2>&1; then
    # Stop Fcitx5 before writing profile/config so it cannot overwrite our edits on exit.
    kwriteconfig6 --file kwinrc --group Wayland --key InputMethod "" --notify
fi
if command -v fcitx5-remote >/dev/null 2>&1 && fcitx5-remote --check >/dev/null 2>&1; then
    fcitx5-remote -e >/dev/null 2>&1 || true
    sleep 1
fi

mkdir -p "$CONFIG_HOME/fcitx5"
FCITX_CONFIG="$CONFIG_HOME/fcitx5/config"
if [[ -e "$FCITX_CONFIG" ]]; then
    cp "$FCITX_CONFIG" "${FCITX_CONFIG}.bak"
fi
cat > "$FCITX_CONFIG" <<'EOF'
[Hotkey]
EnumerateWithTriggerKeys=True
ModifierOnlyKeyTimeout=250

[Hotkey/TriggerKeys]
0=Alt+Shift_L
1=Alt+Shift_R
2=Shift+Alt_L
3=Shift+Alt_R
4=Control+space
5=Super+space

[Hotkey/AltTriggerKeys]

[Hotkey/EnumerateForwardKeys]

[Hotkey/EnumerateBackwardKeys]

[Hotkey/EnumerateGroupForwardKeys]

[Hotkey/EnumerateGroupBackwardKeys]

[Behavior]
ActiveByDefault=False
ShareInputState=All
ShowInputMethodInformation=True
ShowFirstInputMethodInformation=True
EOF
chmod 600 "$FCITX_CONFIG"

PROFILE="$CONFIG_HOME/fcitx5/profile"
if [[ -e "$PROFILE" ]]; then
    cp "$PROFILE" "${PROFILE}.bak"
fi
cat > "$PROFILE" <<'EOF'
[Groups/0]
Name=Default
Default Layout=us
DefaultIM=keyboard-ara

[Groups/0/Items/0]
Name=keyboard-us
Layout=

[Groups/0/Items/1]
Name=keyboard-ara
Layout=

[GroupOrder]
0=Default
EOF
chmod 600 "$PROFILE"

if [[ "${XDG_DESKTOP,,}" == *kde* ]] && [[ "${XDG_SESSION_TYPE:-}" == "wayland" ]] && \
    command -v kwriteconfig6 >/dev/null 2>&1; then
    kwriteconfig6 --file kwinrc --group Wayland --key InputMethod \
        /usr/share/applications/fcitx5-wayland-launcher.desktop --notify
    sleep 1.5
elif command -v fcitx5 >/dev/null 2>&1; then
    fcitx5 -d >/dev/null 2>&1 || true
    sleep 1.5
fi

cat <<'EOF'

Multilang's Fcitx5 module is installed. Your input profile now has only two
layouts: English (US) and Arabic, with Alt+Shift / Ctrl+Space / Super+Space
switching cleanly between them. Multilang runs silently in the background as a
Fcitx5 module without adding a third input method.

EOF
