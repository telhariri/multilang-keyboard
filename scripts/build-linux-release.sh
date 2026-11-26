#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
DIST_DIR="$ROOT_DIR/dist"
ARCHIVE_NAME="Multilang-Linux-x64.tar.gz"
BUNDLE_DIR_NAME="Multilang-Linux-x64"

STAGE_ROOT="$(mktemp -d)"
CHECK_ROOT="$(mktemp -d)"
cleanup() {
    rm -rf "$STAGE_ROOT" "$CHECK_ROOT"
}
trap cleanup EXIT

BUNDLE_DIR="$STAGE_ROOT/$BUNDLE_DIR_NAME"
mkdir -p "$DIST_DIR" "$BUNDLE_DIR/assets" "$BUNDLE_DIR/scripts" "$BUNDLE_DIR/wayland/fcitx5"

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
    "$ROOT_DIR/CHANGELOG.md" \
    "$BUNDLE_DIR/"

cp -f \
    "$ROOT_DIR/assets/multilang-banner.svg" \
    "$ROOT_DIR/assets/multilang.ico" \
    "$BUNDLE_DIR/assets/"

cp -f \
    "$ROOT_DIR/scripts/install-linux.sh" \
    "$ROOT_DIR/scripts/install-wayland-ime.sh" \
    "$ROOT_DIR/scripts/uninstall-linux.sh" \
    "$BUNDLE_DIR/scripts/"
chmod 0755 "$BUNDLE_DIR"/scripts/*.sh

cp -f "$ROOT_DIR/wayland/fcitx5/CMakeLists.txt" "$BUNDLE_DIR/wayland/fcitx5/"
cp -r "$ROOT_DIR/wayland/fcitx5/src" "$BUNDLE_DIR/wayland/fcitx5/"
cp -r "$ROOT_DIR/wayland/fcitx5/tests" "$BUNDLE_DIR/wayland/fcitx5/"

OUT_ARCHIVE="$DIST_DIR/$ARCHIVE_NAME"
rm -f "$OUT_ARCHIVE"
tar --sort=name -C "$STAGE_ROOT" -czf "$OUT_ARCHIVE" "$BUNDLE_DIR_NAME"

tar -xzf "$OUT_ARCHIVE" -C "$CHECK_ROOT"
bash -n "$CHECK_ROOT/$BUNDLE_DIR_NAME"/scripts/*.sh
echo "Built $OUT_ARCHIVE ($(du -h "$OUT_ARCHIVE" | cut -f1))"
