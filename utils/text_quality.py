"""Пошук роботизованих фраз у готовому пості (перед модерацією)."""

from __future__ import annotations

import re

from config import BANNED_AI_PHRASES

_SPACE_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _SPACE_RE.sub(" ", (text or "").lower()).replace("’", "'").strip()


def find_banned_phrases(text: str) -> list[str]:
    """Повертає заборонені фрази, що потрапили в текст."""
    normalized = _norm(text)
    if not normalized:
        return []
    return [p for p in BANNED_AI_PHRASES if _norm(p) in normalized]
