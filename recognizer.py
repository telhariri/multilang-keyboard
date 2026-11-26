"""Arabic/English keyboard layout mapping and language recognition engine."""

from __future__ import annotations

import re

try:
    from wordfreq import zipf_frequency
except ImportError:
    zipf_frequency = None

# Arabic PC keyboard (Arabic 101 / XKB ara), mapped by physical QWERTY key position.
ARABIC_TO_ENGLISH = {
    "ذ": "`", "ض": "q", "ص": "w", "ث": "e", "ق": "r", "ف": "t",
    "غ": "y", "ع": "u", "ه": "i", "خ": "o", "ح": "p", "ج": "[",
    "د": "]", "ش": "a", "س": "s", "ي": "d", "ب": "f", "ل": "g",
    "ا": "h", "ت": "j", "ن": "k", "م": "l", "ك": ";", "ط": "'",
    "ئ": "z", "ء": "x", "ؤ": "c", "ر": "v", "لا": "b", "ى": "n",
    "ة": "m", "و": ",", "ز": ".", "ظ": "/",
}
ENGLISH_TO_ARABIC = {value: key for key, value in ARABIC_TO_ENGLISH.items()}

# Shifted Arabic 101 hamza ligatures and letters produced with Shift+letter on US QWERTY.
SHIFTED_HAMZA_MAP = {
    "H": "أ",
    "Y": "إ",
    "N": "آ",
    "G": "لأ",
    "T": "لإ",
    "B": "لآ",
}

# Full map of shifted US letter keys -> Arabic 101 characters (used to reconstruct
# intended Arabic text when a low-level hook records physical uppercase US letters).
SHIFTED_ASCII_TO_ARABIC = {
    **SHIFTED_HAMZA_MAP,
    "Q": "َ",
    "W": "ً",
    "E": "ُ",
    "R": "ٌ",
    "A": "ِ",
    "S": "ٍ",
    "X": "ْ",
    "J": "ـ",
    "I": "÷",
    "O": "×",
    "P": "؛",
    "K": "،",
    "L": "/",
    "Z": "~",
    "C": "}",
    "V": "{",
    "D": "]",
    "F": "[",
    "U": "`",
    "M": "'",
}

# Extended Arabic -> US physical mapping including Unicode Arabic Presentation Forms
# (emitted directly by Linux XKB 'ara' for key 'b' etc.) and shifted Arabic characters.
ARABIC_TO_ENGLISH_EXT = {
    **ARABIC_TO_ENGLISH,
    "\ufefb": "b", "\ufefc": "b",  # ﻻ / ﻼ
    "\ufef7": "G", "\ufef8": "G",  # ﻷ / ﻸ
    "\ufef9": "T", "\ufefa": "T",  # ﻹ / ﻺ
    "\ufef5": "B", "\ufef6": "B",  # ﻵ / ﻶ
    "أ": "H", "إ": "Y", "آ": "N",
    "َ": "Q", "ً": "W", "ُ": "E", "ٌ": "R",
    "ِ": "A", "ٍ": "S", "ْ": "X", "ـ": "J",
    "÷": "I", "×": "O", "؛": "P", "،": "K",
}

# Offline vocabulary is deliberately conservative: unknown words are left alone.
ENGLISH_WORDS = set("""
a about after again all also an and any are as at be because been before being best better big but by can come could day did do does down each even every few find first for from get give go good great had has have he her here him his how I if in into is it its just know language last learn let like little long look made make man many may me more most much my name need new no not now of off on one only or other our out over people place please point program python question quick read really right said same say see she should so some take than that the their them then there these they thing think this those time to too two under up use very want was way we well were what when where which who will with word work world would write yes you your hello hi
""".split())

ARABIC_WORDS = set("""
انا نحن انت انتم هو هي هم هذا هذه ذلك تلك هنا هناك من الى عن على في مع بين بعد قبل كل بعض لا ما ماذا كيف متى اين لماذا نعم ليس كان تكون يكون كنت قد سوف لن لم اذا أو ثم ايضا فقط جدا اكثر اقل مثل لان لكن حتى حيث كما الذي التي الذين شيء اشياء شخص الناس انسان بيت منزل باب كتاب قلم مدرسة جامعة عمل وقت يوم ايام سنة شهر ساعة صباح مساء ليل نهار اليوم غدا امس الان دائما ربما ممكن صحيح خطا جيد جميل كبير صغير جديد قديم اول اخر افضل مهم سهلة سهل صعب مشكلة حل فكرة سؤال جواب لغة عربي العربية انجليزي الانجليزية انكليزية الانكليزية للانكليزية يبدل يجب الواجهات اللوحة بشكل الكثير الاخطاء معدل ثقة كلمة كلمات حرف حروف لوحة مفاتيح كتابة تطبيق برنامج نظام ويندوز لينكس مرحبا اهلا وسهلا شكرا عفوا من فضلك بخير الحمد لله السلام عليكم وعليكم انت انا اريد اكتب كتب يكتب كتابة نص رسالة مساعدة اذهب يأتي قال يقول يعرف يفهم يريد يعمل يستخدم يقوم يبدأ ينتهي المستخدم الكمبيوتر الحاسوب شاشة نافذة اعدادات خيارات
""".split())

BOUNDARY_PUNCTUATION = set(".,!?;:")
ENGLISH_WORD_PATTERN = re.compile(r"[a-z]+(?:'[a-z]+)*\Z")
TRAILING_ENGLISH_EN_PATTERN = re.compile(r"([a-z]+(?:'[a-z]+)*)([,.])\Z")
TRAILING_ENGLISH_PUNCTUATION = set(",.;'/")
ARABIC_RANGE_START = "\u0600"
ARABIC_RANGE_END = "\u06ff"
ARABIC_PRES_START = "\ufe70"
ARABIC_PRES_END = "\ufeff"
ARABIC_LAYOUT_SYMBOLS = set("`[];',./")
MIN_LANGUAGE_SCORE = 4.0
MIN_LANGUAGE_MARGIN = 2.0
MAX_NATIVE_SCORE = 4.0


def _passes_language_confidence(target_score: float, other_score: float) -> bool:
    return (
        target_score >= MIN_LANGUAGE_SCORE
        and other_score < MAX_NATIVE_SCORE
        and target_score - other_score >= MIN_LANGUAGE_MARGIN
    )


def has_arabic(text: str) -> bool:
    return any(
        (ARABIC_RANGE_START <= char <= ARABIC_RANGE_END)
        or (ARABIC_PRES_START <= char <= ARABIC_PRES_END)
        for char in text
    )


def to_english(text: str) -> str:
    # "لا" is emitted by one physical key ('b') on the standard Arabic PC layout.
    result = []
    i = 0
    while i < len(text):
        two = text[i:i + 2]
        if two == "لا":
            result.append("b")
            i += 2
        elif two == "لأ":
            result.append("G")
            i += 2
        elif two == "لإ":
            result.append("T")
            i += 2
        elif two == "لآ":
            result.append("B")
            i += 2
        else:
            result.append(ARABIC_TO_ENGLISH_EXT.get(text[i], text[i]))
            i += 1
    return "".join(result)


def to_english_candidates(text: str, max_ambiguous: int = 4) -> list[str]:
    """Generate physical US key sequences accounting for 'لا' ('b' vs 'gh')."""
    choices: list[tuple[str, ...]] = []
    ambiguous_count = 0
    i = 0
    while i < len(text):
        two = text[i:i + 2]
        if two == "لا":
            if ambiguous_count < max_ambiguous:
                choices.append(("b", "gh"))
                ambiguous_count += 1
            else:
                choices.append(("b",))
            i += 2
        elif two == "لأ":
            if ambiguous_count < max_ambiguous:
                choices.append(("G", "gH"))
                ambiguous_count += 1
            else:
                choices.append(("G",))
            i += 2
        elif two == "لإ":
            if ambiguous_count < max_ambiguous:
                choices.append(("T", "gY"))
                ambiguous_count += 1
            else:
                choices.append(("T",))
            i += 2
        elif two == "لآ":
            if ambiguous_count < max_ambiguous:
                choices.append(("B", "gN"))
                ambiguous_count += 1
            else:
                choices.append(("B",))
            i += 2
        else:
            ch = text[i]
            if ch in ("\ufefb", "\ufefc"):
                choices.append(("b",))
            else:
                choices.append((ARABIC_TO_ENGLISH_EXT.get(ch, ch),))
            i += 1

    results = [""]
    for opts in choices:
        if len(opts) == 1:
            opt = opts[0]
            results = [item + opt for item in results]
        else:
            results = [item + opt for item in results for opt in opts]
    return results


def to_arabic(text: str) -> str:
    return "".join(ENGLISH_TO_ARABIC.get(char.lower(), char) for char in text)


def to_arabic_with_hamzas(text: str) -> str:
    return "".join(
        SHIFTED_HAMZA_MAP[char]
        if char in SHIFTED_HAMZA_MAP
        else ENGLISH_TO_ARABIC.get(char.lower(), char)
        for char in text
    )


def reconstruct_arabic_from_shifted(text: str) -> str:
    """Reconstruct intended Arabic from uppercase ASCII letters recorded under Shift."""
    return "".join(SHIFTED_ASCII_TO_ARABIC.get(char, char) for char in text)


_UNSET = object()


def recognize(word: str, zipf_fn=_UNSET) -> tuple[str | None, str | None]:
    """Recognize if a word was typed on the wrong layout and return (replacement, target_lang)."""
    if zipf_fn is _UNSET:
        zipf_fn = zipf_frequency
    if len(word) < 2 or zipf_fn is None:
        return None, None
    normalized = word.casefold()

    if has_arabic(word):
        reconstructed_ar = reconstruct_arabic_from_shifted(word)
        reconstructed_norm = reconstructed_ar.casefold()

        # Mixed-script-to-Arabic check: only when word has lowercase ASCII or
        # Arabic-layout punctuation symbols (not pure Windows Shift uppercase like "Hثممخ").
        has_lower_or_symbol = any(
            c.isascii() and (c.islower() or c in ARABIC_LAYOUT_SYMBOLS)
            for c in word
        )
        if has_lower_or_symbol:
            candidate_ar = (
                to_arabic_with_hamzas(word) if not word.isupper() else to_arabic(word)
            )
            if candidate_ar != word:
                arabic_score = zipf_fn(candidate_ar, "ar")
                if any(char in ARABIC_LAYOUT_SYMBOLS for char in word):
                    english_score = 0.0
                else:
                    en_cands = to_english_candidates(word)
                    english_score = max(
                        (
                            zipf_fn(cand.casefold(), "en")
                            for cand in en_cands
                            if ENGLISH_WORD_PATTERN.fullmatch(cand.casefold())
                        ),
                        default=0.0,
                    )
                if _passes_language_confidence(arabic_score, english_score):
                    return candidate_ar, "ar"

        if normalized in ARABIC_WORDS or reconstructed_norm in ARABIC_WORDS:
            return None, None

        arabic_score = max(
            zipf_fn(normalized, "ar"),
            zipf_fn(reconstructed_norm, "ar") if reconstructed_ar != word else 0.0,
        )

        best_score = 0.0
        best_match: tuple[str, str, str] | None = None
        for raw_cand in to_english_candidates(word):
            folded = raw_cand.casefold()
            if ENGLISH_WORD_PATTERN.fullmatch(folded):
                stem = folded
                suffix = ""
                raw_stem = raw_cand
            else:
                match = TRAILING_ENGLISH_EN_PATTERN.fullmatch(folded)
                if not match or len(match.group(1)) < 3:
                    continue
                stem = match.group(1)
                suffix = match.group(2)
                raw_stem = raw_cand[:-1]
            score = zipf_fn(stem, "en")
            if score > best_score:
                best_score = score
                best_match = (raw_stem, stem, suffix)

        if best_match is not None and _passes_language_confidence(best_score, arabic_score):
            raw_stem, stem, suffix = best_match
            if any(char.isascii() and char.isupper() for char in raw_stem):
                return raw_stem + suffix, "en"
            return stem + suffix, "en"
        return None, None

    if word.isascii() and all(char.lower() in ENGLISH_TO_ARABIC for char in word):
        if normalized in ENGLISH_WORDS:
            return None, None

        has_symbols = any(char in ARABIC_LAYOUT_SYMBOLS for char in word)
        if has_symbols:
            stripped = word.rstrip("".join(TRAILING_ENGLISH_PUNCTUATION))
            if stripped and stripped != word and stripped.isalpha():
                english_score = zipf_fn(stripped.casefold(), "en")
            else:
                english_score = 0.0
        else:
            english_score = zipf_fn(normalized, "en")

        has_upper = any(char.isupper() for char in word)
        has_lower_or_sym = any(
            char.islower() or char in ARABIC_LAYOUT_SYMBOLS for char in word
        )
        if (
            has_upper
            and has_lower_or_sym
            and any(char in SHIFTED_HAMZA_MAP for char in word)
        ):
            hamza_cand = to_arabic_with_hamzas(word)
            hamza_score = zipf_fn(hamza_cand, "ar")
            if _passes_language_confidence(hamza_score, english_score):
                return hamza_cand, "ar"

        lower_cand = to_arabic(word)
        lower_score = zipf_fn(lower_cand, "ar")
        if _passes_language_confidence(lower_score, english_score):
            return lower_cand, "ar"

    return None, None
