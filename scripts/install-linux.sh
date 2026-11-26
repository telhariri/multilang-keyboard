#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
INSTALL_DIR="${MULTILANG_HOME:-$HOME/.local/share/multilang}"
BIN_DIR="${HOME}/.local/bin"
APPS_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/256x256/apps"
AUTOSTART_DIR="${HOME}/.config/autostart"

AUTOSTART=false
WAYLAND_MODE="auto"

usage() {
    cat <<'EOF'
Usage: ./scripts/install-linux.sh [OPTIONS]

Options:
  --autostart      Start Multilang automatically when you log in
  --with-wayland   Also install and configure the KDE Wayland Fcitx5 module
  --skip-wayland   Skip Fcitx5 KDE Wayland setup
  -h, --help       Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --autostart)
            AUTOSTART=true
            ;;
        --with-wayland)
            WAYLAND_MODE="yes"
            ;;
        --skip-wayland)
            WAYLAND_MODE="no"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 (3.10+) is required." >&2
    exit 1
fi

if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
    echo "Error: Python 3.10 or newer is required." >&2
    exit 1
fi

if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Error: Python tkinter is missing. Install it with your package manager first:
  Arch/EndeavourOS: sudo pacman -S tk
  Debian/Ubuntu:    sudo apt install python3-tk
  Fedora:           sudo dnf install python3-tkinter
EOF
    exit 1
fi

mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"
RESOLVED_INSTALL="$(cd -- "$INSTALL_DIR" && pwd -P)"

if [[ "$ROOT_DIR" != "$RESOLVED_INSTALL" ]]; then
    cp -f \
        "$ROOT_DIR/main.py" \
        "$ROOT_DIR/recognizer.py" \
        "$ROOT_DIR/layout_switcher.py" \
        "$ROOT_DIR/wayland_backend.py" \
        "$ROOT_DIR/windows_backend.py" \
        "$ROOT_DIR/rust_backend.py" \
        "$ROOT_DIR/requirements.txt" \
        "$ROOT_DIR/LICENSE" \
        "$ROOT_DIR/README.md" \
        "$ROOT_DIR/README.en.md" \
        "$RESOLVED_INSTALL/"
    if [[ -f "$ROOT_DIR/CHANGELOG.md" ]]; then
        cp -f "$ROOT_DIR/CHANGELOG.md" "$RESOLVED_INSTALL/"
    fi

    rm -rf "$RESOLVED_INSTALL/assets" "$RESOLVED_INSTALL/scripts" "$RESOLVED_INSTALL/wayland/fcitx5"
    mkdir -p "$RESOLVED_INSTALL/assets" "$RESOLVED_INSTALL/scripts" "$RESOLVED_INSTALL/wayland/fcitx5"
    cp -r "$ROOT_DIR/assets/." "$RESOLVED_INSTALL/assets/"
    cp -f \
        "$ROOT_DIR/scripts/install-linux.sh" \
        "$ROOT_DIR/scripts/install-wayland-ime.sh" \
        "$ROOT_DIR/scripts/uninstall-linux.sh" \
        "$RESOLVED_INSTALL/scripts/"
    chmod +x "$RESOLVED_INSTALL"/scripts/*.sh

    cp -f "$ROOT_DIR/wayland/fcitx5/CMakeLists.txt" "$RESOLVED_INSTALL/wayland/fcitx5/"
    cp -r "$ROOT_DIR/wayland/fcitx5/src" "$RESOLVED_INSTALL/wayland/fcitx5/"
    if [[ -d "$ROOT_DIR/wayland/fcitx5/tests" ]]; then
        cp -r "$ROOT_DIR/wayland/fcitx5/tests" "$RESOLVED_INSTALL/wayland/fcitx5/"
    fi
fi

if [[ ! -d "$RESOLVED_INSTALL/.venv" ]]; then
    python3 -m venv "$RESOLVED_INSTALL/.venv"
fi

"$RESOLVED_INSTALL/.venv/bin/python" -m pip install --upgrade pip
"$RESOLVED_INSTALL/.venv/bin/python" -m pip install -r "$RESOLVED_INSTALL/requirements.txt"
"$RESOLVED_INSTALL/.venv/bin/python" "$RESOLVED_INSTALL/main.py" --self-test

if [[ -f "$RESOLVED_INSTALL/assets/multilang.ico" ]]; then
    "$RESOLVED_INSTALL/.venv/bin/python" - "$RESOLVED_INSTALL/assets/multilang.ico" "$ICON_DIR/multilang.png" <<'PY'
import sys
from PIL import Image

with Image.open(sys.argv[1]) as img:
    img.convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS).save(sys.argv[2], "PNG")
PY
fi

LAUNCHER="$BIN_DIR/multilang"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$RESOLVED_INSTALL/.venv/bin/python" "$RESOLVED_INSTALL/main.py" "\$@"
EOF
chmod +x "$LAUNCHER"

cat > "$APPS_DIR/multilang.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Multilang
GenericName=Arabic/English Keyboard Layout Corrector
Comment=Automatically correct Arabic and English keyboard layout typos
Exec=$LAUNCHER
Icon=multilang
Terminal=false
Categories=Utility;
StartupNotify=false
EOF

if [[ "$AUTOSTART" == true ]]; then
    mkdir -p "$AUTOSTART_DIR"
    cat > "$AUTOSTART_DIR/multilang.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Multilang
Comment=Start Multilang in the system tray
Exec=$LAUNCHER --start-in-background
Icon=multilang
Terminal=false
Categories=Utility;
StartupNotify=false
EOF
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

if [[ "$WAYLAND_MODE" == "yes" ]] || {
    [[ "$WAYLAND_MODE" == "auto" ]] &&
    [[ "${XDG_SESSION_TYPE:-}" == "wayland" ]] &&
    [[ "${XDG_CURRENT_DESKTOP,,}" == *kde* ]]
}; then
    echo "KDE Plasma Wayland detected — installing and configuring the Fcitx5 module..."
    "$RESOLVED_INSTALL/scripts/install-wayland-ime.sh"
fi

cat <<EOF

Multilang installation complete!
- Installed files: $RESOLVED_INSTALL
- Command launcher: $LAUNCHER
- Desktop entry: $APPS_DIR/multilang.desktop

Run 'multilang' from your application menu or terminal (make sure $BIN_DIR is in PATH).
EOF
