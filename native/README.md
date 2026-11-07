# Native Windows keyboard engine

`multilang_native` is a Rust `cdylib` loaded by `rust_backend.py`. It owns the Windows low-level keyboard hook, suppresses a corrected boundary key, sends backspaces and UTF-16 text through `SendInput`, and requests the target keyboard layout. Python supplies the existing Arabic/English word recognizer through the FFI callback.

The Windows test bundle includes a prebuilt 64-bit DLL, so users do not need a Rust compiler. To build it on Windows with the MSVC Rust toolchain:

```powershell
$env:RUSTFLAGS = "-C target-feature=+crt-static"
cargo build --release --target x86_64-pc-windows-msvc
Copy-Item target\x86_64-pc-windows-msvc\release\multilang_native.dll .\multilang_native.dll
```

Static CRT linking keeps the DLL independent of a separately installed Visual C++ runtime.

The low-level hook is Windows-specific. Linux/X11 uses the existing input listener. KDE Wayland uses the separate Fcitx5 input-method addon under `../wayland/fcitx5`; the addon buffers only the active text field's current composition and asks the running Python app to recognize it over a same-user Unix socket.
