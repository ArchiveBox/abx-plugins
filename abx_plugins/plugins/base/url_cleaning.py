"""Helpers for normalizing URLs extracted from semi-structured content."""

from __future__ import annotations

import re

# https://mathiasbynens.be/demo/url-regex
URL_REGEX = re.compile(
    r"(?=("
    r"http[s]?://"  # start matching from allowed schemes
    r"(?:[a-zA-Z]|[0-9]"  # followed by allowed alphanum characters
    r"|[-_$@.&+!*\(\),]"  #   or allowed symbols (keep hyphen first to match literal hyphen)
    r"|[^\u0000-\u007F])+"  #   or allowed unicode bytes
    r'[^\]\[<>"\s]+'  # stop parsing at these symbols
    r"))",
    re.IGNORECASE | re.UNICODE,
)

QUOTE_DELIMITERS = (
    '"',
    "`",
    "“",
    "”",
)
QUOTE_ENTITY_DELIMITERS = (
    "&quot;",
    "&#34;",
    "&#x22;",
)
APOSTROPHE_ENTITIES = ("&apos;", "&#39;", "&#x27;")
URL_ENTITY_REPLACEMENTS = (
    ("&amp;", "&"),
    ("&#38;", "&"),
    ("&#x26;", "&"),
)


def sanitize_extracted_url(url: str) -> str:
    """Normalize a URL field whose boundaries are already known; retain apostrophes."""
    cleaned = (url or "").strip()
    if not cleaned:
        return cleaned

    # Decode after matching boundaries: &#39; inside a single-quoted HTML
    # attribute is content, not the end of that attribute. Never decode %27.
    for entity in APOSTROPHE_ENTITIES:
        cleaned = re.sub(re.escape(entity), "'", cleaned, flags=re.IGNORECASE)

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


def fix_url_from_markdown(url_str: str, *, preceding_text: str | None = None) -> str:
    """Trim text/Markdown delimiters, keeping balanced punctuation inside URLs.

    Pass the six source characters before a regex match to disambiguate quoted
    fields (including CSV and HTML). Leave preceding_text=None for structured
    URL fields: their parser already removed wrappers, so apostrophes are literal.
    Bare text is inherently ambiguous; internal word apostrophes and balanced
    quote pairs survive, while unmatched trailing quotes are treated as prose.
    """
    cleaned = url_str
    # Structured inputs (JSON fields, parsed HTML attributes, CLI arguments)
    # already have boundaries. Only infer surrounding quotes for text matches.
    surrounding_quote = False
    if preceding_text is not None:
        for opening_quote, closing_quote in (
            ("'", "'"),
            ('"', '"'),
            ("`", "`"),
            ("‘", "’"),
            ("“", "”"),
            *((entity, entity) for entity in APOSTROPHE_ENTITIES),
        ):
            if preceding_text.lower().endswith(opening_quote):
                surrounding_quote = True
                if opening_quote in ("'", "‘") and not preceding_text[
                    :-1
                ].rstrip().endswith("="):
                    # CSV uses doubled quotes inside a quoted field. Only use
                    # this rule at field boundaries, never for href='...'.
                    field = []
                    index = 0
                    csv_field = opening_quote == "'" and re.search(
                        r"(?:^|[,;\t])\s*$",
                        preceding_text[:-1],
                    )
                    last_quote = cleaned.rfind(closing_quote)
                    while index < len(cleaned):
                        if cleaned[index] == closing_quote:
                            if csv_field and cleaned[index : index + 2] == "''":
                                index += 1
                            elif (
                                0 < index < last_quote
                                and cleaned[index - 1].isalnum()
                                and cleaned[index + 1].isalnum()
                            ):
                                # Standalone quoted prose also reaches here:
                                # 'https://example.com/O'Reilly's' has a clear
                                # outer pair plus two internal apostrophes.
                                pass
                            else:
                                break
                        field.append(cleaned[index])
                        index += 1
                    cleaned = "".join(field)
                else:
                    closing_index = cleaned.lower().find(closing_quote)
                    if closing_index != -1:
                        cleaned = cleaned[:closing_index]
                break

    if preceding_text is not None:
        cleaned = sanitize_extracted_url(cleaned)

    # Stop at the first unmatched closing parenthesis, or the last balanced
    # prefix for an unmatched opening parenthesis. This is linear even for long
    # URLs and shares the existing Markdown behavior with the standalone hooks.
    if "(" in cleaned or ")" in cleaned:
        balance = 0
        last_valid_end = 0
        for index, char in enumerate(cleaned, start=1):
            if char == "(":
                balance += 1
            elif char == ")":
                balance -= 1
                if balance < 0:
                    break
            if balance == 0:
                last_valid_end = index
        if last_valid_end:
            cleaned = cleaned[:last_valid_end]

    if preceding_text is not None:
        # Preserve apostrophes in userinfo, but don't absorb a closing quote
        # into a hostname (e.g. https://example.com'after).
        scheme_end = cleaned.find("://")
        if scheme_end != -1:
            authority_end = re.search(r"[/?#]", cleaned[scheme_end + 3 :])
            authority_end = (
                scheme_end + 3 + authority_end.start()
                if authority_end
                else len(cleaned)
            )
            host_start = cleaned.rfind("@", scheme_end + 3, authority_end) + 1
            host_quote = re.search(
                "['‘’]",
                cleaned[max(host_start, scheme_end + 3) : authority_end],
            )
            if host_quote:
                cleaned = cleaned[
                    : max(host_start, scheme_end + 3) + host_quote.start()
                ]

        if not surrounding_quote:
            # Match quotes inside the URL like parentheses, except contractions
            # (What's, rock'n'roll, l’été) don't open or close quoted text. Strip
            # a dangling closer plus following prose punctuation; keep ?q='word'.
            tail = cleaned.rstrip(".,;:!?\\")
            if tail.endswith(("'", "’")):
                quotes = [
                    match.group()
                    for match in re.finditer("['‘’]", tail)
                    if not (
                        0 < match.start() < len(tail) - 1
                        and tail[match.start() - 1].isalnum()
                        and tail[match.start() + 1].isalnum()
                    )
                ]
                if (tail.endswith("'") and quotes.count("'") % 2) or (
                    tail.endswith("’") and quotes.count("’") > quotes.count("‘")
                ):
                    cleaned = tail[:-1]

    return cleaned
