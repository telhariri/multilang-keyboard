#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${MULTILANG_HOME:-$HOME/.local/share/multilang}"
BIN_DIR="${HOME}/.local/bin"
APPS_DIR="${HOME}/.local/share/applications"
ICON_PATH="${HOME}/.local/share/icons/hicolor/256x256/apps/multilang.png"
AUTOSTART_FILE="${HOME}/.config/autostart/multilang.desktop"

rm -f "$BIN_DIR/multilang" "$APPS_DIR/multilang.desktop" "$AUTOSTART_FILE" "$ICON_PATH"

if [[ -n "$INSTALL_DIR" && "$INSTALL_DIR" != "/" && "$INSTALL_DIR" != "$HOME" && -d "$INSTALL_DIR" ]]; then
    rm -rf "$INSTALL_DIR"
fi

if command -v update-desktop-database >/dev/null 2>&1 && [[ -d "$APPS_DIR" ]]; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

echo "Removed Multilang user files from $INSTALL_DIR and ~/.local."

if [[ -e /usr/lib/fcitx5/multilang.so || -e /usr/share/fcitx5/addon/multilang.conf ]]; then
    cat <<'EOF'

System-wide Fcitx5 Wayland module files are still present. To remove them too, run:
  sudo rm -f /usr/lib/fcitx5/multilang.so /usr/share/fcitx5/addon/multilang.conf
EOF
fi
