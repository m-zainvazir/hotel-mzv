"""PCM framing helpers.

Raw `linear16` end to end, no transcode (plans/phase9.3.md D2): Deepgram
accepts it on the way in, Cartesia emits it on the way out, and any
resampling is the browser's job in its AudioWorklet. That's what keeps
`infra/Dockerfile` free of an ffmpeg/apt layer for this whole phase.

Sample rates differ per direction on purpose — 16kHz is what STT wants and
is half the bytes of 24kHz for the leg that runs continuously while someone
holds the mic button; 24kHz is Cartesia's native output and downsampling it
would cost quality for nothing.
"""

from __future__ import annotations

from collections.abc import Iterator

#: Microphone → us. Deepgram's `linear16` at 16kHz mono.
INPUT_SAMPLE_RATE = 16_000
#: Us → browser. Cartesia's `pcm_s16le` at 24kHz mono.
OUTPUT_SAMPLE_RATE = 24_000
#: One sample, 16-bit signed, one channel.
BYTES_PER_SAMPLE = 2
#: ~50 frames/second/direction (D5's arithmetic). Small enough that a frame
#: never stalls the single event loop, large enough not to drown it in
#: syscalls.
FRAME_MS = 20


def bytes_per_frame(sample_rate: int, frame_ms: int = FRAME_MS) -> int:
    return int(sample_rate * frame_ms / 1000) * BYTES_PER_SAMPLE


def duration_ms(pcm: bytes, sample_rate: int) -> float:
    """How long `pcm` takes to play. Used for the fake TTS (silence of the
    *right length*, so timing-sensitive tests are honest) and for logging."""
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    return len(pcm) / (sample_rate * BYTES_PER_SAMPLE) * 1000


def silence(ms: float, sample_rate: int) -> bytes:
    """`ms` of digital silence, frame-aligned down."""
    frames = max(0, int(sample_rate * ms / 1000))
    return b"\x00" * (frames * BYTES_PER_SAMPLE)


def iter_frames(pcm: bytes, sample_rate: int, frame_ms: int = FRAME_MS) -> Iterator[bytes]:
    """Slice `pcm` into wire-sized frames.

    A trailing partial frame is yielded as-is rather than zero-padded: the
    browser concatenates into an AudioBuffer, so padding would insert a
    click of silence between synthesized chunks.
    """
    size = bytes_per_frame(sample_rate, frame_ms)
    for start in range(0, len(pcm), size):
        yield pcm[start : start + size]
