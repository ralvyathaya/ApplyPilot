"""Small shared helpers for custom discovery adapters."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape

from bs4 import BeautifulSoup

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def adapter_config(search_config: dict, name: str) -> dict:
    """Read `discovery.adapter_config.<name>` defensively."""
    discovery = search_config.get("discovery") or {}
    return (discovery.get("adapter_config") or {}).get(name) or {}


def selected_queries(search_config: dict, maximum_tier: int) -> list[str]:
    """Unique query strings from searches.yaml whose tier is at most maximum_tier."""
    selected = []
    for item in search_config.get("queries") or []:
        if int(item.get("tier", 99)) > maximum_tier:
            continue
        query = str(item.get("query") or "").strip()
        if query and query not in selected:
            selected.append(query)
    return selected


def clean_html_text(value: str | None) -> str | None:
    """Convert an HTML fragment to readable plain text."""
    if not value:
        return None
    text = BeautifulSoup(unescape(str(value)), "html.parser").get_text("\n", strip=True)
    return text or None


def kebab_slug(text: str) -> str:
    """Lowercase kebab-case slug used to build search page URLs."""
    return _WORD_SPLIT_RE.sub("-", str(text).lower()).strip("-")


def parse_published_date(value: str | None) -> datetime | None:
    """Parse ISO-8601 or RFC-2822 publish dates into an aware datetime."""
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def published_within(
    value: str | None,
    days_back: int,
    *,
    now: datetime | None = None,
) -> bool:
    """True when a posting is at most days_back old (undated posts pass)."""
    if days_back <= 0:
        return True
    parsed = parse_published_date(value)
    if parsed is None:
        return True
    now = now or datetime.now(UTC)
    return (now - parsed).days <= days_back


def hours_since_published(
    value: str | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Hours elapsed since publication, or None if date is unparseable."""
    parsed = parse_published_date(value)
    if parsed is None:
        return None
    now = now or datetime.now(UTC)
    delta = now - parsed
    return max(0.0, delta.total_seconds() / 3600.0)


def published_within_days(
    value: str | None,
    days: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Check if posting is within days cutoff; undated postings pass."""
    if days <= 0:
        return True
    parsed = parse_published_date(value)
    if parsed is None:
        return True
    now = now or datetime.now(UTC)
    return (now - parsed).total_seconds() <= days * 86400
