"""Discovery adapter for direct company career portals via public ATS APIs.

Scrapes verified company postings directly from ATS engines:
  - Greenhouse: boards-api.greenhouse.io
  - Ashby: api.ashbyhq.com/posting-api
  - Lever: api.lever.co/v0/postings

Zero bot challenges, zero recruiter spam, 100% genuine active jobs directly
from the employer's HR system.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from applypilot import config
from applypilot.config import APP_DIR, CONFIG_DIR
from applypilot.database import get_connection, init_db, store_discovered_jobs
from applypilot.discovery._util import (
    adapter_config,
    clean_html_text,
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

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_WORKERS = 5
DEFAULT_MAX_DAYS_OLD = 30
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def load_companies(custom_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load company registry from user APP_DIR or package CONFIG_DIR."""
    candidates = [
        custom_path,
        APP_DIR / "companies.yaml",
        CONFIG_DIR / "companies.yaml",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                companies = data.get("companies") or {}
                if companies:
                    log.debug("Loaded %d companies from %s", len(companies), candidate)
                    return companies
            except Exception as exc:
                log.warning("Failed to load companies from %s: %s", candidate, exc)

    log.warning("No companies.yaml found in %s or %s", APP_DIR, CONFIG_DIR)
    return {}


def is_job_eligible(
    title: str,
    location_text: str | None,
    description_text: str | None,
    search_queries: list[str] | None = None,
    accept_locations: list[str] | None = None,
    reject_locations: list[str] | None = None,
    posted_at: str | None = None,
    max_days_old: int = 0,
) -> bool:
    """Filter jobs by title relevance, posting date, and location suitability."""
    # 0. Cutoff date check (undated postings pass)
    if max_days_old > 0 and posted_at:
        if not published_within_days(posted_at, max_days_old):
            return False

    # 1. Title relevance
    title_lower = title.lower()
    matches_token = title_is_relevant(title)
    matches_query = False
    if search_queries:
        for q in search_queries:
            q_words = [w for w in q.lower().split() if len(w) > 2]
            if q_words and all(w in title_lower for w in q_words):
                matches_query = True
                break

    if not (matches_token or matches_query):
        return False

    # 2. Location filtering
    loc_clean = (location_text or "").strip()
    loc_lower = loc_clean.lower()

    # Explicit accept matches (e.g. Indonesia, Jakarta, Bandung, West Java)
    accept = accept_locations or ["indonesia", "jakarta", "bandung", "west java"]
    if any(a.lower() in loc_lower for a in accept):
        return True

    # Worldwide / open remote check
    if location_is_worldwide(loc_clean):
        # Check description and location for region lock exclusions
        if region_lock_detected(loc_clean, description_text):
            return False
        return True

    # If location explicitly matches a reject region (e.g. US, UK, Germany) without remote/Indonesia
    reject = reject_locations or []
    if any(r.lower() in loc_lower for r in reject):
        return False

    # If location is empty or generic, check if locked
    if not loc_clean:
        return not region_lock_detected(title, description_text)

    return False


def fetch_greenhouse_jobs(
    client: httpx.Client,
    slug: str,
    company_name: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[DiscoveredJob]:
    """Fetch jobs from Greenhouse public JSON API."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    resp = client.get(url, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}

    jobs: list[DiscoveredJob] = []
    for item in payload.get("jobs", []):
        job_url = item.get("absolute_url")
        title = item.get("title")
        if not job_url or not title:
            continue

        loc_data = item.get("location")
        loc_str = loc_data.get("name") if isinstance(loc_data, dict) else (str(loc_data) if loc_data else None)
        desc = clean_html_text(item.get("content"))
        posted_at = item.get("updated_at") or item.get("first_published")

        jobs.append(
            DiscoveredJob(
                url=job_url,
                title=title.strip(),
                site="greenhouse",
                strategy="direct_ats",
                company=company_name,
                location=loc_str.strip() if loc_str else None,
                full_description=desc,
                application_url=job_url,
                posted_at=posted_at,
            )
        )
    return jobs


def fetch_ashby_jobs(
    client: httpx.Client,
    slug: str,
    company_name: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[DiscoveredJob]:
    """Fetch jobs from Ashby public JSON API."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    resp = client.get(url, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}

    jobs: list[DiscoveredJob] = []
    for item in payload.get("jobs", []):
        job_url = item.get("jobUrl")
        title = item.get("title")
        if not job_url or not title:
            continue

        loc = item.get("location") or ""
        is_remote = item.get("isRemote")
        if is_remote and loc:
            location_str = f"{loc} (Remote)"
        elif is_remote:
            location_str = "Remote"
        else:
            location_str = loc or None

        desc_plain = item.get("descriptionPlain")
        desc = desc_plain.strip() if desc_plain else clean_html_text(item.get("descriptionHtml"))
        app_url = item.get("applyUrl") or job_url
        posted_at = item.get("publishedAt")

        jobs.append(
            DiscoveredJob(
                url=job_url,
                title=title.strip(),
                site="ashby",
                strategy="direct_ats",
                company=company_name,
                location=location_str.strip() if location_str else None,
                full_description=desc,
                application_url=app_url,
                posted_at=posted_at,
            )
        )
    return jobs


def fetch_lever_jobs(
    client: httpx.Client,
    slug: str,
    company_name: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[DiscoveredJob]:
    """Fetch jobs from Lever public JSON API."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = client.get(url, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or []
    if not isinstance(payload, list):
        return []

    jobs: list[DiscoveredJob] = []
    for item in payload:
        job_url = item.get("hostedUrl")
        title = item.get("text")
        if not job_url or not title:
            continue

        categories = item.get("categories") or {}
        location_str = categories.get("location")
        workplace_type = item.get("workplaceType")
        if workplace_type and workplace_type.lower() == "remote" and location_str:
            location_str = f"{location_str} (Remote)"
        elif workplace_type and workplace_type.lower() == "remote":
            location_str = "Remote"

        desc_plain = item.get("descriptionPlain")
        desc = desc_plain.strip() if desc_plain else clean_html_text(item.get("description"))
        app_url = item.get("applyUrl") or job_url

        created_at = item.get("createdAt")
        posted_at = None
        if created_at and isinstance(created_at, (int, float)):
            try:
                posted_at = datetime.fromtimestamp(created_at / 1000.0, tz=timezone.utc).isoformat()
            except (ValueError, OSError):
                posted_at = None
        elif created_at:
            posted_at = str(created_at)

        jobs.append(
            DiscoveredJob(
                url=job_url,
                title=title.strip(),
                site="lever",
                strategy="direct_ats",
                company=company_name,
                location=location_str.strip() if location_str else None,
                full_description=desc,
                application_url=app_url,
                posted_at=posted_at,
            )
        )
    return jobs


def fetch_company_jobs(
    client: httpx.Client,
    company_info: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
) -> list[DiscoveredJob]:
    """Route company fetch according to ATS type."""
    ats = str(company_info.get("ats") or "").lower().strip()
    slug = str(company_info.get("slug") or "").strip()
    name = str(company_info.get("name") or slug)

    if not slug:
        return []

    if ats == "greenhouse":
        return fetch_greenhouse_jobs(client, slug, name, timeout=timeout)
    elif ats == "ashby":
        return fetch_ashby_jobs(client, slug, name, timeout=timeout)
    elif ats == "lever":
        return fetch_lever_jobs(client, slug, name, timeout=timeout)
    else:
        log.warning("Unsupported ATS type '%s' for company '%s'", ats, name)
        return []


def discover_direct_ats(
    search_config: dict | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    """Run Direct ATS discovery across configured company portals."""
    cfg = search_config or config.load_search_config()
    adapter_cfg = adapter_config(cfg, "direct_ats")

    max_tier = int(adapter_cfg.get("max_tier", 2))
    max_days_old = int(adapter_cfg.get("max_days_old", DEFAULT_MAX_DAYS_OLD))
    timeout = float(adapter_cfg.get("timeout_seconds", DEFAULT_TIMEOUT))
    max_workers = int(adapter_cfg.get("max_workers", DEFAULT_MAX_WORKERS))
    queries = selected_queries(cfg, max_tier)

    accept_loc = cfg.get("location_accept")
    reject_loc = cfg.get("location_reject_non_remote")

    all_companies = load_companies()
    only_companies = adapter_cfg.get("companies")
    if only_companies and isinstance(only_companies, list):
        target_companies = {
            k: v for k, v in all_companies.items() if k in only_companies
        }
    else:
        target_companies = all_companies

    if not target_companies:
        log.info("No target companies configured for direct_ats discovery")
        return {"status": "ok", "discovered": 0, "new": 0, "duplicates": 0, "errors": 0}

    discovered: list[DiscoveredJob] = []
    errors = 0

    with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_company = {
                executor.submit(fetch_company_jobs, client, comp_data, timeout): comp_key
                for comp_key, comp_data in target_companies.items()
            }
            for future in as_completed(future_to_company):
                comp_key = future_to_company[future]
                try:
                    jobs = future.result()
                    for job in jobs:
                        if is_job_eligible(
                            title=job.title,
                            location_text=job.location,
                            description_text=job.full_description,
                            search_queries=queries,
                            accept_locations=accept_loc,
                            reject_locations=reject_loc,
                            posted_at=job.posted_at,
                            max_days_old=max_days_old,
                        ):
                            discovered.append(job)
                except Exception as exc:
                    log.warning("Error fetching direct ATS jobs for %s: %s", comp_key, exc)
                    errors += 1

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
        "Direct ATS discovery complete: discovered=%d, new=%d, duplicates=%d, errors=%d",
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
