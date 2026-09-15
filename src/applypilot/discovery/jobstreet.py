"""Best-effort discovery from JobStreet Indonesia's public search pages.

JobStreet refuses plain HTTP clients (Akamai 403), so this adapter drives a
real Chrome window with a persistent profile like the HiringCafe adapter and
reads the server-rendered window.SEEK_REDUX_DATA payload. It never solves or
bypasses challenges and stops cleanly when automated access is refused.

Search results carry no full description; only a teaser is stored so the
enrichment stage fetches each detail page (which exposes JSON-LD).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager

from playwright.sync_api import (
    Browser,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from applypilot import config
from applypilot.database import init_db, store_discovered_jobs
from applypilot.discovery._util import adapter_config, kebab_slug, selected_queries
from applypilot.discovery.models import DiscoveredJob

log = logging.getLogger(__name__)
BASE_URL = "https://www.jobstreet.co.id"
REDUX_MARKER = "window.SEEK_REDUX_DATA"
PAGE_SIZE = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)


def build_search_url(query: str, page: int = 1, location_slug: str | None = None) -> str:
    """Build a JobStreet search URL like /ui-ux-designer-jobs/in-Jakarta?page=2."""
    path = f"/{kebab_slug(query)}-jobs"
    if location_slug:
        path = f"{path}/in-{str(location_slug).strip()}"
    return f"{BASE_URL}{path}?page={max(1, int(page))}"


def parse_redux_data(html: str) -> dict | None:
    """Extract the SEEK_REDUX_DATA JSON blob from server-rendered HTML.

    The script tag contains trailing content, so json.loads on the whole page
    fails and regex cannot handle the nested braces: locate the marker and use
    raw_decode from the first '{' after it.
    """
    marker = html.find(REDUX_MARKER)
    if marker == -1:
        return None
    brace = html.find("{", marker)
    if brace == -1:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(html, brace)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def extract_jobs(payload: dict) -> list[dict]:
    """Pull the job list out of the redux payload (results.results.jobs)."""
    results = payload.get("results") or {}
    inner = results.get("results") or {}
    jobs = inner.get("jobs")
    return jobs if isinstance(jobs, list) else []


def _label_of(value) -> str | None:
    """Best-effort label from a string or a SEEK-style {label/id} object."""
    if isinstance(value, dict):
        label = value.get("label")
        if isinstance(label, dict):
            label = label.get("text")
        if not label:
            label = value.get("id")
        return str(label) if label else None
    if value:
        return str(value)
    return None


def _arrangement_label(value) -> str | None:
    """Work-arrangement labels, mapping SEEK's workFromHome id to 'Remote'."""
    if isinstance(value, dict) and str(value.get("id") or "") == "workFromHome":
        return "Remote"
    if not isinstance(value, dict) and str(value) == "workFromHome":
        return "Remote"
    return _label_of(value)


def _arrangement_labels(value) -> list[str]:
    """Extract work-arrangement labels.

    Seen in the wild as a dict {"data": [{"id", "label": {"text"}}],
    "displayText": "Remote"} and, on older schemas, as a plain list.
    """
    if not value:
        return []
    if isinstance(value, dict):
        display = value.get("displayText")
        if display:
            return [str(display)]
        entries = value.get("data") or []
    elif isinstance(value, list):
        entries = value
    else:
        return [str(value)]
    labels = []
    for entry in entries:
        label = _arrangement_label(entry)
        if label:
            labels.append(label)
    return labels


def _location_labels(locations) -> str | None:
    """Extract readable location labels.

    Seen in the wild as a list of dicts with a "label" key and, on older
    schemas, as a list of plain strings.
    """
    if not locations:
        return None
    if isinstance(locations, (str, dict)):
        locations = [locations]
    parts = []
    for entry in locations:
        if isinstance(entry, dict):
            label = entry.get("label")
            if isinstance(label, dict):
                label = label.get("text")
            if label:
                parts.append(str(label))
        elif entry:
            parts.append(str(entry))
    return ", ".join(dict.fromkeys(parts)) or None


def normalize_job(item: dict) -> DiscoveredJob | None:
    """Normalize one SEEK redux job item; returns None when unusable."""
    job_id = item.get("id")
    title = item.get("title")
    if not job_id or not title:
        return None

    advertiser = item.get("advertiser") or {}
    company = item.get("companyName") or (
        advertiser.get("description") if isinstance(advertiser, dict) else None
    )

    location = _location_labels(item.get("locations"))

    extras = []
    for value in item.get("workTypes") or []:
        label = _label_of(value)
        if label:
            extras.append(label)
    extras += _arrangement_labels(item.get("workArrangements"))
    if extras:
        suffix = ", ".join(dict.fromkeys(extras))
        location = f"{location} ({suffix})" if location else suffix

    parts = [str(item["teaser"])] if item.get("teaser") else []
    parts += [str(bullet) for bullet in (item.get("bulletPoints") or []) if bullet]
    description = "\n".join(parts)[:500] or None

    return DiscoveredJob(
        url=f"{BASE_URL}/job/{job_id}",
        title=str(title),
        company=str(company) if company else None,
        salary=str(item["salaryLabel"]) if item.get("salaryLabel") else None,
        description=description,
        location=location,
        site="JobStreet",
        strategy="jobstreet_browser",
    )


class JobStreetPageLoader(AbstractContextManager):
    """One-browser loader for JobStreet search pages; never bypasses challenges.

    Uses a dedicated persistent profile so repeat visits look like a returning
    visitor. If a challenge appears, the loader waits once for it to
    auto-resolve like a normal page load would, then stops.
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
            self._page = (
                self._context.pages[0] if self._context.pages else self._context.new_page()
            )
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
        return (
            "cloudflare" in body
            or "challenge" in body
            or "access denied" in body
            or "just a moment" in title
        )

    def load(self, url: str) -> dict:
        if self._page is None:
            raise RuntimeError("JobStreet browser is not started")
        page = self._page
        response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        if response and response.status in (403, 429):
            raise RuntimeError(f"JobStreet refused automated access (HTTP {response.status})")

        payload = parse_redux_data(page.content())
        if payload is None:
            if not self._is_challenge_page(page):
                raise RuntimeError("JobStreet page did not expose search data")
            # Challenges often auto-resolve; give the page one bounded chance
            # to settle, exactly like a user waiting it out.
            log.info(
                "JobStreet challenge detected; waiting up to %ds for auto-resolve",
                self.challenge_wait_ms // 1000,
            )
            try:
                page.wait_for_function(
                    f"() => document.documentElement.innerHTML.includes('{REDUX_MARKER}')",
                    timeout=self.challenge_wait_ms,
                )
            except PlaywrightTimeoutError:
                raise RuntimeError(
                    "JobStreet presented a browser challenge; stopping without bypass"
                ) from None
            payload = parse_redux_data(page.content())
            if payload is None:
                raise RuntimeError("JobStreet search data never appeared")
        return payload

    def __exit__(self, exc_type, exc, traceback):
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        return False


def _discover_with_loader(
    search_config: dict,
    page_loader: Callable[[str], dict],
    conn,
    sleep: Callable[[float], None],
) -> dict:
    settings = adapter_config(search_config, "jobstreet")
    maximum_tier = int(settings.get("max_tier", 2))
    max_pages = max(1, int(settings.get("max_pages_per_query", 1)))
    delay = max(0.0, float(settings.get("delay_seconds", 1.5)))
    location_slug = settings.get("location_slug") or None
    queries = selected_queries(search_config, maximum_tier)

    found = new = existing = errors = 0
    seen: set[str] = set()

    for query_index, query in enumerate(queries):
        query_failed = False
        for page_number in range(1, max_pages + 1):
            try:
                payload = page_loader(
                    build_search_url(query, page=page_number, location_slug=location_slug)
                )
            except Exception as exc:  # noqa: BLE001 - isolate each external query
                errors += 1
                query_failed = True
                log.warning("JobStreet query '%s' stopped: %s", query, exc)
                break

            items = extract_jobs(payload)
            batch = []
            for item in items:
                job = normalize_job(item)
                if job is None or job.url in seen:
                    continue
                seen.add(job.url)
                batch.append(job)

            found += len(batch)
            inserted, duplicates = store_discovered_jobs(conn, batch)
            new += inserted
            existing += duplicates

            if len(items) < PAGE_SIZE:
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


def discover_jobstreet(
    search_config: dict | None = None,
    *,
    page_loader: Callable[[str], dict] | None = None,
    conn=None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Discover JobStreet Indonesia jobs and persist normalized records."""
    search_config = search_config if search_config is not None else config.load_search_config()
    conn = conn or init_db()

    if page_loader is not None:
        return _discover_with_loader(search_config, page_loader, conn, sleep)

    settings = adapter_config(search_config, "jobstreet")
    default_profile = str(config.APP_DIR / "browser-profiles" / "jobstreet")
    with JobStreetPageLoader(
        headless=bool(settings.get("headless", False)),
        timeout_ms=int(settings.get("timeout_ms", 60_000)),
        challenge_wait_ms=int(settings.get("challenge_wait_ms", 30_000)),
        profile_dir=str(settings.get("profile_dir") or default_profile),
    ) as loader:
        return _discover_with_loader(search_config, loader.load, conn, sleep)
