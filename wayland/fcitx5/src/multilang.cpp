#include <fcitx-utils/key.h>
#include <fcitx-utils/keysym.h>
#include <fcitx-utils/utf8.h>
#include <fcitx/addonfactory.h>
#include <fcitx/inputcontext.h>
#include <fcitx/inputpanel.h>
#include <fcitx/inputmethodengine.h>
#include <fcitx/text.h>

#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {

constexpr size_t kMaximumWordBytes = 2048;
constexpr size_t kMaximumReplyBytes = 4096;

struct Composition {
    std::string text;
    std::string language;
    std::vector<size_t> characterLengths;
    bool capturing = false;
    std::unordered_set<uint32_t> consumedKeys;
};

std::string socketPath() {
    const char *runtime = std::getenv("XDG_RUNTIME_DIR");
    std::string path = runtime && runtime[0] == '/'
                           ? std::string(runtime) + "/multilang/correction.sock"
                           : "/tmp/multilang-" + std::to_string(getuid()) + "/correction.sock";
    return path;
}

bool writeAll(int fd, const char *data, size_t length) {
    size_t offset = 0;
    while (offset < length) {
        ssize_t written = send(fd, data + offset, length - offset, MSG_NOSIGNAL);
        if (written < 0 && errno == EINTR) {
            continue;
        }
        if (written <= 0) {
            return false;
        }
        offset += static_cast<size_t>(written);
    }
    return true;
}

bool requestService(const std::string &request, std::string &reply) {
    const std::string path = socketPath();
    sockaddr_un address{};
    if (path.size() >= sizeof(address.sun_path)) {
        return false;
    }

    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        return false;
    }

    timeval timeout{0, 200000};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));

    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, path.c_str(), path.size() + 1);
    if (connect(fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0) {
        close(fd);
        return false;
    }

    const std::string wire = request + "\n";
    if (!writeAll(fd, wire.data(), wire.size())) {
        close(fd);
        return false;
    }

    reply.clear();
    char byte = 0;
    while (reply.size() < kMaximumReplyBytes) {
        ssize_t count = recv(fd, &byte, 1, 0);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            close(fd);
            return false;
        }
        if (byte == '\n') {
            close(fd);
            return true;
        }
        if (byte == '\r') {
            close(fd);
            return false;
        }
        reply.push_back(byte);
    }
    close(fd);
    return false;
}

bool serviceReady() {
    std::string reply;
    return requestService("PING", reply) && reply == "READY";
}

bool isArabicLetter(char32_t codepoint) {
    return (codepoint >= 0x0620 && codepoint <= 0x064A) ||
           (codepoint >= 0x066E && codepoint <= 0x06D3) ||
           (codepoint >= 0x0750 && codepoint <= 0x077F) ||
           (codepoint >= 0x08A0 && codepoint <= 0x08FF);
}

bool isArabicMark(char32_t codepoint) {
    return (codepoint >= 0x064B && codepoint <= 0x065F) || codepoint == 0x0670;
}

bool isArabicLayoutSymbol(char32_t codepoint) {
    // These unshifted US keysyms produce Arabic letters in Arabic PC layouts.
    return codepoint == U'`' || codepoint == U'[' || codepoint == U']' ||
           codepoint == U';' || codepoint == U'\'' || codepoint == U',' ||
           codepoint == U'.' || codepoint == U'/';
}

bool isWordCharacter(char32_t codepoint) {
    return (codepoint >= U'a' && codepoint <= U'z') ||
           (codepoint >= U'A' && codepoint <= U'Z') ||
           (codepoint >= U'0' && codepoint <= U'9') ||
           (codepoint >= 0x0660 && codepoint <= 0x0669) ||
           isArabicLetter(codepoint) || isArabicMark(codepoint) ||
           isArabicLayoutSymbol(codepoint);
}

bool isWordJoiner(char32_t codepoint) {
    return codepoint == U'\'' || codepoint == 0x2019 || codepoint == U'-';
}

bool isBoundaryCharacter(char32_t codepoint) {
    return codepoint == U' ' || (codepoint >= 0x21 && codepoint <= 0x2F) ||
           (codepoint >= 0x3A && codepoint <= 0x40) ||
           (codepoint >= 0x5B && codepoint <= 0x60) ||
           (codepoint >= 0x7B && codepoint <= 0x7E) || codepoint == 0x00A0 ||
           codepoint == 0x060C || codepoint == 0x061B || codepoint == 0x061F;
}

char32_t firstCodepoint(const std::string &text) {
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

char32_t usKeySymbol(uint32_t code, bool shifted) {
    if (code >= 2 && code <= 11) {
        static constexpr char normal[] = "1234567890";
        static constexpr char shift[] = "!@#$%^&*()";
        return shifted ? shift[code - 2] : normal[code - 2];
    }
    if (code >= 16 && code <= 25) {
        static constexpr char row[] = "qwertyuiop";
        const auto value = row[code - 16];
        return shifted ? value - ('a' - 'A') : value;
    }
    if (code >= 30 && code <= 38) {
        static constexpr char row[] = "asdfghjkl";
        const auto value = row[code - 30];
        return shifted ? value - ('a' - 'A') : value;
    }
    if (code >= 44 && code <= 50) {
        static constexpr char row[] = "zxcvbnm";
        const auto value = row[code - 44];
        return shifted ? value - ('a' - 'A') : value;
    }
    switch (code) {
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

std::string arabicKeyText(uint32_t code, bool shifted) {
    if (shifted) {
        return {};
    }
    switch (code) {
    case 16: return fcitx::utf8::UCS4ToUTF8(U'ض');
    case 17: return fcitx::utf8::UCS4ToUTF8(U'ص');
    case 18: return fcitx::utf8::UCS4ToUTF8(U'ث');
    case 19: return fcitx::utf8::UCS4ToUTF8(U'ق');
    case 20: return fcitx::utf8::UCS4ToUTF8(U'ف');
    case 21: return fcitx::utf8::UCS4ToUTF8(U'غ');
    case 22: return fcitx::utf8::UCS4ToUTF8(U'ع');
    case 23: return fcitx::utf8::UCS4ToUTF8(U'ه');
    case 24: return fcitx::utf8::UCS4ToUTF8(U'خ');
    case 25: return fcitx::utf8::UCS4ToUTF8(U'ح');
    case 26: return fcitx::utf8::UCS4ToUTF8(U'ج');
    case 27: return fcitx::utf8::UCS4ToUTF8(U'د');
    case 30: return fcitx::utf8::UCS4ToUTF8(U'ش');
    case 31: return fcitx::utf8::UCS4ToUTF8(U'س');
    case 32: return fcitx::utf8::UCS4ToUTF8(U'ي');
    case 33: return fcitx::utf8::UCS4ToUTF8(U'ب');
    case 34: return fcitx::utf8::UCS4ToUTF8(U'ل');
    case 35: return fcitx::utf8::UCS4ToUTF8(U'ا');
    case 36: return fcitx::utf8::UCS4ToUTF8(U'ت');
    case 37: return fcitx::utf8::UCS4ToUTF8(U'ن');
    case 38: return fcitx::utf8::UCS4ToUTF8(U'م');
    case 39: return fcitx::utf8::UCS4ToUTF8(U'ك');
    case 40: return fcitx::utf8::UCS4ToUTF8(U'ط');
    case 41: return fcitx::utf8::UCS4ToUTF8(U'ذ');
    case 44: return fcitx::utf8::UCS4ToUTF8(U'ئ');
    case 45: return fcitx::utf8::UCS4ToUTF8(U'ء');
    case 46: return fcitx::utf8::UCS4ToUTF8(U'ؤ');
    case 47: return fcitx::utf8::UCS4ToUTF8(U'ر');
    case 48: return "لا";
    case 49: return fcitx::utf8::UCS4ToUTF8(U'ى');
    case 50: return fcitx::utf8::UCS4ToUTF8(U'ة');
    case 51: return fcitx::utf8::UCS4ToUTF8(U'و');
    case 52: return fcitx::utf8::UCS4ToUTF8(U'ز');
    case 53: return fcitx::utf8::UCS4ToUTF8(U'ظ');
    default: return {};
    }
}

std::string physicalKeyText(uint32_t keycode, bool shifted,
                            const std::string &language, char32_t observed) {
    if (!keycode || (language != "en" && language != "ar")) {
        return {};
    }

    const uint32_t candidates[] = {keycode, keycode >= 8 ? keycode - 8 : keycode};
    uint32_t selected = keycode;
    bool found = false;
    for (uint32_t candidate : candidates) {
        const char32_t us = usKeySymbol(candidate, shifted);
        const std::string ar = arabicKeyText(candidate, shifted);
        const char32_t arabic = firstCodepoint(ar);
        if (us == observed || arabic == observed ||
            (ar == "لا" && (observed == 0xFEFB || observed == 0xFEFC))) {
            selected = candidate;
            found = true;
            break;
        }
    }

    if (!found && keycode >= 8 && usKeySymbol(keycode - 8, shifted)) {
        selected = keycode - 8;
    }
    if (language == "en") {
        const char32_t symbol = usKeySymbol(selected, shifted);
        return symbol ? fcitx::utf8::UCS4ToUTF8(symbol) : std::string{};
    }
    return arabicKeyText(selected, shifted);
}

uint32_t keyIdentity(const fcitx::KeyEvent &event) {
    const uint32_t code = event.rawKey().code();
    return code ? code : static_cast<uint32_t>(event.key().sym());
}

void rememberConsumed(Composition &state, const fcitx::KeyEvent &event) {
    state.consumedKeys.insert(keyIdentity(event));
}

bool consumeRelease(Composition &state, fcitx::KeyEvent &event) {
    auto found = state.consumedKeys.find(keyIdentity(event));
    if (found == state.consumedKeys.end()) {
        return false;
    }
    state.consumedKeys.erase(found);
    event.filterAndAccept();
    return true;
}

void updatePreedit(fcitx::InputContext *context, const Composition &state) {
    if (state.text.empty()) {
        context->inputPanel().reset();
    } else {
        fcitx::Text preedit(state.text);
        preedit.setCursor(static_cast<int>(fcitx::utf8::length(state.text)));
        context->inputPanel().setClientPreedit(preedit);
    }
    context->updatePreedit();
}

void commitPending(fcitx::InputContext *context, Composition &state) {
    if (!state.text.empty()) {
        context->commitString(state.text);
    }
    state.text.clear();
    state.characterLengths.clear();
    state.capturing = false;
    updatePreedit(context, state);
}

void removeLastUtf8Character(std::string &text) {
    if (text.empty()) {
        return;
    }
    size_t index = text.size() - 1;
    while (index > 0 && (static_cast<unsigned char>(text[index]) & 0xC0) == 0x80) {
        --index;
    }
    text.erase(index);
}

bool hasSensitiveCapability(const fcitx::InputContext *context) {
    return context->capabilityFlags().testAny(
        fcitx::CapabilityFlags{fcitx::CapabilityFlag::Password,
                               fcitx::CapabilityFlag::Sensitive});
}

class MultilangEngine : public fcitx::InputMethodEngineV2 {
public:
    void activate(const fcitx::InputMethodEntry &entry,
                  fcitx::InputContextEvent &event) override {
        FCITX_UNUSED(entry);
        states_[event.inputContext()] = Composition{};
    }

    void deactivate(const fcitx::InputMethodEntry &entry,
                    fcitx::InputContextEvent &event) override {
        FCITX_UNUSED(entry);
        auto *context = event.inputContext();
        auto found = states_.find(context);
        if (found != states_.end()) {
            commitPending(context, found->second);
            states_.erase(found);
        }
    }

    void reset(const fcitx::InputMethodEntry &entry,
               fcitx::InputContextEvent &event) override {
        FCITX_UNUSED(entry);
        auto *context = event.inputContext();
        auto found = states_.find(context);
        if (found != states_.end()) {
            commitPending(context, found->second);
        }
    }

    void keyEvent(const fcitx::InputMethodEntry &entry,
                  fcitx::KeyEvent &event) override {
        FCITX_UNUSED(entry);
        auto *context = event.inputContext();
        auto &state = states_[context];

        if (event.isRelease()) {
            consumeRelease(state, event);
            return;
        }

        if (hasSensitiveCapability(context)) {
            if (state.capturing) {
                commitPending(context, state);
            }
            return;
        }

        const auto &key = event.key();
        if (key.isModifier() || key.sym() == FcitxKey_ISO_Level3_Shift ||
            key.sym() == FcitxKey_ISO_Level5_Shift) {
            if (key.sym() == FcitxKey_Alt_L || key.sym() == FcitxKey_Alt_R) {
                state.language.clear();
            }
            return;
        }

        if (key.states().testAny(
                fcitx::KeyStates{fcitx::KeyState::Ctrl, fcitx::KeyState::Alt,
                                 fcitx::KeyState::Super})) {
            if (state.capturing) {
                commitPending(context, state);
            }
            state.language.clear();
            return;
        }

        if (key.check(FcitxKey_BackSpace)) {
            if (!state.capturing) {
                return;
            }
            if (state.text.empty()) {
                state.capturing = false;
                state.characterLengths.clear();
                updatePreedit(context, state);
                return;
            }
            size_t removeCount = 1;
            if (!state.characterLengths.empty()) {
                removeCount = state.characterLengths.back();
                state.characterLengths.pop_back();
            }
            for (size_t index = 0; index < removeCount; ++index) {
                removeLastUtf8Character(state.text);
            }
            updatePreedit(context, state);
            rememberConsumed(state, event);
            event.filterAndAccept();
            return;
        }

        const char32_t observed = fcitx::Key::keySymToUnicode(key.sym());
        const auto originalKey = event.origKey();
        const auto rawKey = event.rawKey();
        const bool hasOriginalCode = originalKey.code() != 0;
        const uint32_t physicalCode = hasOriginalCode
                                          ? static_cast<uint32_t>(originalKey.code())
                                          : static_cast<uint32_t>(rawKey.code());
        const char32_t physicalSymbol = hasOriginalCode
                                            ? fcitx::Key::keySymToUnicode(originalKey.sym())
                                            : fcitx::Key::keySymToUnicode(rawKey.sym());
        if (!state.capturing && isWordCharacter(observed)) {
            if (!serviceReady()) {
                return;
            }
            if (state.language.empty()) {
                std::string currentLanguage;
                if (requestService("LANG", currentLanguage)) {
                    constexpr std::string_view prefix = "LANG\t";
                    if (currentLanguage.rfind(prefix, 0) == 0) {
                        currentLanguage.erase(0, prefix.size());
                    }
                    if (currentLanguage == "en") {
                        state.language = "en";
                    } else if (currentLanguage == "ar") {
                        state.language = "ar";
                    }
                }
            }
        }
        const bool shifted = key.states().testAny(
            fcitx::KeyStates{fcitx::KeyState::Shift});
        const std::string mappedText = physicalKeyText(
            physicalCode, shifted, state.language,
            physicalSymbol ? physicalSymbol : observed);
        const char32_t codepoint = mappedText.empty()
                                       ? observed
                                       : firstCodepoint(mappedText);
        if (codepoint < 0x20 || codepoint == 0x7F) {
            if (state.capturing) {
                commitPending(context, state);
            }
            return;
        }

        // Some Arabic letters sit on punctuation keys in the US layout.
        // Keep those keys in the candidate so a word such as "hgg,pm" is
        // scored as one Arabic-layout word; ordinary punctuation is still
        // committed unchanged when the confidence check rejects it.
        const bool wordCharacter = isWordCharacter(codepoint);
        const bool joiner = isWordJoiner(codepoint) && !state.text.empty();
        if (!state.capturing && !wordCharacter) {
            return;
        }

        if (state.capturing && !wordCharacter && !joiner &&
            !isBoundaryCharacter(codepoint)) {
            commitPending(context, state);
            return;
        }

        if (!state.capturing) {
            state.capturing = true;
        }

        if (wordCharacter || joiner) {
            const std::string character = mappedText.empty()
                                              ? fcitx::utf8::UCS4ToUTF8(codepoint)
                                              : mappedText;
            if (character.empty() || state.text.size() + character.size() > kMaximumWordBytes) {
                commitPending(context, state);
                return;
            }
            state.text += character;
            state.characterLengths.push_back(fcitx::utf8::length(character));
            updatePreedit(context, state);
            rememberConsumed(state, event);
            event.filterAndAccept();
            return;
        }

        const std::string boundary = mappedText.empty()
                                         ? fcitx::utf8::UCS4ToUTF8(codepoint)
                                         : mappedText;
        finishWord(context, state, boundary);
        rememberConsumed(state, event);
        event.filterAndAccept();
    }

private:
    void finishWord(fcitx::InputContext *context, Composition &state,
                    const std::string &boundary) {
        std::string response;
        std::string replacement;
        std::string language;
        bool fixed = false;

        if (!state.text.empty() && requestService("WORD\t" + state.text, response)) {
            if (response.rfind("FIX\t", 0) == 0) {
                const size_t languageEnd = response.find('\t', 4);
                const size_t switchedEnd = languageEnd == std::string::npos
                                               ? std::string::npos
                                               : response.find('\t', languageEnd + 1);
                if (languageEnd != std::string::npos && switchedEnd != std::string::npos) {
                    language = response.substr(4, languageEnd - 4);
                    const std::string switched =
                        response.substr(languageEnd + 1, switchedEnd - languageEnd - 1);
                    replacement = response.substr(switchedEnd + 1);
                    fixed = (language == "en" || language == "ar") &&
                            (switched == "0" || switched == "1" || switched == "?") &&
                            !replacement.empty();
                    if (fixed) {
                        state.language = language;
                    }
                }
            }
        }

        context->inputPanel().reset();
        context->updatePreedit();
        context->commitString((fixed ? replacement : state.text) + boundary);
        state.text.clear();
        state.characterLengths.clear();
        state.capturing = false;
    }

    std::unordered_map<fcitx::InputContext *, Composition> states_;
};

class MultilangEngineFactory : public fcitx::AddonFactory {
public:
    fcitx::AddonInstance *create(fcitx::AddonManager *manager) override {
        FCITX_UNUSED(manager);
        return new MultilangEngine;
    }
};

} // namespace

FCITX_ADDON_FACTORY(MultilangEngineFactory);
