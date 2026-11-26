#include "logic.hpp"

#include <fcitx-utils/handlertable.h>
#include <fcitx-utils/key.h>
#include <fcitx-utils/keysym.h>
#include <fcitx/addonfactory.h>
#include <fcitx/addoninstance.h>
#include <fcitx/addonmanager.h>
#include <fcitx/event.h>
#include <fcitx/inputcontext.h>
#include <fcitx/inputcontextmanager.h>
#include <fcitx/inputcontextproperty.h>
#include <fcitx/inputmethodgroup.h>
#include <fcitx/inputmethodmanager.h>
#include <fcitx/inputpanel.h>
#include <fcitx/instance.h>
#include <fcitx/text.h>

#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>
#include <vector>

namespace {

std::string socketPath() {
    const char *runtime = std::getenv("XDG_RUNTIME_DIR");
    return runtime && runtime[0] == '/'
               ? std::string(runtime) + "/multilang/correction.sock"
               : "/tmp/multilang-" + std::to_string(getuid()) + "/correction.sock";
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

class ServiceClient {
public:
    ~ServiceClient() { disconnect(); }

    bool serviceReady() {
        const auto now = std::chrono::steady_clock::now();
        if (cachedReady_ && now - lastCheck_ < std::chrono::milliseconds(1500)) {
            return true;
        }
        if (!cachedReady_ && hasChecked_ && now - lastCheck_ < std::chrono::milliseconds(500)) {
            return false;
        }
        hasChecked_ = true;
        lastCheck_ = now;
        const std::string path = socketPath();
        if (::access(path.c_str(), F_OK) != 0) {
            disconnect();
            cachedReady_ = false;
            return false;
        }
        std::string reply;
        cachedReady_ = request("PING", reply) && reply == "READY";
        return cachedReady_;
    }

    bool request(std::string_view payload, std::string &reply) {
        if (payload.empty() || payload.find('\n') != std::string_view::npos) {
            return false;
        }
        for (int attempt = 0; attempt < 2; ++attempt) {
            if (!ensureConnected()) {
                return false;
            }
            if (sendAndReadLine(payload, reply)) {
                return true;
            }
            disconnect();
        }
        cachedReady_ = false;
        return false;
    }

    void markDown() {
        cachedReady_ = false;
        hasChecked_ = true;
        lastCheck_ = std::chrono::steady_clock::now();
        disconnect();
    }

private:
    bool ensureConnected() {
        if (fd_ >= 0) {
            return true;
        }
        const std::string path = socketPath();
        sockaddr_un address{};
        if (path.size() >= sizeof(address.sun_path)) {
            return false;
        }
        int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (fd < 0) {
            return false;
        }
        timeval timeout{0, 150000};
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        address.sun_family = AF_UNIX;
        std::memcpy(address.sun_path, path.c_str(), path.size() + 1);
        if (connect(fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0) {
            close(fd);
            return false;
        }
        fd_ = fd;
        readBuffer_.clear();
        return true;
    }

    bool sendAndReadLine(std::string_view payload, std::string &reply) {
        std::string wire;
        wire.reserve(payload.size() + 1);
        wire.append(payload);
        wire.push_back('\n');
        if (!writeAll(fd_, wire.data(), wire.size())) {
            return false;
        }

        reply.clear();
        while (true) {
            const size_t newlinePos = readBuffer_.find('\n');
            if (newlinePos != std::string::npos) {
                reply = readBuffer_.substr(0, newlinePos);
                readBuffer_.erase(0, newlinePos + 1);
                if (!reply.empty() && reply.back() == '\r') {
                    return false;
                }
                return reply.size() <= multilang::kMaximumReplyBytes;
            }
            if (readBuffer_.size() > multilang::kMaximumReplyBytes) {
                return false;
            }
            char chunk[512];
            ssize_t count = recv(fd_, chunk, sizeof(chunk), 0);
            if (count < 0 && errno == EINTR) {
                continue;
            }
            if (count <= 0) {
                return false;
            }
            readBuffer_.append(chunk, static_cast<size_t>(count));
        }
    }

    void disconnect() {
        if (fd_ >= 0) {
            close(fd_);
            fd_ = -1;
        }
        readBuffer_.clear();
    }

    int fd_ = -1;
    std::string readBuffer_;
    bool cachedReady_ = false;
    bool hasChecked_ = false;
    std::chrono::steady_clock::time_point lastCheck_{};
};

class MultilangState : public fcitx::InputContextProperty {
public:
    multilang::Composition comp;
};

uint32_t keyIdentity(const fcitx::KeyEvent &event) {
    const uint32_t raw = static_cast<uint32_t>(event.rawKey().code());
    if (raw != 0) {
        return raw;
    }
    const uint32_t orig = static_cast<uint32_t>(event.origKey().code());
    return orig ? orig : static_cast<uint32_t>(event.key().sym());
}

void rememberConsumed(multilang::Composition &state, const fcitx::KeyEvent &event) {
    state.consumedKeys.insert(keyIdentity(event));
}

bool consumeRelease(multilang::Composition &state, fcitx::KeyEvent &event) {
    auto found = state.consumedKeys.find(keyIdentity(event));
    if (found == state.consumedKeys.end()) {
        return false;
    }
    state.consumedKeys.erase(found);
    event.filterAndAccept();
    return true;
}

void updatePreedit(fcitx::InputContext *context, const multilang::Composition &state) {
    if (state.displayText.empty()) {
        context->inputPanel().reset();
    } else {
        fcitx::Text preedit(state.displayText);
        // Fcitx5 Text::setCursor expects a byte offset, not a character count.
        preedit.setCursor(state.cursorByteOffset());
        context->inputPanel().setClientPreedit(preedit);
    }
    context->updatePreedit();
}

void clearComposition(fcitx::InputContext *context, multilang::Composition &state) {
    state.clear();
    updatePreedit(context, state);
}

void commitPending(fcitx::InputContext *context, multilang::Composition &state) {
    if (!state.displayText.empty()) {
        context->commitString(state.displayText);
    }
    clearComposition(context, state);
}

bool hasSensitiveCapability(const fcitx::InputContext *context) {
    return context->capabilityFlags().testAny(fcitx::CapabilityFlags{
        fcitx::CapabilityFlag::Password, fcitx::CapabilityFlag::Sensitive});
}

bool isIgnoredNonCharKey(fcitx::KeySym sym) {
    return sym == FcitxKey_ISO_Level3_Shift ||
           sym == FcitxKey_ISO_Level5_Shift ||
           sym == FcitxKey_ISO_Next_Group ||
           sym == FcitxKey_ISO_Prev_Group ||
           sym == FcitxKey_Caps_Lock ||
           sym == FcitxKey_Num_Lock ||
           sym == FcitxKey_Scroll_Lock;
}

class MultilangModule : public fcitx::AddonInstance {
public:
    explicit MultilangModule(fcitx::Instance *instance) : instance_(instance) {
        if (!instance_) {
            return;
        }
        instance_->inputContextManager().registerProperty("multilangState", &factory_);
        connections_.push_back(instance_->watchEvent(
            fcitx::EventType::InputContextKeyEvent,
            fcitx::EventWatcherPhase::PreInputMethod,
            [this](fcitx::Event &event) {
                onKeyEvent(static_cast<fcitx::KeyEvent &>(event));
            }));
        connections_.push_back(instance_->watchEvent(
            fcitx::EventType::InputContextReset,
            fcitx::EventWatcherPhase::PreInputMethod,
            [this](fcitx::Event &event) {
                onReset(static_cast<fcitx::ResetEvent &>(event));
            }));
        connections_.push_back(instance_->watchEvent(
            fcitx::EventType::InputContextFocusOut,
            fcitx::EventWatcherPhase::PreInputMethod,
            [this](fcitx::Event &event) {
                onFocusOut(static_cast<fcitx::FocusOutEvent &>(event));
            }));
        connections_.push_back(instance_->watchEvent(
            fcitx::EventType::InputContextSwitchInputMethod,
            fcitx::EventWatcherPhase::PreInputMethod,
            [this](fcitx::Event &event) {
                onSwitchInputMethod(
                    static_cast<fcitx::InputContextSwitchInputMethodEvent &>(event));
            }));
    }

private:
    multilang::Composition &contextState(fcitx::InputContext *context) {
        return context->propertyFor(&factory_)->comp;
    }

    void onReset(fcitx::ResetEvent &event) {
        auto *context = event.inputContext();
        auto &state = contextState(context);
        if (!state.capturing && state.displayText.empty()) {
            return;
        }
        if (context->capabilityFlags().test(fcitx::CapabilityFlag::ClientUnfocusCommit)) {
            clearComposition(context, state);
        } else {
            commitPending(context, state);
        }
    }

    void onFocusOut(fcitx::FocusOutEvent &event) {
        auto *context = event.inputContext();
        auto &state = contextState(context);
        if (!state.capturing && state.displayText.empty()) {
            return;
        }
        if (context->capabilityFlags().test(fcitx::CapabilityFlag::ClientUnfocusCommit)) {
            clearComposition(context, state);
        } else {
            commitPending(context, state);
        }
    }

    void onSwitchInputMethod(fcitx::InputContextSwitchInputMethodEvent &event) {
        auto *context = event.inputContext();
        auto &state = contextState(context);
        if (state.capturing || !state.displayText.empty()) {
            commitPending(context, state);
        }
    }

    void onKeyEvent(fcitx::KeyEvent &event) {
        if (event.filtered()) {
            return;
        }
        auto *context = event.inputContext();
        if (!context || !instance_) {
            return;
        }
        auto &state = contextState(context);

        if (event.isRelease()) {
            consumeRelease(state, event);
            return;
        }

        const std::string activeIm = instance_->inputMethod(context);
        const std::string_view activeLang = multilang::keyboardImLanguage(activeIm);
        if (activeLang.empty()) {
            if (state.capturing) {
                commitPending(context, state);
            }
            return;
        }

        if (hasSensitiveCapability(context) ||
            !context->capabilityFlags().test(fcitx::CapabilityFlag::Preedit)) {
            if (state.capturing) {
                commitPending(context, state);
            }
            return;
        }

        // Read the unnormalized active-layout key (preserving Shift, CapsLock, AltGr)
        // while letting modifiers and non-word keys flow straight to Fcitx5 untouched.
        const fcitx::Key srcKey = event.rawKey().isValid() ? event.rawKey() : event.origKey();
        if (srcKey.isModifier() || event.key().isModifier() ||
            isIgnoredNonCharKey(srcKey.sym()) ||
            isIgnoredNonCharKey(event.key().sym())) {
            return;
        }

        if (srcKey.states().testAny(fcitx::KeyStates{
                fcitx::KeyState::Ctrl, fcitx::KeyState::Alt,
                fcitx::KeyState::Super, fcitx::KeyState::Super2,
                fcitx::KeyState::Hyper, fcitx::KeyState::Hyper2,
                fcitx::KeyState::Meta})) {
            if (state.capturing) {
                commitPending(context, state);
            }
            return;
        }

        if (srcKey.sym() == FcitxKey_BackSpace || event.key().check(FcitxKey_BackSpace)) {
            if (!state.capturing || state.displayText.empty()) {
                state.capturing = false;
                return;
            }
            state.backspace();
            updatePreedit(context, state);
            rememberConsumed(state, event);
            event.filterAndAccept();
            return;
        }

        const char32_t observed = fcitx::Key::keySymToUnicode(srcKey.sym());
        if (observed < 0x20 || observed == 0x7F) {
            if (state.capturing) {
                commitPending(context, state);
            }
            return;
        }

        // Never trap digits inside preedit; flush any active word and let digits pass through.
        if (multilang::isDigit(observed)) {
            if (state.capturing) {
                commitPending(context, state);
            }
            return;
        }

        const bool shifted = srcKey.states().test(fcitx::KeyState::Shift);
        const bool altGr = srcKey.states().test(fcitx::KeyState::Mod5);
        const uint32_t physicalCode = static_cast<uint32_t>(
            srcKey.code() ? srcKey.code() : event.origKey().code());
        const char shiftedUpper =
            activeLang == "ar"
                ? multilang::shiftedPhysicalUsLetter(
                      observed, physicalCode, shifted, altGr, state.displayText.empty())
                : 0;

        const bool wordChar = multilang::isWordCharacter(observed);
        const bool joiner = multilang::isWordJoiner(observed) && !state.displayText.empty();
        const bool starter = shiftedUpper != 0;

        if (!state.capturing && !wordChar && !starter) {
            return;
        }

        if (!state.capturing) {
            if (!client_.serviceReady()) {
                return;
            }
            state.capturing = true;
        }

        if (wordChar || joiner || starter) {
            const std::string displayChar = multilang::ucs4ToUtf8(observed);
            const std::string detectChar =
                starter ? std::string(1, shiftedUpper) : displayChar;
            if (displayChar.empty() || detectChar.empty() ||
                state.displayText.size() + displayChar.size() > multilang::kMaximumWordBytes ||
                state.detectText.size() + detectChar.size() > multilang::kMaximumWordBytes) {
                commitPending(context, state);
                return;
            }
            state.append(displayChar, detectChar);
            updatePreedit(context, state);
            rememberConsumed(state, event);
            event.filterAndAccept();
            return;
        }

        if (multilang::isBoundaryCharacter(observed)) {
            const std::string boundary = multilang::ucs4ToUtf8(observed);
            finishWord(context, state, boundary, activeIm);
            rememberConsumed(state, event);
            event.filterAndAccept();
            return;
        }

        commitPending(context, state);
    }

    void finishWord(fcitx::InputContext *context, multilang::Composition &state,
                    const std::string &boundary, const std::string &activeIm) {
        std::string replacement;
        std::string targetLanguage;
        bool fixed = false;

        if (multilang::utf8CharCount(state.detectText) >= 2) {
            std::string response;
            if (client_.request("WORD\t" + state.detectText, response)) {
                if (auto parsed = multilang::parseFixResponse(response)) {
                    replacement = std::move(parsed->replacement);
                    targetLanguage = std::move(parsed->language);
                    fixed = true;
                }
            } else {
                client_.markDown();
            }
        }

        const std::string committed = (fixed ? replacement : state.displayText) + boundary;
        clearComposition(context, state);
        context->commitString(committed);

        if (fixed && instance_) {
            std::vector<std::string> ims;
            const auto &groupItems =
                instance_->inputMethodManager().currentGroup().inputMethodList();
            ims.reserve(groupItems.size());
            for (const auto &item : groupItems) {
                ims.push_back(item.name());
            }
            const std::string targetIm =
                multilang::findTargetKeyboardIm(ims, targetLanguage);
            if (!targetIm.empty() && targetIm != activeIm) {
                instance_->setCurrentInputMethod(context, targetIm, false);
            }
        }
    }

    fcitx::Instance *instance_;
    fcitx::FactoryFor<MultilangState> factory_{
        [](fcitx::InputContext &) { return new MultilangState; }};
    std::vector<std::unique_ptr<fcitx::HandlerTableEntry<fcitx::EventHandler>>> connections_;
    ServiceClient client_;
};

class MultilangModuleFactory : public fcitx::AddonFactory {
public:
    fcitx::AddonInstance *create(fcitx::AddonManager *manager) override {
        return new MultilangModule(manager ? manager->instance() : nullptr);
    }
};

} // namespace

FCITX_ADDON_FACTORY(MultilangModuleFactory);
