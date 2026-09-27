"""Discovery adapter for Kalibrr Indonesia using server-rendered Next.js data.

Kalibrr is an employer-verified career portal widely used by Indonesian banks,
enterprises, tech startups, and remote-friendly companies.

Zero browser needed: pages expose __NEXT_DATA__ JSON with full job descriptions,
qualifications, company verification, salary, and remote/work-from-home flags.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from applypilot import config
from applypilot.database import get_connection, init_db, store_discovered_jobs
from applypilot.discovery._util import (
    adapter_config,
    clean_html_text,
    kebab_slug,
    published_within_days,
    selected_queries,
)
from applypilot.discovery.models import DiscoveredJob
from applypilot.discovery.remoteboards import (
    location_is_worldwide,
    region_lock_detected,
    title_is_relevant,
)

log = logging.getLogger(__name__)

BASE_URL = "https://www.kalibrr.com"
DEFAULT_DELAY = 1.0
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_DAYS_OLD = 7  # Public job boards strictly prioritize fresh postings (< 7 days)
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "id,en;q=0.9",
}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def build_search_url(query: str, page: int = 1) -> str:
    """Build a search URL for Kalibrr job board."""
    slug = kebab_slug(query)
    return f"{BASE_URL}/id-ID/job-board/te/{slug}/{page}"


def extract_jobs_from_html(html: str) -> tuple[list[dict[str, Any]], int]:
    """Extract jobs array and total count from __NEXT_DATA__ payload."""
    match = NEXT_DATA_RE.search(html)
    if not match:
        return [], 0

    try:
        data = json.loads(match.group(1))
        page_props = data.get("props", {}).get("pageProps", {})
        jobs = page_props.get("jobs", [])
        count = int(page_props.get("count", len(jobs)) or 0)
        return jobs, count
    except Exception as exc:
        log.warning("Failed to parse Kalibrr __NEXT_DATA__: %s", exc)
        return [], 0


def format_salary(item: dict[str, Any]) -> str | None:
    """Format salary range if shown by employer."""
    if not item.get("salaryShown"):
        return None

    base = item.get("baseSalary")
    max_s = item.get("maximumSalary")
    curr = item.get("salaryCurrency") or "IDR"

    try:
        if base and max_s:
            return f"{curr} {int(base):,} - {int(max_s):,}"
        elif base:
            return f"{curr} {int(base):,}"
    except (ValueError, TypeError):
        return None
    return None


def format_location(item: dict[str, Any]) -> str:
    """Extract location string with WFH/Remote indicators."""
    google_loc = item.get("googleLocation") or {}
    addr = google_loc.get("addressComponents") or {}
    city = addr.get("city") or addr.get("region") or addr.get("country") or "Indonesia"
    is_wfh = bool(item.get("isWorkFromHome"))

    if is_wfh and city:
        return f"{city} (Remote / WFH)"
    elif is_wfh:
        return "Remote / Work From Home"
    return city


def normalize_kalibrr_job(item: dict[str, Any]) -> DiscoveredJob | None:
    """Transform a Kalibrr API job object into a canonical DiscoveredJob."""
    job_id = item.get("id")
    title = item.get("name")
    if not job_id or not title:
        return None

    slug = item.get("slug") or str(job_id)
    company_info = item.get("companyInfo") or item.get("company") or {}
    company_name = company_info.get("name") or item.get("companyName") or "Unknown"
    company_code = company_info.get("code")

    if company_code:
        job_url = f"{BASE_URL}/c/{company_code}/jobs/{job_id}/{slug}"
    else:
        job_url = f"{BASE_URL}/jobs/{job_id}"

    app_url = item.get("applyRedirectUrl") or job_url

    desc_html = item.get("description") or ""
    qual_html = item.get("qualifications") or ""
    combined_html = f"{desc_html}\n\nQualifications:\n{qual_html}" if qual_html else desc_html
    full_desc = clean_html_text(combined_html)
    short_desc = full_desc[:500] if full_desc else None
    posted_at = item.get("createdAt") or item.get("activationDate")

    return DiscoveredJob(
        url=job_url,
        title=title.strip(),
        site="kalibrr",
        strategy="kalibrr_api",
        company=company_name.strip(),
        salary=format_salary(item),
        description=short_desc,
        full_description=full_desc,
        location=format_location(item),
        application_url=app_url,
        posted_at=posted_at,
    )


def is_kalibrr_job_eligible(
    job: DiscoveredJob,
    item_raw: dict[str, Any],
    search_queries: list[str] | None = None,
    accept_locations: list[str] | None = None,
    reject_locations: list[str] | None = None,
    max_days_old: int = 0,
) -> bool:
    """Filter Kalibrr jobs for role relevance, posting freshness, and location compatibility."""
    # 0. Cutoff date check (undated postings pass)
    if max_days_old > 0 and job.posted_at:
        if not published_within_days(job.posted_at, max_days_old):
            return False

    # 1. Title relevance
    title_lower = job.title.lower()
    matches_token = title_is_relevant(job.title)
    matches_query = False
    if search_queries:
        for q in search_queries:
            q_words = [w for w in q.lower().split() if len(w) > 2]
            if q_words and all(w in title_lower for w in q_words):
                matches_query = True
                break

    if not (matches_token or matches_query):
        return False

    # 2. Location compatibility
    is_wfh = bool(item_raw.get("isWorkFromHome"))
    loc_lower = (job.location or "").lower()

    # Indonesia cities or accept list match
    accept = accept_locations or ["indonesia", "jakarta", "bandung", "west java", "jabodetabek"]
    if any(a.lower() in loc_lower for a in accept):
        return True

    # WFH / Remote jobs
    if is_wfh or location_is_worldwide(job.location):
        if region_lock_detected(job.location, job.full_description):
            return False
        return True

    # Reject non-remote in foreign locations (e.g. Philippines onsite)
    reject = reject_locations or []
    if any(r.lower() in loc_lower for r in reject):
        return False

    return True


def discover_kalibrr(
    search_config: dict | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    """Run Kalibrr discovery across configured search queries."""
    cfg = search_config or config.load_search_config()
    adapter_cfg = adapter_config(cfg, "kalibrr")

    max_tier = int(adapter_cfg.get("max_tier", 2))
    max_days_old = int(adapter_cfg.get("max_days_old", DEFAULT_MAX_DAYS_OLD))
    max_pages = int(adapter_cfg.get("max_pages_per_query", 2))
    delay = float(adapter_cfg.get("delay_seconds", DEFAULT_DELAY))
    timeout = float(adapter_cfg.get("timeout_seconds", DEFAULT_TIMEOUT))

    queries = selected_queries(cfg, max_tier)
    accept_loc = cfg.get("location_accept")
    reject_loc = cfg.get("location_reject_non_remote")

    if not queries:
        log.info("No queries found for Kalibrr discovery (tier <= %d)", max_tier)
        return {"status": "ok", "discovered": 0, "new": 0, "duplicates": 0, "errors": 0}

    discovered: list[DiscoveredJob] = []
    errors = 0

    with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout) as client:
        for query in queries:
            for page in range(1, max_pages + 1):
                url = build_search_url(query, page)
                log.info("Kalibrr fetching: %s", url)
                try:
                    resp = client.get(url)
                    if resp.status_code == 404:
                        break
                    resp.raise_for_status()

                    raw_jobs, total_count = extract_jobs_from_html(resp.text)
                    if not raw_jobs:
                        break

                    for raw in raw_jobs:
                        job = normalize_kalibrr_job(raw)
                        if job and is_kalibrr_job_eligible(
                            job=job,
                            item_raw=raw,
                            search_queries=queries,
                            accept_locations=accept_loc,
                            reject_locations=reject_loc,
                            max_days_old=max_days_old,
                        ):
                            discovered.append(job)

                    # If page * 15 >= total_count, no more pages
                    if page * len(raw_jobs) >= total_count:
                        break

                except Exception as exc:
                    log.warning("Error fetching Kalibrr query '%s' page %d: %s", query, page, exc)
                    errors += 1
                    break

                if delay > 0:
                    time.sleep(delay)

    # Deduplicate within this run by url
    seen_urls: set[str] = set()
    unique_jobs: list[DiscoveredJob] = []
    for j in discovered:
        if j.url not in seen_urls:
            seen_urls.add(j.url)
            unique_jobs.append(j)

    if conn is None:
        init_db()
        conn = get_connection()
    new_count, dup_count = store_discovered_jobs(conn, unique_jobs)

    log.info(
        "Kalibrr discovery complete: discovered=%d, new=%d, duplicates=%d, errors=%d",
        len(unique_jobs),
        new_count,
        dup_count,
        errors,
    )
    return {
        "status": "partial" if errors and new_count == 0 else "ok",
        "discovered": len(unique_jobs),
        "new": new_count,
        "duplicates": dup_count,
        "errors": errors,
    }
