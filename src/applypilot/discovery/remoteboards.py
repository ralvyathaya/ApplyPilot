"""Discovery from public remote job boards with free APIs and feeds.

One adapter covers three independent boards:
  - Remotive: JSON API with keyword search and full descriptions included.
  - RemoteOK: public JSON API without keyword search; filtered client-side.
    The first array element is a legal notice, not a job.
  - WeWorkRemotely: RSS feeds for the design and product categories. Item
    titles use a "Company: Position" format.

Only postings open to worldwide/Indonesia candidates are kept, since the
candidate cannot legally work in region-locked countries. Each board
applies strict location rules, and a region-lock phrase scanner over the
title, location, and description catches fine-print restrictions such as
"must be based in the U.S." even when the location field is empty.
Indonesia is never treated as a lock. Each board is fetched independently;
a failing board degrades the run to "partial" instead of killing the
others.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from urllib.parse import urlencode
from xml.etree import ElementTree

import httpx

from applypilot import config
from applypilot.database import init_db, store_discovered_jobs
from applypilot.discovery._util import (
    adapter_config,
    clean_html_text,
    published_within,
    selected_queries,
)
from applypilot.discovery.models import DiscoveredJob

log = logging.getLogger(__name__)
REMOTIVE_API = "https://remotive.com/api/remote-jobs"
REMOTEOK_API = "https://remoteok.com/api"
WWR_FEEDS = (
    "https://weworkremotely.com/categories/remote-design-jobs.rss",
    "https://weworkremotely.com/categories/remote-product-jobs.rss",
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
DEFAULT_BOARDS = ("remotive", "remoteok", "weworkremotely")

# Substring tokens in a lowercased title word that mark a relevant posting.
# Matching is word-based so short tokens like "ui" never match inside
# unrelated words such as "build".
RELEVANT_TITLE_TOKENS = frozenset(
    {
        "design",
        "designer",
        "ux",
        "ui",
        "product",
        "research",
        "researcher",
        "analyst",
        "analytics",
        "marketing",
        "content",
        "junior",
        "intern",
        "internship",
        "entry",
        "associate",
        "specialist",
    }
)
RELEVANT_LOCATION_TOKENS = ("worldwide", "anywhere", "indonesia", "global")
# Location labels that carry no region information at all. Postings labeled
# this way (or unlabeled) are kept, and the region-lock scanner below makes
# the final call from the title/description text.
GENERIC_REMOTE_LABELS = frozenset(
    {"remote", "remoto", "remote work", "work from home", "fully remote", "100% remote"}
)
_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")
WWR_FULL_DESCRIPTION_MIN_CHARS = 400

# Countries/regions a lock phrase can point at. Indonesia is deliberately
# absent: the candidate lives there, so "must be based in Indonesia" is an
# acceptance signal, never a rejection. High precision matters more than
# full coverage, so ambiguous short forms are left out where risky.
_LOCKED_REGIONS = (
    "united states", "u.s.a", "u.s", "usa", "us", "america",
    "canada",
    "united kingdom", "great britain", "england", "scotland", "wales", "uk",
    "europe", "european", "european union", "emea", "eu",
    "north america", "south america", "latin america", "latam", "americas",
    "brazil", "mexico", "argentina", "colombia", "chile",
    "spain", "portugal", "germany", "france", "italy", "netherlands",
    "belgium", "switzerland", "austria", "sweden", "norway", "denmark",
    "finland", "poland", "czech", "hungary", "romania", "greece",
    "ireland", "ukraine", "russia", "lithuania", "turkey", "israel",
    "uae", "dubai", "saudi", "qatar", "egypt", "morocco",
    "nigeria", "kenya", "south africa", "africa",
    "india", "pakistan", "bangladesh", "sri lanka", "nepal",
    "philippines", "vietnam", "thailand", "malaysia", "singapore",
    "china", "hong kong", "taiwan", "japan", "korea", "seoul",
    "australia", "new zealand", "oceania",
)
_THE = r"(?:the\s+)?"
_REGION_ALTERNATION = "|".join(
    re.escape(region) for region in sorted(_LOCKED_REGIONS, key=len, reverse=True)
)
_REGION = rf"\b(?:{_REGION_ALTERNATION})\b"

# High-precision lock patterns only. A bare "based in <region>" without a
# requirement word usually describes the company HQ rather than a hiring
# restriction, and timezone overlap wishes (CET/EST) are workable from
# Indonesia, so neither is matched.
_REGION_LOCK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # "US only", "India - only"
        rf"{_REGION}\s*[-\u2013\u2014]?\s*\bonly\b",
        # "Remote - US", "Remote | South Africa", "remote from Canada"
        rf"\bremote\s*[-\u2013\u2014|/,]\s*(?:in\s+|from\s+)?{_THE}{_REGION}",
        rf"\bremote\s+in\s+{_THE}{_REGION}",
        # "must be based in the U.S.", "required to reside within Canada"
        (
            rf"\b(?:must|need to|required to|should)\s+(?:be\s+)?"
            rf"(?:based|located|resident|reside|residing|living|situated)"
            rf"\s+(?:in|within)\s+{_THE}{_REGION}"
        ),
        # "residents of the UK", "citizens from India"
        rf"\b(?:residents?|citizens?|nationals?)\s+(?:of|from)\s+{_THE}{_REGION}",
        # "eligible to work in Canada", "authorized to work in the United States"
        (
            rf"\b(?:eligible to work|eligibility to work|right to work|work authorization|"
            rf"authorized to work|authorised to work|work permit)\b"
            rf"[^.!?]{{0,80}}?\bin\b[^.!?]{{0,60}}?{_THE}{_REGION}"
        ),
        # "US work authorization", "work permit for India"
        rf"{_THE}{_REGION}\s+(?:work authorization|work permit|work visa)s?\b",
        rf"\b(?:work authorization|work permit|work visa)s?\s+(?:for|in)\s+{_THE}{_REGION}",
        # "restricted to US candidates", "limited to residents of Brazil"
        rf"\b(?:restricted|limited)\s+to\b[^.!?]{{0,40}}?{_THE}{_REGION}",
        # "This position is based in Germany", "you will be living in Spain"
        (
            rf"\b(?:positions?|roles?|jobs?|you|candidates?|applicants?|incumbents?)\b"
            rf"[^.!?]{{0,40}}?"
            rf"\b(?:based|located|resident|reside|living|situated|working)"
            rf"\s+(?:in|within|from)\s+{_THE}{_REGION}"
        ),
        # "based in Mexico or Brazil", "Location: based in the Netherlands"
        (
            rf"\bbased\s+in\s+{_THE}{_REGION}\b[^.!?]{{0,20}}?\b(?:or|and)\s+{_REGION}"
            rf"|\blocation\b[^.!?]{{0,30}}?\bbased\s+in\s+{_THE}{_REGION}"
        ),
        # "South Africa - Remote", "Netherlands - Field based"
        rf"{_THE}{_REGION}\s*[-\u2013\u2014|/,]\s*(?:remote|field|hybrid|onsite)\b",
        # "work from anywhere in the Philippines" ("anywhere in the world" is safe:
        # "world" is not a locked region)
        rf"\banywhere\s+(?:in|across|within)\s+{_THE}{_REGION}",
        # "Philippines (Remote)"
        rf"{_THE}{_REGION}\s*\(\s*(?:remote|hybrid|onsite)\s*\)",
        # "staffing partners across the United States", but not "across US time
        # zones" -- timezone overlap is workable from Indonesia, a hiring pool
        # "across <region>" is not.
        rf"\bacross\s+{_THE}{_REGION}\b(?!\.?\s*time)",
        # "only applicants from India", "we hire only in Europe"
        rf"\bonly\b[^.!?]{{0,30}}?\b(?:from|in)\s+{_THE}{_REGION}",
    )
)
# "(US)" / "(Remote - India)" style suffixes; only trusted in title/location
# text because parentheses inside descriptions are too noisy.
_PAREN_REGION_RE = re.compile(
    rf"\(\s*(?:remote\s*[-\u2013\u2014]\s*)?{_THE}{_REGION}\s*\)", re.IGNORECASE
)

# Some boards serve double-encoded text (UTF-8 bytes decoded as latin-1),
# turning an en dash into "\u00e2\u0080\u0093". Repair the sequences that
# matter for lock matching before scanning.
_MOJIBAKE_FIXES = (
    ("\u00e2\u0080\u0093", "\u2013"),  # en dash
    ("\u00e2\u0080\u0094", "\u2014"),  # em dash
    ("\u00e2\u0080\u00a2", "\u2022"),  # bullet
    ("\u00e2\u0080\u0099", "\u2019"),  # right single quote
)


def _fix_mojibake(text: str) -> str:
    for broken, fixed in _MOJIBAKE_FIXES:
        text = text.replace(broken, fixed)
    return text


def location_is_worldwide(location: str | None) -> bool:
    """True when a posting accepts candidates anywhere or omits the region.

    Empty values and bare "Remote"-style labels carry no region information,
    so those postings are kept and the region-lock scanner makes the final
    call from the description text.
    """
    if not location:
        return True
    text = str(location).lower().strip()
    if text in GENERIC_REMOTE_LABELS:
        return True
    return any(token in text for token in RELEVANT_LOCATION_TOKENS)


def wwr_region_is_worldwide(region: str | None) -> bool:
    """True only for explicit worldwide WeWorkRemotely regions.

    WWR always labels its regions, so an unlabeled item is treated as
    region-locked and dropped instead of silently kept.
    """
    if not region:
        return False
    text = str(region).lower()
    return "anywhere" in text or "world" in text


def region_lock_detected(short_text: str | None, long_text: str | None = None) -> bool:
    """True when the text contains a high-confidence region-lock phrase.

    short_text is the title/location line, where parenthesized region tags
    like "(US)" also count as locks; long_text is the cleaned description.
    Indonesia never appears in the locked-region list, so a requirement to
    be based there is not a rejection.
    """
    short = _fix_mojibake(short_text or "")
    long_text = _fix_mojibake(long_text) if long_text else None
    if _PAREN_REGION_RE.search(short):
        return True
    haystack = f"{short}\n{long_text}" if long_text else short
    return any(pattern.search(haystack) for pattern in _REGION_LOCK_PATTERNS)


def title_is_relevant(title: str, tags: list | None = None) -> bool:
    """True when the title/tags match the candidate's role families."""
    words = {word for word in _WORD_SPLIT_RE.split(str(title).lower()) if word}
    if words & RELEVANT_TITLE_TOKENS:
        return True
    for tag in tags or []:
        if str(tag).lower() in RELEVANT_TITLE_TOKENS:
            return True
    return False


def build_remotive_url(query: str, limit: int = 50) -> str:
    params = urlencode({"search": query, "limit": max(1, int(limit))})
    return f"{REMOTIVE_API}?{params}"


def normalize_remotive_job(item: dict) -> DiscoveredJob | None:
    """Normalize one Remotive API job; None when unusable or out of scope."""
    url = item.get("url")
    title = item.get("title")
    if not url or not title:
        return None
    # Remotive reliably labels the candidate location; unlabeled means
    # unknown scope and is dropped like any foreign region.
    location = item.get("candidate_required_location")
    if not location or not location_is_worldwide(location):
        return None
    if not title_is_relevant(title, item.get("tags")):
        return None
    description = clean_html_text(item.get("description"))
    if region_lock_detected(f"{title} {location}", description):
        return None
    return DiscoveredJob(
        url=str(url),
        title=str(title),
        company=str(item["company_name"]) if item.get("company_name") else None,
        salary=str(item["salary"]) if item.get("salary") else None,
        description=(description[:500] if description else None),
        location=str(location) if location else None,
        site="Remotive",
        strategy="remoteboards_http",
        full_description=description,
    )


def _format_remoteok_salary(item: dict) -> str | None:
    minimum = item.get("salary_min")
    maximum = item.get("salary_max")
    currency = item.get("salary_currency") or ""
    if minimum is None and maximum is None:
        return None
    if minimum is not None and maximum is not None:
        return f"{currency} {minimum}-{maximum}".strip()
    return f"{currency} {minimum if minimum is not None else maximum}".strip()


def normalize_remoteok_job(item) -> DiscoveredJob | None:
    """Normalize one RemoteOK API entry; None when unusable or out of scope.

    The first element of the RemoteOK payload is a legal notice string, so
    anything without a "position" key is dropped here.
    """
    if not isinstance(item, dict) or "position" not in item:
        return None
    url = item.get("url")
    title = item.get("position")
    if not url or not title:
        return None
    location = item.get("location")
    if not location_is_worldwide(location):
        return None
    if not title_is_relevant(title, item.get("tags")):
        return None
    description = clean_html_text(item.get("description"))
    # RemoteOK leaves the location empty for most postings, so the scanner
    # is the only defense against region-locked fine print.
    if region_lock_detected(f"{title} {location or ''}", description):
        return None
    application_url = item.get("apply_url")
    if application_url in (None, "", url):
        application_url = None
    return DiscoveredJob(
        url=str(url),
        title=str(title),
        company=str(item["company"]) if item.get("company") else None,
        salary=_format_remoteok_salary(item),
        description=(description[:500] if description else None),
        location=str(location) if location else None,
        site="RemoteOK",
        strategy="remoteboards_http",
        full_description=description,
        application_url=str(application_url) if application_url else None,
    )


def parse_wwr_feed(xml_text: str) -> list[dict]:
    """Parse a WeWorkRemotely RSS feed into flat dicts with local tag names."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    items = []
    for element in root.iter("item"):
        fields = {}
        for child in element:
            name = child.tag.split("}")[-1]
            fields[name] = (child.text or "").strip()
        if fields:
            items.append(fields)
    return items


def normalize_wwr_job(item: dict) -> DiscoveredJob | None:
    """Normalize one WeWorkRemotely RSS item; None when unusable or regional.

    Titles use a "Company: Position" format. The RSS description is kept as
    the full description only when it is long enough to be the real listing;
    otherwise the enrichment stage fetches the detail page.
    """
    link = item.get("link")
    raw_title = item.get("title")
    if not link or not raw_title:
        return None
    region = item.get("region") or ""
    if not wwr_region_is_worldwide(region):
        return None

    company, separator, position = raw_title.partition(":")
    if not separator or not position.strip():
        company, position = "", raw_title
    position = position.strip()
    if not title_is_relevant(position):
        return None

    description = clean_html_text(item.get("description"))
    if region_lock_detected(f"{position} {region}", description):
        return None
    full_description = (
        description if description and len(description) >= WWR_FULL_DESCRIPTION_MIN_CHARS else None
    )
    location_bits = [bit for bit in (region, item.get("type")) if bit]
    return DiscoveredJob(
        url=str(link),
        title=position,
        company=company.strip() or None,
        description=(description[:500] if description else None),
        location=" - ".join(location_bits) or None,
        site="WeWorkRemotely",
        strategy="remoteboards_rss",
        full_description=full_description,
    )


def _store_unique(conn, batch: list[DiscoveredJob], seen: set[str]) -> tuple[int, int, int]:
    """Store jobs not already seen in this run; returns (found, new, existing)."""
    unique = []
    for job in batch:
        if job.url in seen:
            continue
        seen.add(job.url)
        unique.append(job)
    inserted, duplicates = store_discovered_jobs(conn, unique)
    return len(unique), inserted, duplicates


def _board_status(errors: int, attempts: int) -> str:
    if attempts > 0 and errors == attempts:
        return "error"
    if errors:
        return "partial"
    return "ok"


def _discover_with_http(
    search_config: dict,
    http_get: Callable[[str], str],
    conn,
    sleep: Callable[[float], None],
) -> dict:
    settings = adapter_config(search_config, "remoteboards")
    maximum_tier = int(settings.get("max_tier", 2))
    days_back = int(settings.get("days_back", 30))
    delay = max(0.0, float(settings.get("delay_seconds", 1.0)))
    remotive_limit = int(settings.get("limit_per_query", 50))
    boards = tuple(
        str(name).lower() for name in (settings.get("boards") or DEFAULT_BOARDS)
    )
    queries = selected_queries(search_config, maximum_tier)

    found = new = existing = errors = 0
    seen: set[str] = set()
    board_errors: dict[str, int] = {}
    board_attempts: dict[str, int] = {}

    if "remotive" in boards:
        board_errors["remotive"] = 0
        board_attempts["remotive"] = len(queries)
        for query_index, query in enumerate(queries):
            try:
                data = json.loads(http_get(build_remotive_url(query, remotive_limit)))
                batch = [
                    job
                    for job in (
                        normalize_remotive_job(item)
                        for item in (data.get("jobs") or [])
                        if published_within(item.get("publication_date"), days_back)
                    )
                    if job is not None
                ]
                batch_found, batch_new, batch_existing = _store_unique(conn, batch, seen)
                found += batch_found
                new += batch_new
                existing += batch_existing
            except Exception as exc:  # noqa: BLE001 - isolate each external query
                board_errors["remotive"] += 1
                log.warning("Remotive query '%s' failed: %s", query, exc)
            if delay and query_index < len(queries) - 1:
                sleep(delay)

    if "remoteok" in boards:
        board_errors["remoteok"] = 0
        board_attempts["remoteok"] = 1
        try:
            items = json.loads(http_get(REMOTEOK_API))
            batch = [
                job
                for job in (
                    normalize_remoteok_job(item)
                    for item in items
                    if isinstance(item, dict)
                    and published_within(item.get("date"), days_back)
                )
                if job is not None
            ]
            batch_found, batch_new, batch_existing = _store_unique(conn, batch, seen)
            found += batch_found
            new += batch_new
            existing += batch_existing
        except Exception as exc:  # noqa: BLE001 - isolate each external board
            board_errors["remoteok"] += 1
            log.warning("Remote board 'remoteok' failed: %s", exc)

    if "weworkremotely" in boards:
        board_errors["weworkremotely"] = 0
        board_attempts["weworkremotely"] = len(WWR_FEEDS)
        for feed_url in WWR_FEEDS:
            try:
                items = parse_wwr_feed(http_get(feed_url))
                batch = [
                    job
                    for job in (
                        normalize_wwr_job(item)
                        for item in items
                        if published_within(item.get("pubDate"), days_back)
                    )
                    if job is not None
                ]
                batch_found, batch_new, batch_existing = _store_unique(conn, batch, seen)
                found += batch_found
                new += batch_new
                existing += batch_existing
            except Exception as exc:  # noqa: BLE001 - isolate each external feed
                board_errors["weworkremotely"] += 1
                log.warning("WeWorkRemotely feed failed: %s", exc)
            if delay:
                sleep(delay)

    board_results = {
        name: {
            "status": _board_status(board_errors[name], board_attempts[name]),
            "errors": board_errors[name],
        }
        for name in board_errors
    }
    for result in board_results.values():
        errors += int(result.get("errors", 0))
    statuses = [result["status"] for result in board_results.values()]
    if not statuses:
        status = "ok"
    elif all(item == "error" for item in statuses):
        status = "error"
    elif any(item in ("error", "partial") for item in statuses):
        status = "partial"
    else:
        status = "ok"

    return {
        "status": status,
        "found": found,
        "new": new,
        "existing": existing,
        "errors": errors,
        "boards": board_results,
    }


def discover_remoteboards(
    search_config: dict | None = None,
    *,
    http_get: Callable[[str], str] | None = None,
    conn=None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Discover remote board jobs and persist normalized records."""
    search_config = search_config if search_config is not None else config.load_search_config()
    conn = conn or init_db()

    if http_get is not None:
        return _discover_with_http(search_config, http_get, conn, sleep)

    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        timeout=30.0,
        follow_redirects=True,
    ) as client:

        def _get(url: str) -> str:
            response = client.get(url)
            response.raise_for_status()
            return response.text

        return _discover_with_http(search_config, _get, conn, sleep)
