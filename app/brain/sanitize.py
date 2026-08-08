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


#: http(s) URLs the model wrote directly into its reply text — e.g. an
#: instruction typed into the AI Prompt ("redirect them to
#: https://example.com") rather than registered as a `links` catalog entry.
#: The catalog + `offer_actions` path stays the "right" way (the model never
#: writes a URL at all, so text and button never duplicate each other, and a
#: hallucinated/injected URL in a tool result can't become clickable); this
#: is the deliberate fallback for when it wasn't used, so the URL is at
#: least a real link instead of dead text. Chat-only, same reasoning
#: `offer_actions` itself is chat-only — see app/brain/runner.py's caller.
_URL_RE = re.compile(r'https?://[^\s<>"]+')
_URL_TRAILING_PUNCT = ".,;:!?)]}'\""


def extract_urls(text: str) -> list[str]:
    """URLs found in `text`, in order, de-duplicated, trailing sentence
    punctuation stripped (so "...at https://example.com." doesn't capture
    the period as part of the link)."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_URL_TRAILING_PUNCT)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


_SENTENCE_END = re.compile(r"[.!?…]['\")\]]*(\s|$)")


def _normalise(text: str) -> str:
    """Lowercase, letters/digits/spaces only — punctuation-insensitive compare."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def _sentences(text: str) -> list[str]:
    """Split into sentences, keeping a trailing fragment that never terminated."""
    out: list[str] = []
    rest = text
    while rest:
        match = _SENTENCE_END.search(rest)
        if not match:
            out.append(rest)
            break
        out.append(rest[: match.end()])
        rest = rest[match.end() :]
    return [s for s in (part.strip() for part in out) if s]


#: Function words carry no information to duplicate, so they're ignored when
#: asking "does this sentence say anything the earlier one didn't?".
_STOPWORDS = frozenset(
    """a an and are as at be been being but by can could d did do does done for from get go
    going got had has have he her here him his i if in into is it its just let lets like ll m
    me my of ok okay on one or our out over please re right s she so some sure t that the
    their them then there they this to too up us ve want was we well were what when which
    while will with would you your about all also any back now thanks thank am""".split()
)

#: A sentence reporting that the action already happened is never a repeat of
#: the promise to do it — "I've booked that for you" scores 0.85 against "I can
#: book that for you", which is exactly the confirmation a caller must hear.
#: "we've got" is excluded because it's inventory-speak ("let me check what
#: we've got"), not a report of completion.
_REPORTS_COMPLETION = re.compile(
    r"\b(?:i|we) (?:ve|have) (?!got\b)\w+|\ball set\b|\b(?:has|have) been\b|\bis confirmed\b"
)


def _content_words(normalised: str) -> set[str]:
    return {word for word in normalised.split() if word not in _STOPWORDS and len(word) > 2}


class RepeatSuppressor:
    """Drops sentences the model has already said earlier in the same turn.

    Observed live on Llama 3.3, and still on Gemini: having said "Let me check
    what we've got available for you" and called a tool, it opens the follow-up
    with "Let me check what we've got…" — a *truncated* restatement, not a
    verbatim one, so an exact-match check misses it. In text it reads as a
    stutter; spoken aloud it's jarring, and prompt instructions don't reliably
    prevent it (scoping the "speak before acting" rule away from the instant
    presentation tools cut it from ~4/5 turns to ~1/3, and no further).

    Arm this after a tool hop with whatever was just said. Two things about the
    comparison are load-bearing, and the original version got both wrong:

    * **Targets are individual sentences, not one concatenated blob.** Arming
      with a whole multi-sentence segment meant a restatement of *one* of those
      sentences scored poorly against the concatenation of all of them and
      survived. This was the dominant live failure.
    * **Every sentence in the new segment is checked, not just the first.** The
      restatement frequently lands after an opener ("Sure. I can check with a
      bookseller…"), which the old first-sentence-only check structurally could
      not see.

    Four safeguards keep it from eating real content — it fails *safe*, and
    when unsure it speaks:

    * anything that can't be a restatement is released immediately, so
      genuinely new wording is never delayed;
    * short sentences ("Okay.") are never remembered as targets, since they
      carry no information to duplicate and are normal ways to start;
    * a sentence introducing a content word the target didn't have is kept —
      "Let me check what we've got for **Wednesday** too" is a new request, not
      an echo;
    * a sentence *reporting completion* is never dropped on similarity alone.
      "I've booked that for you" scores 0.85 against "I can book that for you",
      and it is the one sentence in the turn a caller must not miss.

    Known limitation, stated rather than papered over: a *reworded* restatement
    ("I can check with a bookseller" → "Let me check with a bookseller") is only
    caught when enough of the sentence arrives to compare — the fast path
    releases text the moment it can't be a prefix of anything, because holding
    every sentence to its boundary would cost the §13 latency budget on every
    turn to fix a fraction of one. Near-verbatim restatements, which are the
    common shape, are caught either way.
    """

    #: Don't hold more than this many characters waiting for a sentence to end.
    MAX_HOLD = 160
    #: Below this, a sentence is too short to confidently call a repeat.
    MIN_MATCH = 12
    #: Similarity above which two sentences count as the same utterance. Lower
    #: than the original 0.8 because the novel-content-word and
    #: reports-completion guards below now carry the false-positive load that
    #: the threshold alone used to.
    SIMILARITY = 0.7
    #: Cap on remembered sentences, so a long turn can't grow this unbounded.
    MAX_TARGETS = 24

    def __init__(self) -> None:
        #: Normalised sentences already spoken this turn, oldest first.
        self._targets: list[str] = []
        #: The part of the current sentence not yet released.
        self._buffer = ""
        #: The part of the current sentence already released. Non-empty means
        #: this sentence is committed — we can't unsay it, so the rest of it
        #: streams straight through and only the *next* sentence is judged.
        self._said = ""
        self._active = False

    def arm(self, previously_spoken: str) -> None:
        """Remember what was just said and start judging what comes next.

        Additive: targets accumulate across every tool hop in the turn, so a
        sentence from before the first hop still suppresses an echo of it three
        hops later.
        """
        self._remember(previously_spoken)
        self._buffer = ""
        self._said = ""
        self._active = bool(self._targets)

    def feed(self, text: str) -> str:
        """Consume a chunk; return what's safe to say now."""
        if not self._active:
            return text
        if not text:
            return ""
        self._buffer += text
        return self._drain(final=False)

    def flush(self) -> str:
        """End of segment: the buffer is a complete sentence even unterminated."""
        if not self._active:
            return ""
        out = self._drain(final=True)
        self._buffer = ""
        self._said = ""
        return out

    def _drain(self, final: bool) -> str:
        out: list[str] = []

        # Complete sentences first — each is judged on its own.
        while self._buffer:
            match = _SENTENCE_END.search(self._buffer)
            if not match:
                break
            head = self._buffer[: match.end()]
            self._buffer = self._buffer[match.end() :]
            sentence = self._said + head
            committed = bool(self._said)
            self._said = ""
            if committed or not self._is_repeat(_normalise(sentence)):
                out.append(head)
                self._remember(sentence)

        if not self._buffer:
            return "".join(out)

        # A trailing fragment with no terminator in sight.
        if self._said:
            # Already streaming this sentence — pass it through, but keep the
            # full text so it can be remembered once it ends.
            self._said += self._buffer
            out.append(self._buffer)
            self._buffer = ""
        elif final:
            sentence, self._buffer = self._buffer, ""
            if not self._is_repeat(_normalise(sentence)):
                out.append(sentence)
                self._remember(sentence)
        elif len(self._buffer) >= self.MAX_HOLD or not self._could_repeat(_normalise(self._buffer)):
            # Either it can no longer become a restatement of anything, or
            # we've held as long as is reasonable with no boundary in sight.
            # Say it rather than risk swallowing real content.
            self._said, self._buffer = self._buffer, ""
            out.append(self._said)

        return "".join(out)

    def _remember(self, text: str) -> None:
        for sentence in _sentences(text):
            normalised = _normalise(sentence)
            if len(normalised) >= self.MIN_MATCH and normalised not in self._targets:
                self._targets.append(normalised)
        del self._targets[: -self.MAX_TARGETS]

    def _could_repeat(self, candidate: str) -> bool:
        """Is this partial sentence still shadowing something already said?"""
        if not candidate:
            return True
        return any(
            target.startswith(candidate) or candidate.startswith(target) for target in self._targets
        )

    def _is_repeat(self, candidate: str) -> bool:
        if not candidate:
            return False
        return any(self._matches(candidate, target) for target in self._targets)

    def _matches(self, candidate: str, target: str) -> bool:
        # A truncation of something already said contains nothing new by
        # definition, so it needs no further guard.
        if target.startswith(candidate):
            return True
        if candidate.startswith(target):
            # An extension, though, may be carrying the new part in its tail.
            return not self._adds_anything(candidate, target)
        if SequenceMatcher(None, candidate, target).ratio() < self.SIMILARITY:
            return False
        return not self._adds_anything(candidate, target)

    def _adds_anything(self, candidate: str, target: str) -> bool:
        if _REPORTS_COMPLETION.search(candidate):
            return True
        return bool(_content_words(candidate) - _content_words(target))
