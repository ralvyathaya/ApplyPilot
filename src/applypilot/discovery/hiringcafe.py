"""Best-effort personal discovery from HiringCafe's public search UI.

HiringCafe does not publish a supported developer API. This adapter uses the
server-rendered page that a normal browser receives, does not solve or bypass
challenges, and stops cleanly if automated access is refused.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from html import unescape
from urllib.parse import quote, urlencode, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import (
    Browser,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from applypilot import config
from applypilot.database import init_db, store_discovered_jobs
from applypilot.discovery.models import DiscoveredJob

log = logging.getLogger(__name__)
BASE_URL = "https://hiring.cafe"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)


def build_search_state(query: str, days: int = 7) -> dict:
    """Build a conservative Indonesia search state for HiringCafe."""
    return {
        "searchQuery": query,
        "locations": [
            {
                "formatted_address": "Indonesia",
                "types": ["country"],
                "id": "applypilot_indonesia",
                "address_components": [
                    {
                        "long_name": "Indonesia",
                        "short_name": "ID",
                        "types": ["country"],
                    }
                ],
                "options": {
                    "flexible_regions": [
                        "anywhere_in_continent",
                        "anywhere_in_world",
                    ]
                },
            }
        ],
        "defaultToUserLocation": False,
        "userLocation": None,
        "dateFetchedPastNDays": days,
        "sortBy": "date",
    }


def build_search_url(query: str, days: int = 7, page: int = 0) -> str:
    state = json.dumps(build_search_state(query, days), separators=(",", ":"))
    return f"{BASE_URL}/?{urlencode({'searchState': state, 'page': page})}"


def _first(*values):
    return next((value for value in values if value not in (None, "", [], {})), None)


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = BeautifulSoup(unescape(str(value)), "html.parser").get_text("\n", strip=True)
    return text or None


def _external_apply_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    host = parsed.netloc.lower().split(":", 1)[0]
    if host in {"hiring.cafe", "www.hiring.cafe", "hiringcafe.com", "www.hiringcafe.com"}:
        return None
    return value


def _format_salary(v5: dict) -> str | None:
    minimum = _first(
        v5.get("yearly_min_compensation"),
        v5.get("min_compensation"),
        v5.get("salary_min"),
    )
    maximum = _first(
        v5.get("yearly_max_compensation"),
        v5.get("max_compensation"),
        v5.get("salary_max"),
    )
    currency = _first(v5.get("salary_currency"), v5.get("currency"), "")
    if minimum is None and maximum is None:
        return None
    if minimum is not None and maximum is not None:
        return f"{currency} {minimum}-{maximum}".strip()
    return f"{currency} {minimum if minimum is not None else maximum}".strip()


def normalize_hit(hit: dict) -> DiscoveredJob | None:
    """Normalize one undocumented HiringCafe search hit defensively."""
    info = hit.get("job_information") or {}
    v5 = hit.get("v5_processed_job_data") or {}
    enriched = hit.get("enriched_company_data") or {}
    company_info = info.get("company_info") or {}

    source_id = _first(
        hit.get("requisition_id"),
        hit.get("objectID"),
        hit.get("id"),
        hit.get("original_source_id"),
    )
    application_url = _external_apply_url(hit.get("apply_url"))
    if source_id:
        job_url = f"{BASE_URL}/job/{quote(str(source_id), safe='')}"
    else:
        job_url = application_url

    title = _first(info.get("title"), v5.get("core_job_title"), hit.get("title"))
    if not job_url or not title:
        return None

    company = _first(
        company_info.get("name"),
        v5.get("company_name"),
        enriched.get("name"),
        hit.get("company_name"),
    )
    location = _first(
        v5.get("formatted_workplace_location"),
        info.get("location"),
        hit.get("location"),
    )
    workplace_type = v5.get("workplace_type")
    if workplace_type and location and workplace_type.lower() not in location.lower():
        location = f"{location} ({workplace_type})"
    elif workplace_type and not location:
        location = workplace_type

    description = _clean_text(
        _first(
            info.get("description"),
            hit.get("description"),
            v5.get("requirements_summary"),
        )
    )
    summary = _clean_text(v5.get("requirements_summary"))
    skills = v5.get("technical_tools") or []
    details = [part for part in (description, summary) if part]
    if skills:
        details.append("Skills: " + ", ".join(str(skill) for skill in skills))
    full_description = "\n\n".join(dict.fromkeys(details)) or None

    return DiscoveredJob(
        url=job_url,
        title=str(title),
        company=str(company) if company else None,
        salary=_format_salary(v5),
        description=(full_description[:500] if full_description else None),
        location=str(location) if location else None,
        site="HiringCafe",
        strategy="hiringcafe_browser",
        full_description=full_description,
        application_url=application_url,
    )


class HiringCafePageLoader(AbstractContextManager):
    """One-browser loader for SSR search payloads; never bypasses challenges.

    A dedicated persistent profile is used so repeat visits look like a
    returning visitor (cookies accumulate across runs). If a managed
    challenge appears, the loader waits once for it to auto-resolve like a
    normal page load would, then stops. Interactive CAPTCHAs are never
    answered or bypassed.
    """

    def __init__(
        self,
        headless: bool = False,
        timeout_ms: int = 60_000,
        challenge_wait_ms: int = 30_000,
        profile_dir: str | None = None,
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.challenge_wait_ms = challenge_wait_ms
        self.profile_dir = profile_dir
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context = None
        self._page: Page | None = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        chrome_path = config.get_chrome_path()
        if self.profile_dir:
            self._context = self._playwright.chromium.launch_persistent_context(
                self.profile_dir,
                executable_path=chrome_path,
                headless=self.headless,
                user_agent=USER_AGENT,
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        else:
            self._browser = self._playwright.chromium.launch(
                executable_path=chrome_path,
                headless=self.headless,
            )
            context = self._browser.new_context(user_agent=USER_AGENT)
            self._page = context.new_page()
        return self

    def _is_challenge_page(self, page: Page) -> bool:
        title = page.title().lower()
        body = page.locator("body").inner_text(timeout=5_000).lower()
        return "cloudflare" in body or "challenge" in body or "just a moment" in title

    def load(self, url: str) -> dict:
        if self._page is None:
            raise RuntimeError("HiringCafe browser is not started")
        page = self._page
        response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        if response and response.status in (403, 429):
            raise RuntimeError(f"HiringCafe refused automated access (HTTP {response.status})")

        try:
            element = page.wait_for_selector("#__NEXT_DATA__", timeout=self.timeout_ms)
        except PlaywrightTimeoutError as exc:
            if not self._is_challenge_page(page):
                raise RuntimeError("HiringCafe page did not expose search data") from exc
            # Managed challenges often auto-resolve on their own; give the page
            # one bounded chance to settle, exactly like a user waiting it out.
            log.info("HiringCafe challenge detected; waiting up to %ds for auto-resolve",
                     self.challenge_wait_ms // 1000)
            try:
                element = page.wait_for_selector("#__NEXT_DATA__", timeout=self.challenge_wait_ms)
            except PlaywrightTimeoutError:
                raise RuntimeError(
                    "HiringCafe presented a browser challenge; stopping without bypass"
                ) from exc

        if element is None:
            raise RuntimeError("HiringCafe search data element disappeared")
        payload = json.loads(element.text_content() or "{}")
        return payload.get("props", {}).get("pageProps", {})

    def __exit__(self, exc_type, exc, traceback):
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        return False


def _adapter_config(search_config: dict) -> dict:
    discovery = search_config.get("discovery") or {}
    return (discovery.get("adapter_config") or {}).get("hiringcafe") or {}


def _selected_queries(search_config: dict, maximum_tier: int) -> list[str]:
    selected = []
    for item in search_config.get("queries") or []:
        if int(item.get("tier", 99)) > maximum_tier:
            continue
        query = str(item.get("query") or "").strip()
        if query and query not in selected:
            selected.append(query)
    return selected


def _discover_with_loader(
    search_config: dict,
    page_loader: Callable[[str], dict],
    conn,
    sleep: Callable[[float], None],
) -> dict:
    settings = _adapter_config(search_config)
    maximum_tier = int(settings.get("max_tier", 2))
    max_pages = max(1, int(settings.get("max_pages_per_query", 1)))
    days = max(1, int(settings.get("date_fetched_past_n_days", 7)))
    delay = max(0.0, float(settings.get("delay_seconds", 1.5)))
    queries = _selected_queries(search_config, maximum_tier)

    found = new = existing = errors = 0
    seen: set[str] = set()

    for query_index, query in enumerate(queries):
        query_failed = False
        for page in range(max_pages):
            try:
                props = page_loader(build_search_url(query, days=days, page=page))
            except Exception as exc:  # noqa: BLE001 - isolate each external query
                errors += 1
                query_failed = True
                log.warning("HiringCafe query '%s' stopped: %s", query, exc)
                break

            if props.get("ssrError"):
                errors += 1
                query_failed = True
                log.warning("HiringCafe query '%s' failed: %s", query, props["ssrError"])
                break

            hits = props.get("ssrHits") or []
            batch = []
            for hit in hits:
                job = normalize_hit(hit)
                if job is None or job.url in seen:
                    continue
                seen.add(job.url)
                batch.append(job)

            found += len(batch)
            inserted, duplicates = store_discovered_jobs(conn, batch)
            new += inserted
            existing += duplicates

            if props.get("ssrIsLastPage") or not hits:
                break
            if delay:
                sleep(delay)

        if delay and query_index < len(queries) - 1 and not query_failed:
            sleep(delay)

    status = "error" if queries and errors == len(queries) else "partial" if errors else "ok"
    return {
        "status": status,
        "found": found,
        "new": new,
        "existing": existing,
        "errors": errors,
        "queries": len(queries),
    }


def discover_hiringcafe(
    search_config: dict | None = None,
    *,
    page_loader: Callable[[str], dict] | None = None,
    conn=None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Discover HiringCafe jobs and persist normalized direct-apply records."""
    search_config = search_config if search_config is not None else config.load_search_config()
    conn = conn or init_db()

    if page_loader is not None:
        return _discover_with_loader(search_config, page_loader, conn, sleep)

    settings = _adapter_config(search_config)
    default_profile = str(config.APP_DIR / "browser-profiles" / "hiringcafe")
    with HiringCafePageLoader(
        headless=bool(settings.get("headless", False)),
        timeout_ms=int(settings.get("timeout_ms", 60_000)),
        challenge_wait_ms=int(settings.get("challenge_wait_ms", 30_000)),
        profile_dir=str(settings.get("profile_dir") or default_profile),
    ) as loader:
        return _discover_with_loader(search_config, loader.load, conn, sleep)
