#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v pacman >/dev/null 2>&1; then
    cat >&2 <<'EOF'
This installer currently supports Arch Linux and Arch-based distributions.
Install Fcitx5 development files, CMake, and a C++ compiler with your system
package manager, then rerun this script.
EOF
    exit 2
fi

sudo pacman -S --needed \
    fcitx5 fcitx5-qt fcitx5-gtk fcitx5-configtool cmake gcc

cmake -S "$ROOT_DIR/wayland/fcitx5" \
    -B "$ROOT_DIR/build/fcitx5" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/usr
cmake --build "$ROOT_DIR/build/fcitx5" --parallel
sudo rm -f /usr/share/fcitx5/addon/multilang-addon.conf
sudo cmake --install "$ROOT_DIR/build/fcitx5"

CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
XDG_DESKTOP="${XDG_CURRENT_DESKTOP:-}"
if [[ "${XDG_DESKTOP,,}" == *kde* ]] && command -v kwriteconfig6 >/dev/null 2>&1; then
    # Automatic switching needs one active Fcitx engine across application
    # contexts. Otherwise a window can keep using keyboard-sy while another
    # window is on Multilang.
    kwriteconfig6 --file "$CONFIG_HOME/fcitx5/config" \
        --group Behavior --key ShareInputState All

    kwriteconfig6 --file "$CONFIG_HOME/kwinrc" \
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
        OPTIONS="$(kreadconfig6 --file "$CONFIG_HOME/kxkbrc" \
            --group Layout --key Options --default '')"
        case ",$OPTIONS," in
            *,grp:alt_shift_toggle,*) ;;
            *) OPTIONS="${OPTIONS:+$OPTIONS,}grp:alt_shift_toggle" ;;
        esac
        kwriteconfig6 --file "$CONFIG_HOME/kxkbrc" \
            --group Layout --key Options "$OPTIONS"
    fi

    WAYLAND_CONFIG="$CONFIG_HOME/fcitx5/conf/wayland.conf"
    mkdir -p "$(dirname -- "$WAYLAND_CONFIG")"
    if grep -q '^Allow Overriding System XKB Settings=' "$WAYLAND_CONFIG" 2>/dev/null; then
        sed -i 's/^Allow Overriding System XKB Settings=.*/Allow Overriding System XKB Settings=False/' \
            "$WAYLAND_CONFIG"
    else
        printf '\nAllow Overriding System XKB Settings=False\n' >> "$WAYLAND_CONFIG"
    fi
fi

PROFILE="$CONFIG_HOME/fcitx5/profile"
if [[ ! -e "$PROFILE" ]]; then
    mkdir -p "$(dirname -- "$PROFILE")"
    cat > "$PROFILE" <<'EOF'
[Groups/0]
Name=Default
Default Layout=us
DefaultIM=multilang

[Groups/0/Items/0]
Name=keyboard-us
Layout=

[Groups/0/Items/1]
Name=multilang
Layout=

[GroupOrder]
0=Default
EOF
    chmod 600 "$PROFILE"
fi

if command -v fcitx5-remote >/dev/null 2>&1 && \
    fcitx5-remote --check >/dev/null 2>&1; then
    fcitx5-remote -r
fi

cat <<'EOF'

Multilang's Fcitx5 engine is installed. On KDE Wayland, the installer selects
Fcitx 5 Wayland Launcher and adds Multilang to a new Fcitx profile. It shares
Fcitx input state across applications so the selected engine stays consistent.
If you had an existing Fcitx profile, add Multilang once with fcitx5-configtool.
The installer also makes sure US English and Arabic 101 are available in KDE.
It prevents Fcitx from replacing KDE's layout list with a single layout.
Alt+Shift switches between the two layouts. Log out and back in if KWin does not
reload the input method and layout list immediately. When you start Multilang,
it activates the Multilang engine in Fcitx5 automatically.

EOF
