"""Helpers for normalizing URLs extracted from semi-structured content."""

from __future__ import annotations

QUOTE_DELIMITERS = (
    '"',
    "'",
    "`",
    "“",
    "”",
    "‘",
    "’",
)
QUOTE_ENTITY_DELIMITERS = (
    "&quot;",
    "&#34;",
    "&#x22;",
    "&apos;",
    "&#39;",
    "&#x27;",
)
URL_ENTITY_REPLACEMENTS = (
    ("&amp;", "&"),
    ("&#38;", "&"),
    ("&#x26;", "&"),
)


def sanitize_extracted_url(url: str) -> str:
    """Trim quote-delimited garbage from an extracted URL candidate."""
    cleaned = (url or "").strip()
    if not cleaned:
        return cleaned

    lower_cleaned = cleaned.lower()
    cut_index = len(cleaned)

    for delimiter in QUOTE_DELIMITERS:
        found_index = cleaned.find(delimiter)
        if found_index != -1:
            cut_index = min(cut_index, found_index)

    for delimiter in QUOTE_ENTITY_DELIMITERS:
        found_index = lower_cleaned.find(delimiter)
        if found_index != -1:
            cut_index = min(cut_index, found_index)

    cleaned = cleaned[:cut_index].strip()
    lower_cleaned = cleaned.lower()
    for entity, replacement in URL_ENTITY_REPLACEMENTS:
        while entity in lower_cleaned:
            entity_index = lower_cleaned.find(entity)
            cleaned = (
                cleaned[:entity_index]
                + replacement
                + cleaned[entity_index + len(entity) :]
            )
            lower_cleaned = cleaned.lower()

    return cleaned
