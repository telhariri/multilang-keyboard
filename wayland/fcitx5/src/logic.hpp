#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

namespace multilang {

inline constexpr size_t kMaximumWordBytes = 2048;
inline constexpr size_t kMaximumReplyBytes = 4096;

inline std::string ucs4ToUtf8(char32_t codepoint) {
    std::string out;
    if (codepoint == 0 || codepoint > 0x10FFFF) {
        return out;
    }
    if (codepoint <= 0x7F) {
        out.push_back(static_cast<char>(codepoint));
    } else if (codepoint <= 0x7FF) {
        out.push_back(static_cast<char>(0xC0 | ((codepoint >> 6) & 0x1F)));
        out.push_back(static_cast<char>(0x80 | (codepoint & 0x3F)));
    } else if (codepoint <= 0xFFFF) {
        out.push_back(static_cast<char>(0xE0 | ((codepoint >> 12) & 0x0F)));
        out.push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (codepoint & 0x3F)));
    } else {
        out.push_back(static_cast<char>(0xF0 | ((codepoint >> 18) & 0x07)));
        out.push_back(static_cast<char>(0x80 | ((codepoint >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (codepoint & 0x3F)));
    }
    return out;
}

inline size_t utf8CharCount(std::string_view text) {
    size_t count = 0;
    for (unsigned char byte : text) {
        if ((byte & 0xC0) != 0x80) {
            ++count;
        }
    }
    return count;
}

inline char32_t firstCodepoint(std::string_view text) {
    if (text.empty()) {
        return 0;
    }
    const auto *bytes = reinterpret_cast<const unsigned char *>(text.data());
    if (bytes[0] < 0x80) {
        return bytes[0];
    }
    if ((bytes[0] & 0xE0) == 0xC0 && text.size() >= 2) {
        return (static_cast<char32_t>(bytes[0] & 0x1F) << 6) |
               static_cast<char32_t>(bytes[1] & 0x3F);
    }
    if ((bytes[0] & 0xF0) == 0xE0 && text.size() >= 3) {
        return (static_cast<char32_t>(bytes[0] & 0x0F) << 12) |
               (static_cast<char32_t>(bytes[1] & 0x3F) << 6) |
               static_cast<char32_t>(bytes[2] & 0x3F);
    }
    if ((bytes[0] & 0xF8) == 0xF0 && text.size() >= 4) {
        return (static_cast<char32_t>(bytes[0] & 0x07) << 18) |
               (static_cast<char32_t>(bytes[1] & 0x3F) << 12) |
               (static_cast<char32_t>(bytes[2] & 0x3F) << 6) |
               static_cast<char32_t>(bytes[3] & 0x3F);
    }
    return 0;
}

inline std::string_view keyboardImLanguage(std::string_view imName) {
    constexpr std::string_view kPrefix = "keyboard-";
    if (imName.rfind(kPrefix, 0) != 0) {
        return {};
    }
    std::string_view layout = imName.substr(kPrefix.size());
    const size_t dash = layout.find('-');
    if (dash != std::string_view::npos) {
        layout = layout.substr(0, dash);
    }
    if (layout == "us" || layout == "gb" || layout == "uk" ||
        layout == "ca" || layout == "au") {
        return "en";
    }
    if (layout == "ara" || layout == "sy" || layout == "eg" || layout == "iq" ||
        layout == "ma" || layout == "dz" || layout == "tn" || layout == "jo" ||
        layout == "lb" || layout == "sa" || layout == "ae" || layout == "kw" ||
        layout == "qa" || layout == "bh" || layout == "om" || layout == "ye" ||
        layout == "ly" || layout == "sd" || layout == "ps") {
        return "ar";
    }
    return {};
}

inline std::string findTargetKeyboardIm(const std::vector<std::string> &ims,
                                        std::string_view targetLanguage) {
    for (const auto &im : ims) {
        if (keyboardImLanguage(im) == targetLanguage) {
            return im;
        }
    }
    return {};
}

inline bool isArabicPresentationForm(char32_t codepoint) {
    return codepoint >= 0xFEF5 && codepoint <= 0xFEFC;
}

inline bool isArabicLetter(char32_t codepoint) {
    return (codepoint >= 0x0620 && codepoint <= 0x064A) ||
           (codepoint >= 0x066E && codepoint <= 0x06D3) ||
           (codepoint >= 0x0750 && codepoint <= 0x077F) ||
           (codepoint >= 0x08A0 && codepoint <= 0x08FF) ||
           isArabicPresentationForm(codepoint);
}

inline bool isArabicMark(char32_t codepoint) {
    return (codepoint >= 0x064B && codepoint <= 0x065F) || codepoint == 0x0670;
}

inline bool isArabicLayoutSymbol(char32_t codepoint) {
    return codepoint == U'`' || codepoint == U'[' || codepoint == U']' ||
           codepoint == U';' || codepoint == U'\'' || codepoint == U',' ||
           codepoint == U'.' || codepoint == U'/';
}

inline bool isDigit(char32_t codepoint) {
    return (codepoint >= U'0' && codepoint <= U'9') ||
           (codepoint >= 0x0660 && codepoint <= 0x0669) ||
           (codepoint >= 0x06F0 && codepoint <= 0x06F9);
}

inline bool isWordCharacter(char32_t codepoint) {
    // Digits are intentionally excluded so numbers are never buffered in preedit.
    return (codepoint >= U'a' && codepoint <= U'z') ||
           (codepoint >= U'A' && codepoint <= U'Z') ||
           isArabicLetter(codepoint) ||
           isArabicMark(codepoint) ||
           isArabicLayoutSymbol(codepoint);
}

inline bool isWordJoiner(char32_t codepoint) {
    return codepoint == U'\'' || codepoint == 0x2019 || codepoint == U'-';
}

inline bool isBoundaryCharacter(char32_t codepoint) {
    return codepoint == U' ' ||
           (codepoint >= 0x21 && codepoint <= 0x2F) ||
           (codepoint >= 0x3A && codepoint <= 0x40) ||
           (codepoint >= 0x5B && codepoint <= 0x60) ||
           (codepoint >= 0x7B && codepoint <= 0x7E) ||
           codepoint == 0x00A0 ||
           codepoint == 0x00D7 || // ×
           codepoint == 0x00F7 || // ÷
           codepoint == 0x060C || // ،
           codepoint == 0x061B || // ؛
           codepoint == 0x061F;   // ؟
}

inline char32_t usKeySymbol(uint32_t evdevCode, bool shifted, bool capsLock = false) {
    if (evdevCode >= 2 && evdevCode <= 11) {
        static constexpr char normal[] = "1234567890";
        static constexpr char shift[] = "!@#$%^&*()";
        return shifted ? shift[evdevCode - 2] : normal[evdevCode - 2];
    }
    const bool upper = shifted ^ capsLock;
    if (evdevCode >= 16 && evdevCode <= 25) {
        static constexpr char row[] = "qwertyuiop";
        const char value = row[evdevCode - 16];
        return static_cast<char32_t>(upper ? value - ('a' - 'A') : value);
    }
    if (evdevCode >= 30 && evdevCode <= 38) {
        static constexpr char row[] = "asdfghjkl";
        const char value = row[evdevCode - 30];
        return static_cast<char32_t>(upper ? value - ('a' - 'A') : value);
    }
    if (evdevCode >= 44 && evdevCode <= 50) {
        static constexpr char row[] = "zxcvbnm";
        const char value = row[evdevCode - 44];
        return static_cast<char32_t>(upper ? value - ('a' - 'A') : value);
    }
    switch (evdevCode) {
    case 12: return shifted ? U'_' : U'-';
    case 13: return shifted ? U'+' : U'=';
    case 14: return U'\b';
    case 15: return U'\t';
    case 26: return shifted ? U'{' : U'[';
    case 27: return shifted ? U'}' : U']';
    case 28: return U'\n';
    case 39: return shifted ? U':' : U';';
    case 40: return shifted ? U'"' : U'\'';
    case 41: return shifted ? U'~' : U'`';
    case 43: return shifted ? U'|' : U'\\';
    case 51: return shifted ? U'<' : U',';
    case 52: return shifted ? U'>' : U'.';
    case 53: return shifted ? U'?' : U'/';
    case 57: return U' ';
    default: return 0;
    }
}

inline char usLetterForEvdev(uint32_t evdevCode, bool shifted) {
    char base = 0;
    if (evdevCode >= 16 && evdevCode <= 25) {
        static constexpr char row[] = "qwertyuiop";
        base = row[evdevCode - 16];
    } else if (evdevCode >= 30 && evdevCode <= 38) {
        static constexpr char row[] = "asdfghjkl";
        base = row[evdevCode - 30];
    } else if (evdevCode >= 44 && evdevCode <= 50) {
        static constexpr char row[] = "zxcvbnm";
        base = row[evdevCode - 44];
    }
    if (base == 0) {
        return 0;
    }
    return shifted ? static_cast<char>(base - ('a' - 'A')) : base;
}

inline std::string arabicKeyText(uint32_t evdevCode, bool shifted) {
    if (shifted) {
        if (evdevCode >= 2 && evdevCode <= 11) {
            static constexpr char shift[] = "!@#$%^&*)(";
            return std::string(1, shift[evdevCode - 2]);
        }
        switch (evdevCode) {
        case 12: return "_";
        case 13: return "+";
        case 16: return ucs4ToUtf8(0x064E); // َ
        case 17: return ucs4ToUtf8(0x064B); // ً
        case 18: return ucs4ToUtf8(0x064F); // ُ
        case 19: return ucs4ToUtf8(0x064C); // ٌ
        case 20: return ucs4ToUtf8(0xFEF9); // ﻹ
        case 21: return ucs4ToUtf8(U'إ');
        case 22: return "`";
        case 23: return ucs4ToUtf8(0x00F7); // ÷
        case 24: return ucs4ToUtf8(0x00D7); // ×
        case 25: return ucs4ToUtf8(0x061B); // ؛
        case 26: return "<";
        case 27: return ">";
        case 30: return ucs4ToUtf8(0x0650); // ِ
        case 31: return ucs4ToUtf8(0x064D); // ٍ
        case 32: return "]";
        case 33: return "[";
        case 34: return ucs4ToUtf8(0xFEF7); // ﻷ
        case 35: return ucs4ToUtf8(U'أ');
        case 36: return ucs4ToUtf8(0x0640); // ـ
        case 37: return ucs4ToUtf8(0x060C); // ،
        case 38: return "/";
        case 39: return ":";
        case 40: return "\"";
        case 41: return ucs4ToUtf8(0x0651); // ّ
        case 43: return "|";
        case 44: return "~";
        case 45: return ucs4ToUtf8(0x0652); // ْ
        case 46: return "}";
        case 47: return "{";
        case 48: return ucs4ToUtf8(0xFEF5); // ﻵ
        case 49: return ucs4ToUtf8(U'آ');
        case 50: return "'";
        case 51: return ",";
        case 52: return ".";
        case 53: return ucs4ToUtf8(0x061F); // ؟
        default: return {};
        }
    }
    switch (evdevCode) {
    case 16: return ucs4ToUtf8(U'ض');
    case 17: return ucs4ToUtf8(U'ص');
    case 18: return ucs4ToUtf8(U'ث');
    case 19: return ucs4ToUtf8(U'ق');
    case 20: return ucs4ToUtf8(U'ف');
    case 21: return ucs4ToUtf8(U'غ');
    case 22: return ucs4ToUtf8(U'ع');
    case 23: return ucs4ToUtf8(U'ه');
    case 24: return ucs4ToUtf8(U'خ');
    case 25: return ucs4ToUtf8(U'ح');
    case 26: return ucs4ToUtf8(U'ج');
    case 27: return ucs4ToUtf8(U'د');
    case 30: return ucs4ToUtf8(U'ش');
    case 31: return ucs4ToUtf8(U'س');
    case 32: return ucs4ToUtf8(U'ي');
    case 33: return ucs4ToUtf8(U'ب');
    case 34: return ucs4ToUtf8(U'ل');
    case 35: return ucs4ToUtf8(U'ا');
    case 36: return ucs4ToUtf8(U'ت');
    case 37: return ucs4ToUtf8(U'ن');
    case 38: return ucs4ToUtf8(U'م');
    case 39: return ucs4ToUtf8(U'ك');
    case 40: return ucs4ToUtf8(U'ط');
    case 41: return ucs4ToUtf8(U'ذ');
    case 44: return ucs4ToUtf8(U'ئ');
    case 45: return ucs4ToUtf8(U'ء');
    case 46: return ucs4ToUtf8(U'ؤ');
    case 47: return ucs4ToUtf8(U'ر');
    case 48: return ucs4ToUtf8(0xFEFB); // ﻻ
    case 49: return ucs4ToUtf8(U'ى');
    case 50: return ucs4ToUtf8(U'ة');
    case 51: return ucs4ToUtf8(U'و');
    case 52: return ucs4ToUtf8(U'ز');
    case 53: return ucs4ToUtf8(U'ظ');
    default: return {};
    }
}

inline uint32_t evdevFromKeycode(uint32_t keycode, bool shifted, char32_t observed) {
    if (keycode == 0) {
        return 0;
    }
    const uint32_t candidates[] = {keycode, keycode >= 8 ? keycode - 8 : keycode};
    for (uint32_t candidate : candidates) {
        const char32_t us = usKeySymbol(candidate, shifted);
        const std::string ar = arabicKeyText(candidate, shifted);
        const char32_t arCp = firstCodepoint(ar);
        if ((us != 0 && us == observed) || (arCp != 0 && arCp == observed) ||
            (candidate == 48 && !shifted &&
             (observed == 0xFEFB || observed == 0xFEFC))) {
            return candidate;
        }
    }
    if (keycode >= 8 && usKeySymbol(keycode - 8, shifted) != 0) {
        return keycode - 8;
    }
    return keycode;
}

inline bool isArabicShiftedSymbolOnLetterKey(char32_t cp) {
    return cp == 0x061B /* ؛ */ || cp == 0x060C /* ، */ ||
           cp == 0x0640 /* ـ */ || cp == U'}' || cp == U'{' ||
           cp == U'[' || cp == U']' || cp == U'/' || cp == U'~' ||
           cp == 0x00F7 /* ÷ */ || cp == 0x00D7 /* × */ ||
           cp == U'`' || cp == U'\'' || cp == 0x2019;
}

inline char shiftedPhysicalUsLetter(char32_t observed, uint32_t keycode,
                                    bool shifted, bool altGr,
                                    bool bufferEmpty) {
    if (!shifted || altGr || observed == 0) {
        return 0;
    }
    if ((observed >= U'a' && observed <= U'z') ||
        (observed >= U'A' && observed <= U'Z') ||
        isDigit(observed)) {
        return 0;
    }
    const uint32_t evdev = evdevFromKeycode(keycode, shifted, observed);
    const char letter = usLetterForEvdev(evdev, true);
    if (letter == 0) {
        return 0;
    }
    if (isArabicLetter(observed) || isArabicMark(observed)) {
        return letter;
    }
    if (bufferEmpty && isArabicShiftedSymbolOnLetterKey(observed)) {
        return letter;
    }
    return 0;
}

struct Composition {
    std::string displayText;
    std::string detectText;
    std::vector<size_t> displayByteLengths;
    std::vector<size_t> detectByteLengths;
    bool capturing = false;
    std::unordered_set<uint32_t> consumedKeys;

    void append(std::string_view displayChar, std::string_view detectChar) {
        displayText.append(displayChar);
        detectText.append(detectChar);
        displayByteLengths.push_back(displayChar.size());
        detectByteLengths.push_back(detectChar.size());
    }

    bool backspace() {
        if (displayText.empty()) {
            capturing = false;
            return false;
        }
        const size_t dispPop = displayByteLengths.empty()
                                   ? 1
                                   : std::min(displayByteLengths.back(), displayText.size());
        if (!displayByteLengths.empty()) {
            displayByteLengths.pop_back();
        }
        displayText.erase(displayText.size() - dispPop);

        if (!detectText.empty()) {
            const size_t detPop = detectByteLengths.empty()
                                      ? 1
                                      : std::min(detectByteLengths.back(), detectText.size());
            if (!detectByteLengths.empty()) {
                detectByteLengths.pop_back();
            }
            detectText.erase(detectText.size() - detPop);
        }

        if (displayText.empty()) {
            displayByteLengths.clear();
            detectByteLengths.clear();
            detectText.clear();
            capturing = false;
        }
        return true;
    }

    void clear() {
        displayText.clear();
        detectText.clear();
        displayByteLengths.clear();
        detectByteLengths.clear();
        capturing = false;
    }

    int cursorByteOffset() const {
        return static_cast<int>(displayText.size());
    }
};

struct FixResponse {
    std::string language;
    std::string switched;
    std::string replacement;
};

inline std::optional<FixResponse> parseFixResponse(std::string_view response) {
    if (response.rfind("FIX\t", 0) != 0) {
        return std::nullopt;
    }
    const size_t languageEnd = response.find('\t', 4);
    if (languageEnd == std::string_view::npos) {
        return std::nullopt;
    }
    const size_t switchedEnd = response.find('\t', languageEnd + 1);
    if (switchedEnd == std::string_view::npos) {
        return std::nullopt;
    }
    std::string language(response.substr(4, languageEnd - 4));
    std::string switched(response.substr(languageEnd + 1, switchedEnd - languageEnd - 1));
    std::string replacement(response.substr(switchedEnd + 1));
    if ((language != "en" && language != "ar") ||
        (switched != "0" && switched != "1" && switched != "?") ||
        replacement.empty()) {
        return std::nullopt;
    }
    return FixResponse{std::move(language), std::move(switched), std::move(replacement)};
}

} // namespace multilang
