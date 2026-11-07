use std::sync::atomic::{AtomicUsize, Ordering};

pub type CorrectionCallback = unsafe extern "C" fn(
    word: *const u8,
    word_len: usize,
    output: *mut u8,
    output_capacity: usize,
    output_len: *mut usize,
    target_language: *mut u32,
) -> i32;

pub type StatusCallback = unsafe extern "C" fn(message: *const u8, message_len: usize);

static CORRECTION_CALLBACK: AtomicUsize = AtomicUsize::new(0);
static STATUS_CALLBACK: AtomicUsize = AtomicUsize::new(0);

#[no_mangle]
pub extern "C" fn multilang_start(
    correction_callback: Option<CorrectionCallback>,
    status_callback: Option<StatusCallback>,
) -> i32 {
    let Some(correction_callback) = correction_callback else {
        return -1;
    };
    CORRECTION_CALLBACK.store(correction_callback as usize, Ordering::Release);
    STATUS_CALLBACK.store(
        status_callback.map_or(0, |callback| callback as usize),
        Ordering::Release,
    );

    #[cfg(windows)]
    {
        return windows::start();
    }

    #[cfg(not(windows))]
    {
        -2
    }
}

#[no_mangle]
pub extern "C" fn multilang_stop() -> i32 {
    #[cfg(windows)]
    {
        return windows::stop();
    }

    #[cfg(not(windows))]
    {
        0
    }
}

#[no_mangle]
pub extern "C" fn multilang_version() -> *const u8 {
    b"0.1.0\0".as_ptr()
}

#[no_mangle]
pub extern "C" fn multilang_abi_version() -> u32 {
    2
}

#[cfg(windows)]
mod windows;

#[cfg(test)]
mod tests {
    #[cfg(windows)]
    use super::{CorrectionCallback, StatusCallback};

    #[test]
    fn replacement_encodes_as_utf16_code_units() {
        let units: Vec<u16> = "hello 😀 ".encode_utf16().collect();
        assert_eq!(units.len(), 9);
        assert_eq!(units[6], 0xD83D);
        assert_eq!(units[7], 0xDE00);
        assert_eq!(units[8], b' ' as u16);
    }

    #[test]
    fn native_abi_version_matches_python_bridge() {
        assert_eq!(super::multilang_abi_version(), 2);
    }

    #[cfg(windows)]
    #[test]
    fn windows_dll_loads_and_hook_starts_and_stops() {
        use std::os::windows::ffi::OsStrExt;

        unsafe extern "C" fn never_corrects(
            _word: *const u8,
            _word_len: usize,
            _output: *mut u8,
            _output_capacity: usize,
            _output_len: *mut usize,
            _target_language: *mut u32,
        ) -> i32 {
            0
        }

        unsafe extern "C" fn ignore_status(_message: *const u8, _message_len: usize) {}

        type StartFunction =
            unsafe extern "C" fn(Option<CorrectionCallback>, Option<StatusCallback>) -> i32;
        type StopFunction = unsafe extern "C" fn() -> i32;

        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn LoadLibraryW(path: *const u16) -> isize;
            fn GetProcAddress(module: isize, name: *const u8) -> *mut std::ffi::c_void;
            fn FreeLibrary(module: isize) -> i32;
        }

        let correction: CorrectionCallback = never_corrects;
        let status: StatusCallback = ignore_status;
        let executable = std::env::current_exe().expect("current executable path");
        let dll_path = executable
            .parent()
            .and_then(|deps| deps.parent())
            .expect("release directory")
            .join("multilang_native.dll");
        let mut wide_path: Vec<u16> = dll_path.as_os_str().encode_wide().collect();
        wide_path.push(0);

        unsafe {
            let module = LoadLibraryW(wide_path.as_ptr());
            assert_ne!(module, 0, "could not load {}", dll_path.display());
            let start_ptr = GetProcAddress(module, b"multilang_start\0".as_ptr());
            let stop_ptr = GetProcAddress(module, b"multilang_stop\0".as_ptr());
            assert!(!start_ptr.is_null(), "multilang_start export missing");
            assert!(!stop_ptr.is_null(), "multilang_stop export missing");
            let start: StartFunction = std::mem::transmute(start_ptr);
            let stop: StopFunction = std::mem::transmute(stop_ptr);
            assert_eq!(start(Some(correction), Some(status)), 0);
            assert_eq!(stop(), 0);
            assert_eq!(FreeLibrary(module), 1);
        }
    }
}
