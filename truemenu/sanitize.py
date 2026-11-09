"""Strip instruction-like text from merchant-supplied strings before any agent sees them.

Menu text is written by merchants and copied by POS systems; an agent must be able to treat it
as data. Anything that reads like an instruction to a model is removed and flagged.
"""
import re

PATTERNS = [
    r"\bignore\b.{0,30}\b(instructions?|previous|prior|above|all)\b",
    r"\bdisregard\b",
    r"\bsystem\s*prompt\b",
    r"\byou\s+are\s+(now|an?|the)\b",
    r"\b(assistant|system|user)\s*:",
    r"\btell\s+the\s+user\b",
    r"\b(only|best)\s+safe\s+choice\b",
    r"<\s*/?\s*(script|system|instructions?)\b",
    r"https?://",
]
_RX = [re.compile(p, re.I) for p in PATTERNS]


def clean(text):
    """Return (clean_text, flags)."""
    if not text:
        return "", []
    hits = [p.pattern for p in _RX if p.search(text)]
    if not hits:
        return text.strip(), []
    # Keep sentences that contain no hit; drop the rest.
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [s for s in sentences if not any(p.search(s) for p in _RX)]
    out = " ".join(kept).strip()
    out = (out + " " if out else "") + "[instruction-like text removed]"
    return out, ["instruction_like_text"]
