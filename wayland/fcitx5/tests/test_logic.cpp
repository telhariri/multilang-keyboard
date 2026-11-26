#include "logic.hpp"

#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#define CHECK(cond)                                                               \
    do {                                                                          \
        if (!(cond)) {                                                            \
            std::cerr << "CHECK failed at line " << __LINE__ << ": " #cond "\n"; \
            std::exit(1);                                                         \
        }                                                                         \
    } while (false)

int main() {
    using namespace multilang;

    // 1. Preedit cursor MUST be in bytes, not UTF-8 codepoint count.
    {
        Composition comp;
        const std::string letters[] = {"ا", "ل", "س", "ل", "ا", "م"};
        for (const auto &ch : letters) {
            comp.append(ch, ch);
        }
        CHECK(utf8CharCount(comp.displayText) == 6);
        CHECK(comp.cursorByteOffset() == 12);
    }

    // 2. Digits must NOT be captured into preedit (prevents duplication on click).
    {
        for (char32_t cp = U'0'; cp <= U'9'; ++cp) {
            CHECK(isDigit(cp));
            CHECK(!isWordCharacter(cp));
        }
        CHECK(isDigit(0x0660));
        CHECK(!isWordCharacter(0x0665));
    }

    // 3. Arabic Presentation Forms (XKB 'ara' key 'b' -> U+FEFB ﻻ) must be word letters.
    {
        CHECK(isArabicLetter(0xFEFB));
        CHECK(isArabicLetter(0xFEFC));
        CHECK(isArabicLetter(0xFEF7));
        CHECK(isArabicLetter(0xFEF9));
        CHECK(isArabicLetter(0xFEF5));
        CHECK(isWordCharacter(0xFEFB));
    }

    // 4. Shifted Arabic 101 characters including Arabic question mark '؟' (must NOT become 'ظ').
    {
        CHECK(arabicKeyText(53, false) == "ظ");
        CHECK(arabicKeyText(53, true) == "؟");
        CHECK(arabicKeyText(35, true) == "أ");
        CHECK(arabicKeyText(21, true) == "إ");
        CHECK(arabicKeyText(49, true) == "آ");
        CHECK(arabicKeyText(34, true) == ucs4ToUtf8(0xFEF7));
        CHECK(arabicKeyText(20, true) == ucs4ToUtf8(0xFEF9));
        CHECK(arabicKeyText(48, true) == ucs4ToUtf8(0xFEF5));
        CHECK(isBoundaryCharacter(0x061F)); // ؟
        CHECK(isBoundaryCharacter(0x060C)); // ،
        CHECK(isBoundaryCharacter(0x061B)); // ؛
    }

    // 5. Dual displayText / detectText tracking for Shift on Arabic layout.
    {
        Composition comp;
        char upperH = shiftedPhysicalUsLetter(U'أ', 35 + 8, true, false, comp.displayText.empty());
        CHECK(upperH == 'H');
        comp.append("أ", std::string(1, upperH));
        comp.append("ث", "ث");
        comp.append("م", "م");
        comp.append("م", "م");
        comp.append("خ", "خ");
        CHECK(comp.displayText == "أثممخ");
        CHECK(comp.detectText == "Hثممخ");

        Composition py;
        char upperP = shiftedPhysicalUsLetter(0x061B, 25 + 8, true, false, py.displayText.empty());
        CHECK(upperP == 'P');

        Composition mid;
        mid.append("ن", "ن");
        char midP = shiftedPhysicalUsLetter(0x061B, 25 + 8, true, false, mid.displayText.empty());
        CHECK(midP == 0);

        char q = shiftedPhysicalUsLetter(0x061F, 53 + 8, true, false, true);
        CHECK(q == 0);
    }

    // 6. Backspace keeps displayText and detectText synchronized and clears capturing when empty.
    {
        Composition comp;
        comp.capturing = true;
        comp.append("أ", "H");
        comp.append("ث", "ث");
        CHECK(comp.backspace());
        CHECK(comp.displayText == "أ");
        CHECK(comp.detectText == "H");
        CHECK(comp.capturing);
        CHECK(comp.backspace());
        CHECK(comp.displayText.empty());
        CHECK(comp.detectText.empty());
        CHECK(!comp.capturing);
    }

    // 7. US Shift + CapsLock XOR logic.
    {
        CHECK(usKeySymbol(30, false, false) == U'a');
        CHECK(usKeySymbol(30, true, false) == U'A');
        CHECK(usKeySymbol(30, false, true) == U'A');
        CHECK(usKeySymbol(30, true, true) == U'a');
    }

    // 8. Protocol parser validation.
    {
        auto ok = parseFixResponse("FIX\ten\t?\thello");
        CHECK(ok.has_value());
        CHECK(ok->language == "en");
        CHECK(ok->switched == "?");
        CHECK(ok->replacement == "hello");

        CHECK(!parseFixResponse("NONE").has_value());
        CHECK(!parseFixResponse("FIX\tfr\t1\tbonjour").has_value());
        CHECK(!parseFixResponse("FIX\ten\t1\t").has_value());
    }

    // 9. Two-language keyboard IM matching and target lookup.
    {
        CHECK(keyboardImLanguage("keyboard-us") == "en");
        CHECK(keyboardImLanguage("keyboard-us-intl") == "en");
        CHECK(keyboardImLanguage("keyboard-gb") == "en");
        CHECK(keyboardImLanguage("keyboard-ara") == "ar");
        CHECK(keyboardImLanguage("keyboard-ara-digits") == "ar");
        CHECK(keyboardImLanguage("keyboard-sy") == "ar");
        CHECK(keyboardImLanguage("keyboard-eg") == "ar");
        CHECK(keyboardImLanguage("pinyin").empty());

        const std::vector<std::string> group{"keyboard-us", "keyboard-ara"};
        CHECK(findTargetKeyboardIm(group, "ar") == "keyboard-ara");
        CHECK(findTargetKeyboardIm(group, "en") == "keyboard-us");
        CHECK(findTargetKeyboardIm(group, "fr").empty());
    }

    std::cout << "All multilang Fcitx5 C++ logic tests passed.\n";
    return 0;
}
