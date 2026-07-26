"""Recover from models that emit tool calls as plain text.

Llama 3.3 on Groq intermittently writes a call into the *content* instead of
the structured `tool_calls` field:

    Okay, booking that in. <function=send_confirmation>{"job_id": "job_x"}</function>

Two things go wrong if we ignore that. The caller hears the raw markup read
aloud by TTS, and the tool never actually runs. So we do both halves here:
strip it from anything spoken, and promote it to a real tool call.

This is the concrete mitigation for the Groq tool-calling risk in plan §17 —
and it lives in the brain, not in a channel, because both channels need it.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

_OPEN = "<function"

#: `<function=name>{...}</function>`, tolerating a missing '>' or closing tag.
_INLINE_CALL = re.compile(
    r"<function=(?P<name>[\w.\-]+)\s*>?\s*(?P<args>\{.*?\})\s*(?:</function\s*>)?",
    re.DOTALL,
)


def extract_inline_tool_calls(content: str) -> tuple[str, list[dict[str, Any]]]:
    """Split leaked tool calls out of `content`.

    Returns the text with the markup removed, plus any calls recovered from it.
    Malformed JSON is dropped rather than guessed at — a half-parsed booking is
    worse than none.
    """
    if _OPEN not in content:
        return content, []

    calls: list[dict[str, Any]] = []

    def _take(match: re.Match[str]) -> str:
        try:
            args = json.loads(match.group("args"))
        except json.JSONDecodeError:
            return ""
        if isinstance(args, dict):
            calls.append(
                {
                    "name": match.group("name"),
                    "args": args,
                    "id": f"inline_{len(calls)}",
                    "type": "tool_call",
                }
            )
        return ""

    cleaned = _INLINE_CALL.sub(_take, content)
    # Drop any unterminated remnant, e.g. a truncated '<function=book_job{"a"'.
    cleaned = cleaned.split(_OPEN)[0] if _OPEN in cleaned else cleaned
    return cleaned.strip(), calls


class InlineToolCallFilter:
    """Streaming counterpart — withholds leaked markup mid-stream.

    Tokens are spoken the moment they're produced, so the decision to suppress
    has to be made without seeing the rest of the reply. We hold back only the
    few characters that could still turn into a `<function` marker, which costs
    nothing perceptible and guarantees the markup is never voiced.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._suppressing = False

    def feed(self, text: str) -> str:
        """Consume a streamed chunk; return the part that's safe to say now."""
        self._pending += text
        out: list[str] = []

        while self._pending:
            if self._suppressing:
                end = self._pending.find("</function>")
                if end == -1:
                    # Still inside the markup — hold everything back.
                    if len(self._pending) > 4096:  # runaway; give up on it
                        self._pending = ""
                    return "".join(out)
                self._pending = self._pending[end + len("</function>") :]
                self._suppressing = False
                continue

            start = self._pending.find(_OPEN)
            if start != -1:
                out.append(self._pending[:start])
                self._pending = self._pending[start:]
                self._suppressing = True
                continue

            # No marker yet — emit everything except a possible partial one.
            keep = _partial_marker_length(self._pending)
            if keep:
                out.append(self._pending[:-keep])
                self._pending = self._pending[-keep:]
            else:
                out.append(self._pending)
                self._pending = ""
            break

        return "".join(out)

    def flush(self) -> str:
        """End of stream: emit what's left, unless it was markup."""
        remainder = "" if self._suppressing else self._pending
        self._pending = ""
        self._suppressing = False
        return remainder


def _partial_marker_length(text: str) -> int:
    """How many trailing chars could still grow into `<function`."""
    for size in range(min(len(text), len(_OPEN) - 1), 0, -1):
        if _OPEN.startswith(text[-size:]):
            return size
    return 0


_SENTENCE_END = re.compile(r"[.!?…]['\")\]]*(\s|$)")


def _normalise(text: str) -> str:
    """Lowercase, letters/digits/spaces only — punctuation-insensitive compare."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def _split_first_sentence(text: str) -> tuple[str, str]:
    match = _SENTENCE_END.search(text)
    if not match:
        return text, ""
    cut = match.end()
    return text[:cut], text[cut:]


class RepeatSuppressor:
    """Drops a re-spoken acknowledgement at the start of a reply segment.

    Observed live on Llama 3.3: having said "Let me check what we've got
    available for you" and called a tool, it opens the follow-up with "Let me
    check what we've got…" — a *truncated* restatement, not a verbatim one, so
    an exact-match check misses it. In text it reads as a stutter; spoken aloud
    it's jarring, and prompt instructions don't reliably prevent it.

    Arm this after a tool hop with whatever was just said. The first sentence of
    the new segment is dropped when it's clearly the same utterance — either one
    is a prefix of the other, or they're near-identical.

    Two safeguards keep it from eating real content:

    * anything that can't be a restatement is released immediately, so genuinely
      new wording is never delayed;
    * short openers ("Okay.") are left alone, since they carry no information to
      duplicate and are common as genuine sentence starters.
    """

    #: Don't hold more than this many characters waiting for a sentence to end.
    MAX_HOLD = 160
    #: Below this, an opener is too short to confidently call a repeat.
    MIN_MATCH = 12
    #: Similarity above which two near-identical sentences count as the same.
    SIMILARITY = 0.8

    def __init__(self) -> None:
        self._target = ""
        self._buffer = ""
        self._active = False

    def arm(self, previously_spoken: str) -> None:
        self._target = _normalise(previously_spoken)
        self._buffer = ""
        self._active = len(self._target) >= self.MIN_MATCH

    def feed(self, text: str) -> str:
        """Consume a chunk; return what's safe to say now."""
        if not self._active:
            return text
        if not text:
            return ""

        self._buffer += text
        candidate = _normalise(self._buffer)
        if not candidate:
            return ""

        # Fast path: once it can't be a restatement either way, stop holding.
        if not (self._target.startswith(candidate) or candidate.startswith(self._target)):
            return self._release()

        if not _SENTENCE_END.search(self._buffer):
            # Held as long as is reasonable with no sentence boundary in sight.
            # We can't tell where the restatement ends, so say all of it rather
            # than risk swallowing real content.
            return "" if len(self._buffer) < self.MAX_HOLD else self._release()

        return self._decide()

    def flush(self) -> str:
        # At flush the segment is complete, so the buffer *is* the sentence
        # even without a terminator.
        return self._decide() if self._active else self._release()

    def _decide(self) -> str:
        self._active = False
        buffer, self._buffer = self._buffer, ""
        head, tail = _split_first_sentence(buffer)
        return tail.lstrip() if self._is_repeat(_normalise(head)) else buffer

    def _is_repeat(self, head: str) -> bool:
        """Is `head` the same utterance as what was just said?

        No length floor here — `arm()` already refuses to engage on a target too
        short to duplicate meaningfully, and the prefix/similarity tests are
        what actually keep unrelated openers safe.
        """
        if not head:
            return False
        if head.startswith(self._target) or self._target.startswith(head):
            return True
        return SequenceMatcher(None, head, self._target).ratio() >= self.SIMILARITY

    def _release(self) -> str:
        out = self._buffer
        self._buffer = ""
        self._active = False
        return out
