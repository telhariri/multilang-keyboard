use crate::state::{is_word_character, safe_vk_index, HookState, PendingReplacement};
use crate::{CORRECTION_CALLBACK, STATUS_CALLBACK};
use std::collections::HashSet;
use std::mem::{size_of, zeroed};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::ptr::null_mut;
use std::sync::atomic::{AtomicIsize, AtomicU32, Ordering};
use std::sync::{mpsc, Mutex, OnceLock};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const WH_KEYBOARD_LL: i32 = 13;
const WH_MOUSE_LL: i32 = 14;
const HC_ACTION: i32 = 0;
const WM_KEYDOWN: u32 = 0x0100;
const WM_KEYUP: u32 = 0x0101;
const WM_SYSKEYDOWN: u32 = 0x0104;
const WM_SYSKEYUP: u32 = 0x0105;
const WM_LBUTTONDOWN: u32 = 0x0201;
const WM_RBUTTONDOWN: u32 = 0x0204;
const WM_MBUTTONDOWN: u32 = 0x0207;
const WM_QUIT: u32 = 0x0012;
const WM_INPUTLANGCHANGEREQUEST: u32 = 0x0050;
const WM_APP_INJECT: u32 = 0x8001;
const LLKHF_INJECTED: u32 = 0x10;
const TU_NO_STATE_CHANGE: u32 = 0x0004;
const SWITCH_LAYOUT_FLAG: u32 = 0x0100;
const INPUT_KEYBOARD: u32 = 1;
const KEYEVENTF_KEYUP: u32 = 0x0002;
const KEYEVENTF_UNICODE: u32 = 0x0004;
const PRIMARYLANGID_ARABIC: u16 = 0x0001;
const PRIMARYLANGID_ENGLISH: u16 = 0x0009;

const VK_BACK: u32 = 0x08;
const VK_TAB: u32 = 0x09;
const VK_RETURN: u32 = 0x0D;
const VK_SHIFT: u32 = 0x10;
const VK_CONTROL: u32 = 0x11;
const VK_MENU: u32 = 0x12;
const VK_CAPITAL: u32 = 0x14;
const VK_ESCAPE: u32 = 0x1B;
const VK_SPACE: u32 = 0x20;
const VK_DELETE: u32 = 0x2E;
const VK_NUMLOCK: u32 = 0x90;
const VK_SCROLL: u32 = 0x91;
const VK_LSHIFT: u32 = 0xA0;
const VK_RSHIFT: u32 = 0xA1;
const VK_LCONTROL: u32 = 0xA2;
const VK_RCONTROL: u32 = 0xA3;
const VK_LMENU: u32 = 0xA4;
const VK_RMENU: u32 = 0xA5;
const VK_LWIN: u32 = 0x5B;
const VK_RWIN: u32 = 0x5C;

static HOOK_HANDLE: AtomicIsize = AtomicIsize::new(0);
static MOUSE_HOOK_HANDLE: AtomicIsize = AtomicIsize::new(0);
static HOOK_THREAD_ID: AtomicU32 = AtomicU32::new(0);
static US_KEYBOARD_LAYOUT: AtomicIsize = AtomicIsize::new(0);
static STATE: OnceLock<Mutex<HookState>> = OnceLock::new();
static THREAD: OnceLock<Mutex<Option<JoinHandle<()>>>> = OnceLock::new();
static LAYOUT_SENDER: OnceLock<Mutex<Option<mpsc::Sender<LayoutSwitchRequest>>>> = OnceLock::new();
static LAYOUT_THREAD: OnceLock<Mutex<Option<JoinHandle<()>>>> = OnceLock::new();

type HookProc = Option<unsafe extern "system" fn(i32, usize, isize) -> isize>;

struct LayoutSwitchRequest {
    hwnd: isize,
    target_language: u32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct Point {
    x: i32,
    y: i32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct Rect {
    left: i32,
    top: i32,
    right: i32,
    bottom: i32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct GuiThreadInfo {
    cb_size: u32,
    flags: u32,
    hwnd_active: isize,
    hwnd_focus: isize,
    hwnd_capture: isize,
    hwnd_menu_owner: isize,
    hwnd_move_size: isize,
    hwnd_caret: isize,
    rc_caret: Rect,
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
    fn GetGUIThreadInfo(thread_id: u32, info: *mut GuiThreadInfo) -> i32;
    fn GetKeyState(key: i32) -> i16;
    fn GetKeyboardLayout(thread_id: u32) -> isize;
    fn GetKeyboardLayoutList(count: i32, list: *mut isize) -> i32;
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

fn state() -> &'static Mutex<HookState> {
    STATE.get_or_init(|| Mutex::new(HookState::default()))
}

fn thread_slot() -> &'static Mutex<Option<JoinHandle<()>>> {
    THREAD.get_or_init(|| Mutex::new(None))
}

fn layout_sender_slot() -> &'static Mutex<Option<mpsc::Sender<LayoutSwitchRequest>>> {
    LAYOUT_SENDER.get_or_init(|| Mutex::new(None))
}

fn layout_thread_slot() -> &'static Mutex<Option<JoinHandle<()>>> {
    LAYOUT_THREAD.get_or_init(|| Mutex::new(None))
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

    let (layout_tx, layout_rx) = mpsc::channel::<LayoutSwitchRequest>();
    let layout_thread = thread::Builder::new()
        .name("MultilangLayoutSwitcher".to_owned())
        .spawn(move || layout_worker(layout_rx));
    let Ok(layout_thread) = layout_thread else {
        return -5;
    };

    let (ready_sender, ready_receiver) = mpsc::sync_channel(1);
    let thread = thread::Builder::new()
        .name("MultilangRustKeyboardHook".to_owned())
        .spawn(move || unsafe { hook_thread(ready_sender) });
    let Ok(thread) = thread else {
        drop(layout_tx);
        let _ = layout_thread.join();
        return -3;
    };

    match ready_receiver.recv_timeout(Duration::from_secs(5)) {
        Ok(0) => {
            *layout_sender_slot()
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(layout_tx);
            *layout_thread_slot()
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(layout_thread);
            *thread_guard = Some(thread);
            0
        }
        Ok(error) => {
            let _ = thread.join();
            drop(layout_tx);
            let _ = layout_thread.join();
            error
        }
        Err(_) => {
            let thread_id = HOOK_THREAD_ID.load(Ordering::Acquire);
            if thread_id != 0 {
                unsafe { PostThreadMessageW(thread_id, WM_QUIT, 0, 0) };
            }
            let _ = thread.join();
            drop(layout_tx);
            let _ = layout_thread.join();
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
    if thread_id != 0 {
        unsafe { PostThreadMessageW(thread_id, WM_QUIT, 0, 0) };
    }
    let join_result = thread.join();

    *layout_sender_slot()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner()) = None;
    if let Some(layout_thread) = layout_thread_slot()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .take()
    {
        let _ = layout_thread.join();
    }

    HOOK_THREAD_ID.store(0, Ordering::Release);
    *state()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner()) = HookState::default();

    if join_result.is_err() {
        return -6;
    }
    0
}

fn layout_worker(receiver: mpsc::Receiver<LayoutSwitchRequest>) {
    while let Ok(request) = receiver.recv() {
        let switched = unsafe { switch_layout(request.hwnd, request.target_language) };
        if switched {
            report_status(if request.target_language == 1 {
                "تم التصحيح — English"
            } else {
                "تم التصحيح — العربية"
            });
        } else {
            report_status(if request.target_language == 1 {
                "MULTILANG_SWITCH_FAILED:en"
            } else {
                "MULTILANG_SWITCH_FAILED:ar"
            });
        }
    }
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

    let mouse_hook = SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_hook_proc), module, 0);
    MOUSE_HOOK_HANDLE.store(mouse_hook, Ordering::Release);

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

    if mouse_hook != 0 {
        UnhookWindowsHookEx(mouse_hook);
        MOUSE_HOOK_HANDLE.store(0, Ordering::Release);
    }
    UnhookWindowsHookEx(hook);
    HOOK_HANDLE.store(0, Ordering::Release);
    HOOK_THREAD_ID.store(0, Ordering::Release);
}

unsafe extern "system" fn hook_proc(code: i32, w_param: usize, l_param: isize) -> isize {
    if code == HC_ACTION && l_param != 0 {
        let suppress = catch_unwind(AssertUnwindSafe(|| {
            let event = &*(l_param as *const LowLevelKeyboardEvent);
            (event.flags & LLKHF_INJECTED == 0) && process_key_event(w_param as u32, event)
        }))
        .unwrap_or(false);
        if suppress {
            return 1;
        }
    }
    CallNextHookEx(HOOK_HANDLE.load(Ordering::Acquire), code, w_param, l_param)
}

unsafe extern "system" fn mouse_hook_proc(code: i32, w_param: usize, l_param: isize) -> isize {
    if code == HC_ACTION
        && matches!(
            w_param as u32,
            WM_LBUTTONDOWN | WM_RBUTTONDOWN | WM_MBUTTONDOWN
        )
    {
        let _ = catch_unwind(AssertUnwindSafe(|| {
            let mut st = state()
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            st.clear_word();
        }));
    }
    CallNextHookEx(
        MOUSE_HOOK_HANDLE.load(Ordering::Acquire),
        code,
        w_param,
        l_param,
    )
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
        if is_shift(key) {
            state.shift_keys.remove(&key);
            return false;
        }
        if is_modifier(key) {
            state.modifiers.remove(&key);
        }
        return false;
    }

    if state.suppressed_keys.contains(&key) {
        return true;
    }
    if is_lock_key(key) {
        return false;
    }
    if is_shift(key) {
        state.shift_keys.insert(key);
        return false;
    }
    if is_modifier(key) {
        state.modifiers.insert(key);
        state.clear_word();
        return false;
    }

    // Reconcile stuck modifier/shift keys in case KEYUP was missed across UAC or Alt+Tab.
    state
        .modifiers
        .retain(|&vk| GetAsyncKeyState(vk as i32) < 0);
    state
        .shift_keys
        .retain(|&vk| GetAsyncKeyState(vk as i32) < 0);

    if !state.modifiers.is_empty() {
        state.clear_word();
        return false;
    }

    let hwnd = GetForegroundWindow();
    state.on_hwnd_or_idle(hwnd, Instant::now());

    if key == VK_SPACE {
        return complete_word(&mut state, " ", key, hwnd);
    }
    if key == VK_BACK {
        state.backspace();
        return false;
    }
    if key == VK_DELETE
        || matches!(key, VK_TAB | VK_RETURN | VK_ESCAPE)
        || matches!(key, 0x21..=0x28 | 0x2D)
    {
        state.clear_word();
        return false;
    }

    let active_layout = if hwnd == 0 {
        0
    } else {
        GetKeyboardLayout(GetWindowThreadProcessId(hwnd, null_mut()))
    };
    let mut translated = translate_key(key, event.scan_code, hwnd, &state.shift_keys);
    if !state.shift_keys.is_empty() && ((active_layout as u16) & 0x03FF) == PRIMARYLANGID_ARABIC {
        // Arabic 101 maps Shift+letter to Arabic marks and symbols. Use the
        // physical US key only for detection; the original key still reaches
        // the foreground app unchanged.
        let mut us_layout = US_KEYBOARD_LAYOUT.load(Ordering::Acquire);
        if us_layout == 0 {
            let us_name: Vec<u16> = "00000409\0".encode_utf16().collect();
            us_layout = LoadKeyboardLayoutW(us_name.as_ptr(), 0);
            if us_layout != 0 {
                US_KEYBOARD_LAYOUT.store(us_layout, Ordering::Release);
            }
        }
        if us_layout != 0 {
            let physical =
                translate_key_with_layout(key, event.scan_code, us_layout, &state.shift_keys);
            if physical.len() == 1 && physical.as_bytes()[0].is_ascii_alphabetic() {
                translated = physical;
            }
        }
    }
    if translated.is_empty() {
        state.clear_word();
        return false;
    }
    if translated.chars().all(is_word_character) {
        state.push_text(&translated);
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

fn is_shift(key: u32) -> bool {
    matches!(key, VK_SHIFT | VK_LSHIFT | VK_RSHIFT)
}

fn is_lock_key(key: u32) -> bool {
    matches!(key, VK_CAPITAL | VK_NUMLOCK | VK_SCROLL)
}

unsafe fn complete_word(state: &mut HookState, boundary: &str, key: u32, hwnd: isize) -> bool {
    let word = state.word();
    let backspaces = state.backspaces();
    state.clear_word();
    if word.is_empty() || state.pending.is_some() {
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

    // Queue injection and return immediately; layout switching runs asynchronously
    // on MultilangLayoutSwitcher so WH_KEYBOARD_LL never approaches Windows' hook timeout.
    state.pending = Some(PendingReplacement {
        backspaces,
        text: replacement,
        hwnd,
        switch_requested,
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

unsafe fn translate_key(
    key: u32,
    scan_code: u32,
    hwnd: isize,
    shift_keys: &HashSet<u32>,
) -> String {
    let thread_id = if hwnd == 0 {
        0
    } else {
        GetWindowThreadProcessId(hwnd, null_mut())
    };
    let layout = GetKeyboardLayout(thread_id);
    translate_key_with_layout(key, scan_code, layout, shift_keys)
}

unsafe fn translate_key_with_layout(
    key: u32,
    scan_code: u32,
    layout: isize,
    shift_keys: &HashSet<u32>,
) -> String {
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
    if (GetKeyState(VK_CAPITAL as i32) & 1) != 0 {
        keyboard_state[VK_CAPITAL as usize] |= 0x01;
    }
    let shift_is_down = !shift_keys.is_empty();
    keyboard_state[VK_SHIFT as usize] = if shift_is_down {
        keyboard_state[VK_SHIFT as usize] | 0x80
    } else {
        keyboard_state[VK_SHIFT as usize] & !0x80
    };
    for shift_key in [VK_LSHIFT, VK_RSHIFT] {
        keyboard_state[shift_key as usize] = if shift_keys.contains(&shift_key) {
            keyboard_state[shift_key as usize] | 0x80
        } else {
            keyboard_state[shift_key as usize] & !0x80
        };
    }
    if let Some(index) = safe_vk_index(key) {
        keyboard_state[index] |= 0x80;
    }
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

unsafe fn find_installed_layout(target_primary: u16) -> Option<isize> {
    let count = GetKeyboardLayoutList(0, null_mut());
    if count <= 0 {
        return None;
    }
    let mut layouts = vec![0isize; count as usize];
    let actual = GetKeyboardLayoutList(count, layouts.as_mut_ptr());
    if actual <= 0 {
        return None;
    }
    layouts
        .into_iter()
        .take(actual as usize)
        .find(|&hkl| ((hkl as u16) & 0x03FF) == target_primary)
}

unsafe fn switch_layout(hwnd: isize, target_language: u32) -> bool {
    if hwnd == 0 {
        return false;
    }
    let thread_id = GetWindowThreadProcessId(hwnd, null_mut());
    if thread_id == 0 {
        return false;
    }
    let target_primary = if target_language == 1 {
        PRIMARYLANGID_ENGLISH
    } else {
        PRIMARYLANGID_ARABIC
    };
    if ((GetKeyboardLayout(thread_id) as u16) & 0x03FF) == target_primary {
        return true;
    }

    let layout = find_installed_layout(target_primary).unwrap_or_else(|| {
        let layout_name: Vec<u16> = if target_language == 1 {
            "00000409\0".encode_utf16().collect()
        } else {
            "00000401\0".encode_utf16().collect()
        };
        LoadKeyboardLayoutW(layout_name.as_ptr(), 0)
    });
    if layout == 0 {
        return false;
    }

    let mut target_hwnd = hwnd;
    let mut gui: GuiThreadInfo = zeroed();
    gui.cb_size = size_of::<GuiThreadInfo>() as u32;
    if GetGUIThreadInfo(thread_id, &mut gui) != 0 && gui.hwnd_focus != 0 {
        target_hwnd = gui.hwnd_focus;
    }

    if PostMessageW(target_hwnd, WM_INPUTLANGCHANGEREQUEST, 0, layout) == 0
        && (target_hwnd == hwnd || PostMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, layout) == 0)
    {
        return false;
    }

    let mut posted_fallback = target_hwnd == hwnd;
    let start = Instant::now();
    let deadline = start + Duration::from_millis(500);
    while Instant::now() < deadline {
        if ((GetKeyboardLayout(thread_id) as u16) & 0x03FF) == target_primary {
            return true;
        }
        if !posted_fallback && start.elapsed() >= Duration::from_millis(150) {
            PostMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, layout);
            posted_fallback = true;
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
        return;
    }
    if pending.switch_requested {
        let sender = layout_sender_slot()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        if let Some(tx) = sender {
            let _ = tx.send(LayoutSwitchRequest {
                hwnd: pending.hwnd,
                target_language: pending.target_language,
            });
            return;
        }
        report_status("تم تصحيح الكلمة");
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

#[cfg(test)]
mod shift_tests {
    use super::*;

    #[test]
    fn tracked_shift_translates_a_us_letter_as_uppercase() {
        let layout_name: Vec<u16> = "00000409\0".encode_utf16().collect();
        let layout = unsafe { LoadKeyboardLayoutW(layout_name.as_ptr(), 0) };
        assert_ne!(layout, 0, "could not load the US keyboard layout");

        let shifted = HashSet::from([VK_LSHIFT]);
        let translated = unsafe { translate_key_with_layout(0x48, 0x23, layout, &shifted) };
        assert_eq!(translated, "H");
    }
}
