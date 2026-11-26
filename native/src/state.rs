use std::collections::HashSet;
use std::time::{Duration, Instant};

pub const IDLE_RESET_DURATION: Duration = Duration::from_secs(8);

pub struct PendingReplacement {
    pub backspaces: usize,
    pub text: String,
    pub hwnd: isize,
    pub switch_requested: bool,
    pub target_language: u32,
}

#[derive(Default)]
pub struct HookState {
    pub typed_chars: Vec<char>,
    pub last_hwnd: isize,
    pub last_key_time: Option<Instant>,
    pub modifiers: HashSet<u32>,
    pub shift_keys: HashSet<u32>,
    pub suppressed_keys: HashSet<u32>,
    pub pending: Option<PendingReplacement>,
}

impl HookState {
    pub fn on_hwnd_or_idle(&mut self, hwnd: isize, now: Instant) {
        if self.last_hwnd != hwnd {
            self.last_hwnd = hwnd;
            self.clear_word();
        } else if self
            .last_key_time
            .is_some_and(|last| now.saturating_duration_since(last) > IDLE_RESET_DURATION)
        {
            self.clear_word();
        }
        self.last_key_time = Some(now);
    }

    pub fn push_text(&mut self, text: &str) {
        self.typed_chars.extend(text.chars());
    }

    pub fn backspace(&mut self) {
        self.typed_chars.pop();
    }

    pub fn word(&self) -> String {
        self.typed_chars.iter().collect()
    }

    pub fn backspaces(&self) -> usize {
        self.typed_chars.len()
    }

    pub fn clear_word(&mut self) {
        self.typed_chars.clear();
    }
}

pub fn is_word_character(character: char) -> bool {
    // Digits are intentionally excluded so numbers are never buffered/rewritten.
    character.is_alphabetic()
        || matches!(
            character,
            '\'' | '’' | '-' | '`' | '[' | ']' | ';' | ',' | '.' | '/'
        )
}

pub fn safe_vk_index(vk_code: u32) -> Option<usize> {
    if vk_code < 256 {
        Some(vk_code as usize)
    } else {
        None
    }
}

pub fn encode_utf16_units(text: &str) -> Vec<u16> {
    text.encode_utf16().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lam_alef_counts_as_two_backspaces_and_single_backspace_deletes_alef() {
        let mut state = HookState::default();
        // On Windows Arabic 101, physical 'b' produces "لا" (2 Unicode chars).
        state.push_text("لا");
        state.push_text("خ");
        state.push_text("خ");
        state.push_text("ن");
        assert_eq!(state.word(), "لاخخن");
        assert_eq!(state.backspaces(), 5);

        state.backspace();
        assert_eq!(state.word(), "لاخخ");
        assert_eq!(state.backspaces(), 4);

        // Removing 'ا' leaves 'ل' just like Windows editor Backspace does.
        let mut lam_only = HookState::default();
        lam_only.push_text("لا");
        lam_only.backspace();
        assert_eq!(lam_only.word(), "ل");
        assert_eq!(lam_only.backspaces(), 1);
    }

    #[test]
    fn digits_are_excluded_from_word_characters() {
        for ch in '0'..='9' {
            assert!(!is_word_character(ch));
        }
        assert!(!is_word_character('٥'));
        assert!(is_word_character('a'));
        assert!(is_word_character('ض'));
        assert!(is_word_character(';'));
    }

    #[test]
    fn hwnd_change_and_idle_timeout_clear_buffered_word() {
        let mut state = HookState::default();
        let t0 = Instant::now();
        state.on_hwnd_or_idle(100, t0);
        state.push_text("اثممخ");
        assert_eq!(state.word(), "اثممخ");

        // Changing foreground window clears buffer immediately.
        state.on_hwnd_or_idle(200, t0 + Duration::from_millis(50));
        assert!(state.word().is_empty());

        // Exceeding idle timeout clears buffer.
        state.push_text("sghl");
        state.on_hwnd_or_idle(200, t0 + IDLE_RESET_DURATION + Duration::from_secs(1));
        assert!(state.word().is_empty());
    }

    #[test]
    fn vk_index_bounds_are_enforced() {
        assert_eq!(safe_vk_index(0), Some(0));
        assert_eq!(safe_vk_index(255), Some(255));
        assert_eq!(safe_vk_index(256), None);
        assert_eq!(safe_vk_index(u32::MAX), None);
    }

    #[test]
    fn utf16_units_encode_bmp_and_surrogate_pairs() {
        let units = encode_utf16_units("hello 😀 ");
        assert_eq!(units.len(), 9);
        assert_eq!(units[6], 0xD83D);
        assert_eq!(units[7], 0xDE00);
        assert_eq!(units[8], b' ' as u16);
    }
}
