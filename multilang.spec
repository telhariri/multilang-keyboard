# PyInstaller build for the Windows x64 release.
from pathlib import Path

root = Path.cwd()
import wordfreq

wordfreq_data = Path(wordfreq.__file__).parent / "data"
required_data = [
    (str(wordfreq_data / "large_ar.msgpack.gz"), "wordfreq/data"),
    (str(wordfreq_data / "large_en.msgpack.gz"), "wordfreq/data"),
]

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=[(str(root / "native" / "multilang_native.dll"), "native")],
    datas=required_data + [(str(root / "LICENSE"), ".")],
    hiddenimports=[
        "pynput.keyboard._win32",
        "pynput._util.win32",
        "pystray._win32",
        "pystray._util.win32",
        "PIL.ImageWin",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Multilang",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "assets" / "multilang.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Multilang",
)
