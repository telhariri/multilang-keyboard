use crate::{CORRECTION_CALLBACK, STATUS_CALLBACK};
use std::collections::HashSet;
use std::mem::{size_of, zeroed};
use std::ptr::null_mut;
use std::sync::atomic::{AtomicIsize, AtomicU32, Ordering};
use std::sync::{mpsc, Mutex, OnceLock};
use std::thread::{self, JoinHandle};
use std::time::Duration;

const WH_KEYBOARD_LL: i32 = 13;
const HC_ACTION: i32 = 0;
const WM_KEYDOWN: u32 = 0x0100;
const WM_KEYUP: u32 = 0x0101;
const WM_SYSKEYDOWN: u32 = 0x0104;
const WM_SYSKEYUP: u32 = 0x0105;
const WM_QUIT: u32 = 0x0012;
const WM_INPUTLANGCHANGEREQUEST: u32 = 0x0050;
const WM_APP_INJECT: u32 = 0x8001;
const LLKHF_INJECTED: u32 = 0x10;
const TU_NO_STATE_CHANGE: u32 = 0x0004;
const SWITCH_LAYOUT_FLAG: u32 = 0x0100;
const INPUT_KEYBOARD: u32 = 1;
const KEYEVENTF_KEYUP: u32 = 0x0002;
const KEYEVENTF_UNICODE: u32 = 0x0004;

const VK_BACK: u32 = 0x08;
const VK_TAB: u32 = 0x09;
const VK_RETURN: u32 = 0x0D;
const VK_SHIFT: u32 = 0x10;
const VK_CONTROL: u32 = 0x11;
const VK_MENU: u32 = 0x12;
const VK_ESCAPE: u32 = 0x1B;
const VK_SPACE: u32 = 0x20;
const VK_DELETE: u32 = 0x2E;
const VK_LSHIFT: u32 = 0xA0;
const VK_RSHIFT: u32 = 0xA1;
const VK_LCONTROL: u32 = 0xA2;
const VK_RCONTROL: u32 = 0xA3;
const VK_LMENU: u32 = 0xA4;
const VK_RMENU: u32 = 0xA5;
const VK_LWIN: u32 = 0x5B;
const VK_RWIN: u32 = 0x5C;

static HOOK_HANDLE: AtomicIsize = AtomicIsize::new(0);
static HOOK_THREAD_ID: AtomicU32 = AtomicU32::new(0);
static STATE: OnceLock<Mutex<HookState>> = OnceLock::new();
static THREAD: OnceLock<Mutex<Option<JoinHandle<()>>>> = OnceLock::new();

type HookProc = Option<unsafe extern "system" fn(i32, usize, isize) -> isize>;

#[repr(C)]
#[derive(Clone, Copy)]
struct Point {
    x: i32,
    y: i32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct Message {
    hwnd: isize,
    message: u32,
    w_param: usize,
    l_param: isize,
    time: u32,
    point: Point,
    private: u32,
}

#[repr(C)]
struct LowLevelKeyboardEvent {
    vk_code: u32,
    scan_code: u32,
    flags: u32,
    time: u32,
    extra_info: usize,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct KeyboardInput {
    virtual_key: u16,
    scan_code: u16,
    flags: u32,
    time: u32,
    extra_info: usize,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct MouseInput {
    dx: i32,
    dy: i32,
    mouse_data: u32,
    flags: u32,
    time: u32,
    extra_info: usize,
}

#[repr(C)]
#[derive(Clone, Copy)]
union InputData {
    keyboard: KeyboardInput,
    mouse: MouseInput,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct Input {
    input_type: u32,
    data: InputData,
}

#[cfg(target_pointer_width = "64")]
const _: [(); 40] = [(); size_of::<Input>()];

#[cfg(target_pointer_width = "32")]
const _: [(); 28] = [(); size_of::<Input>()];

#[link(name = "user32")]
unsafe extern "system" {
    fn CallNextHookEx(hook: isize, code: i32, w_param: usize, l_param: isize) -> isize;
    fn DispatchMessageW(message: *const Message) -> isize;
    fn GetAsyncKeyState(key: i32) -> i16;
    fn GetForegroundWindow() -> isize;
    fn GetKeyboardLayout(thread_id: u32) -> isize;
    fn GetKeyboardState(state: *mut u8) -> i32;
    fn GetMessageW(message: *mut Message, hwnd: isize, min: u32, max: u32) -> i32;
    fn GetWindowThreadProcessId(hwnd: isize, process_id: *mut u32) -> u32;
    fn LoadKeyboardLayoutW(layout: *const u16, flags: u32) -> isize;
    fn PeekMessageW(message: *mut Message, hwnd: isize, min: u32, max: u32, remove: u32) -> i32;
    fn PostMessageW(hwnd: isize, message: u32, w_param: usize, l_param: isize) -> i32;
    fn PostThreadMessageW(thread_id: u32, message: u32, w_param: usize, l_param: isize) -> i32;
    fn SendInput(count: u32, inputs: *const Input, size: i32) -> u32;
    fn SetWindowsHookExW(id: i32, proc: HookProc, module: isize, thread_id: u32) -> isize;
    fn ToUnicodeEx(
        virtual_key: u32,
        scan_code: u32,
        state: *const u8,
        output: *mut u16,
        output_capacity: i32,
        flags: u32,
        layout: isize,
    ) -> i32;
    fn TranslateMessage(message: *const Message) -> i32;
    fn UnhookWindowsHookEx(hook: isize) -> i32;
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetCurrentThreadId() -> u32;
    fn GetModuleHandleExW(flags: u32, name_or_address: *const u16, module: *mut isize) -> i32;
}

#[derive(Default)]
struct HookState {
    physical_units: Vec<String>,
    modifiers: HashSet<u32>,
    suppressed_keys: HashSet<u32>,
    pending: Option<PendingReplacement>,
}

struct PendingReplacement {
    backspaces: usize,
    text: String,
    switch_requested: bool,
    switched: bool,
    target_language: u32,
}

impl HookState {
    fn word(&self) -> String {
        self.physical_units.concat()
    }

    fn clear_word(&mut self) {
        self.physical_units.clear();
    }
}

fn state() -> &'static Mutex<HookState> {
    STATE.get_or_init(|| Mutex::new(HookState::default()))
}

fn thread_slot() -> &'static Mutex<Option<JoinHandle<()>>> {
    THREAD.get_or_init(|| Mutex::new(None))
}

pub fn start() -> i32 {
    *state()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner()) = HookState::default();
    let mut thread_guard = thread_slot()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if thread_guard.is_some() {
        return 1;
    }

    let (ready_sender, ready_receiver) = mpsc::sync_channel(1);
    let thread = thread::Builder::new()
        .name("MultilangRustKeyboardHook".to_owned())
        .spawn(move || unsafe { hook_thread(ready_sender) });
    let Ok(thread) = thread else {
        return -3;
    };

    match ready_receiver.recv_timeout(Duration::from_secs(5)) {
        Ok(0) => {
            *thread_guard = Some(thread);
            0
        }
        Ok(error) => {
            let _ = thread.join();
            error
        }
        Err(_) => {
            let thread_id = HOOK_THREAD_ID.load(Ordering::Acquire);
            if thread_id != 0 {
                unsafe { PostThreadMessageW(thread_id, WM_QUIT, 0, 0) };
            }
            let _ = thread.join();
            -4
        }
    }
}

pub fn stop() -> i32 {
    let mut thread_guard = thread_slot()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let Some(thread) = thread_guard.take() else {
        return 0;
    };
    let thread_id = HOOK_THREAD_ID.load(Ordering::Acquire);
    let posted = if thread_id == 0 {
        0
    } else {
        unsafe { PostThreadMessageW(thread_id, WM_QUIT, 0, 0) }
    };
    if posted == 0 {
        // A failed post normally means the hook thread has already exited.
        // Join it so its callback pointers can safely be released by Python.
        if thread.join().is_err() {
            return -6;
        }
        HOOK_THREAD_ID.store(0, Ordering::Release);
        *state()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = HookState::default();
        return 0;
    }
    if thread.join().is_err() {
        return -6;
    }
    *state()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner()) = HookState::default();
    HOOK_THREAD_ID.store(0, Ordering::Release);
    0
}

unsafe fn hook_thread(ready: mpsc::SyncSender<i32>) {
    let thread_id = GetCurrentThreadId();
    HOOK_THREAD_ID.store(thread_id, Ordering::Release);

    let mut message: Message = zeroed();
    PeekMessageW(&mut message, 0, 0, 0, 0);

    let mut module = 0isize;
    let address = hook_proc as *const () as *const u16;
    if GetModuleHandleExW(0x0000_0004 | 0x0000_0002, address, &mut module) == 0 {
        let _ = ready.send(-7);
        return;
    }
    let hook = SetWindowsHookExW(WH_KEYBOARD_LL, Some(hook_proc), module, 0);
    if hook == 0 {
        let _ = ready.send(-8);
        return;
    }
    HOOK_HANDLE.store(hook, Ordering::Release);
    let _ = ready.send(0);

    loop {
        let result = GetMessageW(&mut message, 0, 0, 0);
        if result <= 0 {
            break;
        }
        if message.message == WM_APP_INJECT {
            send_pending_replacement();
        } else {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }

    UnhookWindowsHookEx(hook);
    HOOK_HANDLE.store(0, Ordering::Release);
    HOOK_THREAD_ID.store(0, Ordering::Release);
}

unsafe extern "system" fn hook_proc(code: i32, w_param: usize, l_param: isize) -> isize {
    if code == HC_ACTION {
        let event = &*(l_param as *const LowLevelKeyboardEvent);
        if event.flags & LLKHF_INJECTED == 0 && process_key_event(w_param as u32, event) {
            return 1;
        }
    }
    CallNextHookEx(HOOK_HANDLE.load(Ordering::Acquire), code, w_param, l_param)
}

unsafe fn process_key_event(message: u32, event: &LowLevelKeyboardEvent) -> bool {
    let key = event.vk_code;
    let down = matches!(message, WM_KEYDOWN | WM_SYSKEYDOWN);
    let up = matches!(message, WM_KEYUP | WM_SYSKEYUP);
    if !down && !up {
        return false;
    }

    let mut state = state()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if up {
        if state.suppressed_keys.remove(&key) {
            return true;
        }
        if is_modifier(key) {
            state.modifiers.remove(&key);
        }
        return false;
    }

    if state.suppressed_keys.contains(&key) {
        return true;
    }
    if is_modifier(key) {
        state.modifiers.insert(key);
        state.clear_word();
        return false;
    }
    if !state.modifiers.is_empty() {
        state.clear_word();
        return false;
    }
    if matches!(key, VK_SHIFT | VK_LSHIFT | VK_RSHIFT) {
        return false;
    }

    let hwnd = GetForegroundWindow();
    if key == VK_SPACE {
        return complete_word(&mut state, " ", key, hwnd);
    }
    if key == VK_BACK {
        state.physical_units.pop();
        return false;
    }
    if key == VK_DELETE
        || matches!(key, VK_TAB | VK_RETURN | VK_ESCAPE)
        || matches!(key, 0x21..=0x28 | 0x2D)
    {
        state.clear_word();
        return false;
    }

    let translated = translate_key(key, event.scan_code, hwnd);
    if translated.is_empty() {
        state.clear_word();
        return false;
    }
    if translated.chars().all(is_word_character) {
        state.physical_units.push(translated);
        return false;
    }
    if translated.chars().count() == 1 {
        return complete_word(&mut state, &translated, key, hwnd);
    }
    state.clear_word();
    false
}

fn is_modifier(key: u32) -> bool {
    matches!(
        key,
        VK_CONTROL | VK_LCONTROL | VK_RCONTROL | VK_MENU | VK_LMENU | VK_RMENU | VK_LWIN | VK_RWIN
    )
}

fn is_word_character(character: char) -> bool {
    character.is_alphanumeric()
        || matches!(character, '\'' | '’' | '-' | '`' | '[' | ']' | ';' | ',' | '.' | '/')
}

unsafe fn complete_word(state: &mut HookState, boundary: &str, key: u32, hwnd: isize) -> bool {
    let word = state.word();
    let backspaces = state.physical_units.len();
    state.clear_word();
    if word.is_empty() {
        return false;
    }

    let callback_address = CORRECTION_CALLBACK.load(Ordering::Acquire);
    if callback_address == 0 {
        return false;
    }
    let callback: crate::CorrectionCallback = std::mem::transmute(callback_address);
    let mut output = [0u8; 2048];
    let mut output_len = 0usize;
    let mut target_language = 0u32;
    let corrected = callback(
        word.as_ptr(),
        word.len(),
        output.as_mut_ptr(),
        output.len(),
        &mut output_len,
        &mut target_language,
    );
    let switch_requested = target_language & SWITCH_LAYOUT_FLAG != 0;
    let target_language = target_language & 0x00FF;
    if corrected == 0 || output_len > output.len() || !matches!(target_language, 1 | 2) {
        return false;
    }
    let Ok(mut replacement) = String::from_utf8(output[..output_len].to_vec()) else {
        report_status("فشل التصحيح: نص الاستبدال غير صالح");
        return false;
    };
    replacement.push_str(boundary);

    let switched = switch_requested && switch_layout(hwnd, target_language);
    state.pending = Some(PendingReplacement {
        backspaces,
        text: replacement,
        switch_requested,
        switched,
        target_language,
    });
    if PostThreadMessageW(HOOK_THREAD_ID.load(Ordering::Acquire), WM_APP_INJECT, 0, 0) == 0 {
        state.pending = None;
        report_status("فشل إدخال النص المصحح");
        return false;
    }
    state.suppressed_keys.insert(key);
    true
}

unsafe fn translate_key(key: u32, scan_code: u32, hwnd: isize) -> String {
    let thread_id = if hwnd == 0 {
        0
    } else {
        GetWindowThreadProcessId(hwnd, null_mut())
    };
    let layout = GetKeyboardLayout(thread_id);
    let mut keyboard_state = [0u8; 256];
    GetKeyboardState(keyboard_state.as_mut_ptr());
    for modifier in [
        VK_SHIFT,
        VK_LSHIFT,
        VK_RSHIFT,
        VK_CONTROL,
        VK_LCONTROL,
        VK_RCONTROL,
        VK_MENU,
        VK_LMENU,
        VK_RMENU,
    ] {
        if GetAsyncKeyState(modifier as i32) < 0 {
            keyboard_state[modifier as usize] |= 0x80;
        }
    }
    keyboard_state[key as usize] |= 0x80;
    let mut output = [0u16; 8];
    let length = ToUnicodeEx(
        key,
        scan_code,
        keyboard_state.as_ptr(),
        output.as_mut_ptr(),
        output.len() as i32,
        TU_NO_STATE_CHANGE,
        layout,
    );
    if length <= 0 {
        return String::new();
    }
    String::from_utf16_lossy(&output[..(length as usize).min(output.len())])
}

unsafe fn switch_layout(hwnd: isize, target_language: u32) -> bool {
    if hwnd == 0 {
        return false;
    }
    let thread_id = GetWindowThreadProcessId(hwnd, null_mut());
    if thread_id == 0 {
        return false;
    }
    let target_language_id = if target_language == 1 { 0x0409 } else { 0x0401 };
    if (GetKeyboardLayout(thread_id) as u16) == target_language_id {
        return true;
    }

    let layout_name: Vec<u16> = if target_language == 1 {
        "00000409\0".encode_utf16().collect()
    } else {
        "00000401\0".encode_utf16().collect()
    };
    let layout = LoadKeyboardLayoutW(layout_name.as_ptr(), 0);
    if layout == 0 {
        return false;
    }
    if PostMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, layout) == 0 {
        return false;
    }

    let deadline = std::time::Instant::now() + Duration::from_millis(500);
    while std::time::Instant::now() < deadline {
        if (GetKeyboardLayout(thread_id) as u16) == target_language_id {
            return true;
        }
        thread::sleep(Duration::from_millis(10));
    }
    false
}

unsafe fn send_pending_replacement() {
    let pending = {
        let mut state = state()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.pending.take()
    };
    let Some(pending) = pending else {
        return;
    };

    let inserted = send_replacement(pending.backspaces, &pending.text);
    if !inserted {
        report_status("فشل إدخال النص المصحح");
    } else if pending.switched {
        report_status(if pending.target_language == 1 {
            "تم التصحيح — English"
        } else {
            "تم التصحيح — العربية"
        });
    } else if pending.switch_requested {
        report_status(if pending.target_language == 1 {
            "MULTILANG_SWITCH_FAILED:en"
        } else {
            "MULTILANG_SWITCH_FAILED:ar"
        });
    } else {
        report_status("تم تصحيح الكلمة");
    }
}

unsafe fn send_replacement(backspaces: usize, text: &str) -> bool {
    let mut inputs = Vec::with_capacity(backspaces * 2 + text.encode_utf16().count() * 2);
    for _ in 0..backspaces {
        inputs.push(keyboard_input(VK_BACK as u16, 0, 0));
        inputs.push(keyboard_input(VK_BACK as u16, 0, KEYEVENTF_KEYUP));
    }
    for unit in text.encode_utf16() {
        inputs.push(keyboard_input(0, unit, KEYEVENTF_UNICODE));
        inputs.push(keyboard_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP));
    }

    let mut sent_total = 0usize;
    while sent_total < inputs.len() {
        let sent = SendInput(
            (inputs.len() - sent_total) as u32,
            inputs.as_ptr().add(sent_total),
            size_of::<Input>() as i32,
        ) as usize;
        if sent == 0 {
            return false;
        }
        sent_total += sent;
    }
    true
}

fn keyboard_input(virtual_key: u16, scan_code: u16, flags: u32) -> Input {
    Input {
        input_type: INPUT_KEYBOARD,
        data: InputData {
            keyboard: KeyboardInput {
                virtual_key,
                scan_code,
                flags,
                time: 0,
                extra_info: 0,
            },
        },
    }
}

fn report_status(message: &str) {
    let callback_address = STATUS_CALLBACK.load(Ordering::Acquire);
    if callback_address == 0 {
        return;
    }
    let callback: crate::StatusCallback = unsafe { std::mem::transmute(callback_address) };
    unsafe { callback(message.as_ptr(), message.len()) };
}
