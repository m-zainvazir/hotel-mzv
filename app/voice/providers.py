"""Which STT/TTS a tenant gets.

One place decides, exactly like `app/tools/providers.py` — tools depend on
factories, never on a vendor module, which is what keeps the brain
provider-agnostic (CLAUDE.md convention #4). Voice inherits that rule
wholesale: nothing in `app/voice/session.py` imports Deepgram or Cartesia.

**Dispatch is on the deployment setting, not the tenant's `voice.provider`.**
Those answer different questions. `VoiceSettings.provider` says which voice
*Vapi* should use on a real phone call (it's written into the assistant at
provisioning); `VOICE_TTS_PROVIDER` says which vendor this relay can
actually reach. A tenant whose phone voice is on 11labs will therefore sound
different in the tester than on the phone — an honest caveat, not a bug, and
a far better failure than refusing to test the bot at all. The tenant's
`voice_id`/`speed` are still honoured whenever the vendor matches.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.tenancy.models import TenantConfig
from app.voice.stt.base import SpeechToText
from app.voice.stt.fake import FakeSpeechToText
from app.voice.tts.base import TextToSpeech
from app.voice.tts.fake import FakeTextToSpeech

logger = logging.getLogger(__name__)

_stt_override: SpeechToText | None = None
_tts_override: TextToSpeech | None = None


class VoiceProviderError(RuntimeError):
    """A configured provider can't be built — a missing key, a missing
    optional dependency. Raised loudly rather than degraded to the fake:
    silence that looks like a working tester is the one outcome worse than a
    refused socket, because an operator would read it as the *bot* being
    broken."""


def set_voice_overrides(
    *, stt: SpeechToText | None = None, tts: TextToSpeech | None = None
) -> None:
    """Test hook, matching `app/tools/providers.py`'s override pattern."""
    global _stt_override, _tts_override
    _stt_override = stt
    _tts_override = tts


def reset_voice_overrides() -> None:
    set_voice_overrides(stt=None, tts=None)


def get_stt(tenant: TenantConfig) -> SpeechToText:
    del tenant  # per-tenant STT has no config surface yet; the seam is here
    if _stt_override is not None:
        return _stt_override

    settings = get_settings()
    if settings.voice_stt_provider == "fake":
        return FakeSpeechToText()
    if settings.voice_stt_provider == "deepgram":
        if not settings.deepgram_api_key:
            raise VoiceProviderError("VOICE_STT_PROVIDER=deepgram but DEEPGRAM_API_KEY is unset")
        try:
            from app.voice.stt.deepgram import DeepgramSpeechToText
        except ImportError as exc:  # the `voice` extra isn't installed
            raise VoiceProviderError(
                "deepgram STT needs the `voice` extra: pip install -e '.[voice]'"
            ) from exc
        return DeepgramSpeechToText()
    raise VoiceProviderError(f"unknown STT provider {settings.voice_stt_provider!r}")


def get_tts(tenant: TenantConfig) -> TextToSpeech:
    del tenant
    if _tts_override is not None:
        return _tts_override

    settings = get_settings()
    if settings.voice_tts_provider == "fake":
        return FakeTextToSpeech()
    if settings.voice_tts_provider == "cartesia":
        if not settings.cartesia_api_key:
            raise VoiceProviderError("VOICE_TTS_PROVIDER=cartesia but CARTESIA_API_KEY is unset")
        try:
            from app.voice.tts.cartesia import CartesiaTextToSpeech
        except ImportError as exc:
            raise VoiceProviderError(
                "cartesia TTS needs the `voice` extra: pip install -e '.[voice]'"
            ) from exc
        return CartesiaTextToSpeech()
    raise VoiceProviderError(f"unknown TTS provider {settings.voice_tts_provider!r}")
