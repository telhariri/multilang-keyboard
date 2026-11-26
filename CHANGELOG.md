# Changelog

جميع التغييرات البارزة في **Multilang** موثّقة في هذا الملف.

---

## [1.1.0] - 2026-11-26

### العربية

#### التحسينات والميزات الجديدة
- **دعم `gh` المرتبط بمفتاح «لا»**: التعرف على الكلمات الإنجليزية التي تحتوي `gh` عند كتابتها على التخطيط العربي (`قهلاف` ← `right`، `ىهلاف` ← `night`، `ةهلاف` ← `might`، `مهلاف` ← `light`، `فاخعلاف` ← `thought`، `فاقخعلا` ← `through`) ودعم أشكال العرض العربية (`\ufefbخخن` ← `book`).
- **دعم الهمزات مع `Shift` و`CapsLock`**: استعادة الكلمات العربية المبدوءة بالهمزة على التخطيط الإنجليزي (`Hkh` ← `أنا`، `Ygn` ← `إلى`، `hgNk` ← `الآن`، `Hpl]` ← `أحمد`، `SGHL` ← `سلام`) دون المساس بالكلمات العربية الصحيحة على Windows.
- **حماية علامات الترقيم والرموز البرمجية**: منع إفساد الكلمات الإنجليزية المتبوعة بفاصلة أو نقطة أو فاصلة منقوطة أو فاصلة عليا أو `/` (مثل `of.` و`p/` و`a,`) وأسماء الملفات والروابط (`main.py` و`google.com`) مع استعادة الترقيم العربي إلى الإنجليزي للكلمات الواضحة.
- **حزم وتجربة موحّدة على Linux وWindows**:
  - حزمة إصدار Linux جاهزة للتنزيل (`Multilang-Linux-x64.tar.gz`) مع سكربت تثبيت متكامل (`scripts/install-linux.sh`) يدعم Arch/EndeavourOS وDebian/Ubuntu وFedora ويُنشئ أيقونة القائمة والتشغيل التلقائي.
  - إضافة ملفات التحقق `SHA256SUMS.txt` لكل إصدار رسمي على GitHub Releases.
  - إضافة خيارات سطر الأوامر: `--start-in-background` و`--debug` و`--self-test` و`--version` مع منع تشغيل أكثر من نسخة.

#### إصلاحات KDE Plasma Wayland (Fcitx5)
- **لغتان فقط دون لغة ثالثة**: تحويل إضافة Fcitx5 إلى وحدة خلفية (`Category=Module`) تعمل فوق `keyboard-us` و`keyboard-ara` فقط دون إضافة لغة ثالثة («مزدوج / Multilang»).
- **إصلاح التبديل بالاختصارات**: حل تعارض `Alt+Shift` بين KWin وFcitx5 وإلغاء خطف ضغطات `Shift` المنفردة.
- **مؤشر الكتابة وتنسيق الأحرف**: حساب موضع المؤشر في `preedit` بالبايت بدلاً من عدد الأحرف، والحفاظ على `Shift` و`CapsLock` وعلامة الاستفهام `؟`.
- **استقرار الأداء والإدخال**: استبعاد الأرقام من `preedit`، منع تكرار النص عند النقر بالفأرة مع تطبيقات `ClientUnfocusCommit`، تسريع اتصال Unix socket، وإصلاح تحليل مخرجات GVariant (`true`/`false`) في `gdbus`.

#### إصلاحات Windows
- نقل تبديل التخطيط في مراقب Rust وPython إلى خيط خلفي غير معيق لتجنّب مهلة Windows `LowLevelHooksTimeout`.
- دعم تخطيطات العربية والإنجليزية الإقليمية عبر `PRIMARYLANGID` و`GetKeyboardLayoutList` و`GetGUIThreadInfo`.
- احتساب الحذف بالحرف الكامل لـ«لا» (مسافتَي رجوع)، وإعادة تعيين المخزن المؤقت عند النقر بالفأرة أو تغيير النافذة أو بعد مهلة خمول، وحماية الفهرسة والحدود الآمنة في FFI.
- إصلاح تغليف PyInstaller (`pynput.mouse._win32` ومعالجة غياب `stdout`/`stderr` في وضع النافذة دون طرفية).

---

### English

#### New Features & Improvements
- **Branching candidate scoring for Arabic `لا` (`b` / `gh`) & presentation forms**: Recovers English `gh` words typed on an Arabic layout (`قهلاف` → `right`, `ىهلاف` → `night`, `فاخعلاف` → `thought`, `\ufefbخخن` → `book`) alongside boosted Arabic frequency recall.
- **Shifted hamza & CapsLock recovery**: Accurately restores Arabic words starting with `H`/`Y`/`N`/`G`/`T`/`B` (`Hkh` → `أنا`, `Ygn` → `إلى`, `SGHL` → `سلام`) while preventing false rewrites of real Arabic words under Windows Shift hooks.
- **Punctuation & code-token safety**: Protects English tokens ending in punctuation (`of.`, `p/`, `a,`, `uk;`) and code/URLs (`main.py`, `google.com`) with zero false positives across top 5k EN/AR benchmarks.
- **Community Release Packaging for Linux & Windows**:
  - New Linux release archive (`Multilang-Linux-x64.tar.gz`) with installer (`scripts/install-linux.sh`) and uninstaller (`scripts/uninstall-linux.sh`) supporting Arch/EndeavourOS, Debian/Ubuntu, and Fedora.
  - Automated `SHA256SUMS.txt` checksums alongside `Multilang-Setup.exe`, `Multilang-Windows-x64.zip`, and `Multilang-Linux-x64.tar.gz`.
  - Added single-instance protection and CLI flags: `--start-in-background`, `--debug`, `--self-test`, `--version`.

#### KDE Plasma Wayland (Fcitx5) Fixes
- Converted Fcitx5 integration into a silent background `Module` operating strictly across two layouts (`keyboard-us` and `keyboard-ara`) with no third input method.
- Resolved `Alt+Shift` conflicts and single-Shift misfires; enabled reliable instant switching via Fcitx5.
- Fixed UTF-8 byte-offset preedit cursor placement, preserved `Shift`/`CapsLock`/`؟`, excluded digits from preedit, prevented click-induced duplicate text with `ClientUnfocusCommit`, and fixed GVariant lowercase boolean parsing (`true`/`false`) for `gdbus`.

#### Windows Fixes
- Moved layout switching off low-level keyboard hook callbacks onto a background worker thread (`MultilangLayoutSwitcher` / Python worker).
- Preserved regional Arabic and English layouts via `PRIMARYLANGID`, `GetKeyboardLayoutList`, and `GetGUIThreadInfo`.
- Fixed 2-backspace counting for `لا`, mouse-click/window-change buffer clearing, bounds-checked VK state lookups, FFI `catch_unwind`, and console-less PyInstaller CLI handling.

---

## [1.0.0] - 2026-11-09

- Initial public release with Arabic/English word correction, native Rust Windows hook (`Multilang-Setup.exe`, `Multilang-Windows-x64.zip`), X11 support, and initial KDE Wayland Fcitx5 add-on.
