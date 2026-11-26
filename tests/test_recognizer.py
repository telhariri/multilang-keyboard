import unittest

from wordfreq import top_n_list, zipf_frequency

import main
import recognizer


class RecognizerRegressionTests(unittest.TestCase):
    def test_english_words_with_gh_are_recovered_from_arabic_layout(self):
        cases = {
            "قهلاف": "right",
            "ىهلاف": "night",
            "ةهلاف": "might",
            "مهلاف": "light",
            "فاخعلاف": "thought",
            "فاقخعلا": "through",
            "ثىخعلا": "enough",
            "لاقهلاف": "bright",
            "ثهلاف": "eight",
            "شمفاخعلا": "although",
        }
        for typed, expected in cases.items():
            with self.subTest(typed=typed):
                self.assertEqual(main.TypingMonitor._recognize(typed), (expected, "en"))

    def test_arabic_presentation_forms_map_to_english(self):
        self.assertEqual(main.TypingMonitor._recognize("\ufefbخخن"), ("book", "en"))
        self.assertEqual(main.TypingMonitor._recognize("\ufef7خخن"), (None, None))
        self.assertTrue(recognizer.has_arabic("\ufefbخخن"))
        self.assertEqual(recognizer.to_english("\ufefbخخن"), "book")

    def test_shifted_hamza_and_capslock_arabic_words_are_recovered(self):
        cases = {
            "Hkh": "أنا",
            "Ygn": "إلى",
            "hgNk": "الآن",
            "hBk": "الآن",
            "Hpl]": "أحمد",
            "Hvd]": "أريد",
            "Y`h": "إذا",
            "hG,g": "الأول",
            "SGHL": "سلام",
            "Sghl": "سلام",
        }
        for typed, expected in cases.items():
            with self.subTest(typed=typed):
                self.assertEqual(main.TypingMonitor._recognize(typed), (expected, "ar"))

    def test_trailing_punctuation_arabic_to_english_for_stems_of_three_or_more(self):
        self.assertEqual(main.TypingMonitor._recognize("اثممخو"), ("hello,", "en"))
        self.assertEqual(main.TypingMonitor._recognize("اثممخز"), ("hello.", "en"))

    def test_english_words_with_trailing_punctuation_are_never_corrupted(self):
        for token in ("of.", "a,", "a;", "i,", "s,", "d,", "t;", "r'", "l,", "p/", "o'", "uk;", "u."):
            with self.subTest(token=token):
                self.assertEqual(main.TypingMonitor._recognize(token), (None, None))

    def test_windows_shift_arabic_buffers_never_corrupt_real_arabic_words(self):
        for token in ("Hي", "Nخر", "خطH", "هى"):
            with self.subTest(token=token):
                self.assertEqual(main.TypingMonitor._recognize(token), (None, None))

    def test_code_tokens_urls_and_numbers_are_untouched(self):
        for token in ("main.py", "google.com", "v1.0", "arr[i]", "test.txt", "README.md", "user/repo"):
            with self.subTest(token=token):
                self.assertEqual(main.TypingMonitor._recognize(token), (None, None))

    def test_arabic_words_ending_with_dal_bracket_still_recover(self):
        for typed, expected in (("uk]", "عند"), ("u]]", "عدد"), ("Hp]", "أحد"), ("r]", "قد")):
            with self.subTest(typed=typed):
                self.assertEqual(main.TypingMonitor._recognize(typed), (expected, "ar"))

    def test_benchmark_zero_false_positives_and_high_recall(self):
        en_top2k = top_n_list("en", 2000)
        en_top5k = top_n_list("en", 5000)
        ar_top5k = top_n_list("ar", 5000)

        fp_punct = [
            w + p
            for w in en_top2k
            for p in (",", ".", ";", "'", "/")
            if recognizer.recognize(w + p, zipf_fn=zipf_frequency)[1] == "ar"
        ]
        self.assertEqual(fp_punct, [])

        fp_en = [
            w for w in en_top5k
            if recognizer.recognize(w, zipf_fn=zipf_frequency)[1] == "ar"
        ]
        self.assertEqual(fp_en, [])

        fp_ar = [
            w for w in ar_top5k
            if recognizer.recognize(w, zipf_fn=zipf_frequency)[1] == "en"
        ]
        self.assertEqual(fp_ar, [])

        def sim_win_ar(word):
            out, i = [], 0
            while i < len(word):
                if word.startswith("لأ", i):
                    out.append("G")
                    i += 2
                elif word.startswith("لإ", i):
                    out.append("T")
                    i += 2
                elif word.startswith("لآ", i):
                    out.append("B")
                    i += 2
                elif word[i] == "أ":
                    out.append("H")
                    i += 1
                elif word[i] == "إ":
                    out.append("Y")
                    i += 1
                elif word[i] == "آ":
                    out.append("N")
                    i += 1
                else:
                    out.append(word[i])
                    i += 1
            return "".join(out)

        fp_win_shift = [
            (w, sim_win_ar(w))
            for w in ar_top5k
            if sim_win_ar(w) != w
            and recognizer.recognize(sim_win_ar(w), zipf_fn=zipf_frequency) != (None, None)
        ]
        self.assertEqual(fp_win_shift, [])

        en_words = [w for w in en_top2k[:500] if w.isascii() and len(w) >= 2]
        en_hits = sum(
            1 for w in en_words
            if recognizer.recognize(recognizer.to_arabic(w), zipf_fn=zipf_frequency) == (w, "en")
        )
        self.assertGreater(en_hits / len(en_words), 0.98)

        ar_to_en_shifted = dict(recognizer.ARABIC_TO_ENGLISH)
        ar_to_en_shifted.update({"أ": "H", "إ": "Y", "آ": "N"})

        def sim_type_ar_on_en(word):
            out, i = [], 0
            while i < len(word):
                if word.startswith("لأ", i):
                    out.append("G")
                    i += 2
                elif word.startswith("لإ", i):
                    out.append("T")
                    i += 2
                elif word.startswith("لآ", i):
                    out.append("B")
                    i += 2
                elif word.startswith("لا", i):
                    out.append("b")
                    i += 2
                else:
                    out.append(ar_to_en_shifted.get(word[i], word[i]))
                    i += 1
            return "".join(out)

        ar_words = [w for w in ar_top5k[:500] if len(w) >= 2 and recognizer.has_arabic(w)]
        ar_hits = sum(
            1 for w in ar_words
            if recognizer.recognize(sim_type_ar_on_en(w), zipf_fn=zipf_frequency)[1] == "ar"
        )
        self.assertGreater(ar_hits / len(ar_words), 0.93)


if __name__ == "__main__":
    unittest.main()
