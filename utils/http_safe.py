"""Приховування секретів у URL/повідомленнях помилок (логи, exception str)."""

from __future__ import annotations

import re

# key=, apiKey=, api_key= у query string (YouTube, NewsAPI, FRED тощо).
_SECRET_QUERY = re.compile(
    r"([?&](?:key|api[_-]?key|apikey|token|access_token)=)([^&\s\"']+)",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Замінює значення секретних query-параметрів на ***."""
    if not text:
        return text
    return _SECRET_QUERY.sub(r"\1***", text)


def safe_error_text(error: BaseException) -> str:
    """Текст помилки без API-ключів у URL."""
    return redact_secrets(str(error))
