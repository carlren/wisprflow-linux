"""
Disfluency cleanup for transcripts: filler words, stutters, false starts.

Two layers work together for the local STT backend:
  1. build_local_prompt() -- prompt steering: tells the model to omit fillers
     itself (contextual, zero added latency: same single inference call).
  2. clean_disfluencies() -- mechanical regex pass over the returned text
     (microseconds): catches filler tokens and repetitions the model left in.

The OpenRouter backend (gpt-4o-transcribe) already filters these itself, so
the cleanup is applied to the local backend only.
"""
import re

# Standalone filler utterances. \b guards keep "umbrella"/"spectrum" safe.
_FILLERS = r"um+|uh+|er+m*|ah+|hmm+|mmm+|eh+"
_FILLER_RE = re.compile(r",?\s*\b(?:%s)\b[,;.!?]*" % _FILLERS, re.IGNORECASE)
_REPEAT_RE = re.compile(r"\b(\w+)(?:\s+\1\b)+", re.IGNORECASE)
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.!?;:])")
_LEADING_PUNCT_RE = re.compile(r"^[,;.!?]+\s*")


def clean_disfluencies(text: str) -> str:
    """Remove filler words and repeated-word stutters from a transcript."""
    if not text:
        return text
    text = _FILLER_RE.sub("", text)
    prev = None
    while prev != text:  # collapse 3+ repeats: "I I I want" -> "I want"
        prev = text
        text = _REPEAT_RE.sub(r"\1", text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = _LEADING_PUNCT_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


DISFLUENCY_INSTRUCTION = (
    "Transcribe the speech accurately. Omit filler words (um, uh, er, ah), "
    "false starts, and repeated words; output only the cleaned transcript."
)


def build_local_prompt(cfg: dict) -> str:
    """Assemble the prompt sent to the local STT server."""
    parts = [DISFLUENCY_INSTRUCTION]
    vocab = cfg.get("vocab") or ""
    if isinstance(vocab, list):
        vocab = ", ".join(vocab)
    vocab = vocab.strip().strip(",")
    if vocab:
        parts.append(
            "Terms that may appear in the audio: %s. "
            "Use these exact spellings when the audio contains these terms." % vocab
        )
    user_prompt = (cfg.get("prompt") or "").strip()
    if user_prompt:
        parts.append(user_prompt)
    return " ".join(parts)
