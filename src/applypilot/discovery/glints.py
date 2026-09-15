"""Discovery from Glints Indonesia's server-rendered search page.

A plain HTTP GET with a browser user agent returns the first results page
inside a __NEXT_DATA__ script tag, so no browser is needed. Deeper pages are
client-rendered (the SSR payload comes back empty), so only page 1 (~30 jobs
per query) is used.

Search results carry no description; the enrichment stage fetches each detail
page (which exposes JSON-LD).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from urllib.parse import urlencode

import httpx

from applypilot import config
from applypilot.database import init_db, store_discovered_jobs
from applypilot.discovery._util import adapter_config, kebab_slug, selected_queries
from applypilot.discovery.models import DiscoveredJob

log = logging.getLogger(__name__)
BASE_URL = "https://glints.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def build_search_url(query: str) -> str:
    """Build a Glints fresh-graduate search URL for Indonesia."""
    params = urlencode(
        {
            "country": "ID",
            "yearsOfExperienceRanges": "FRESH_GRAD",
            "keyword": query,
        }
    )
    return f"{BASE_URL}/id/en/opportunities/jobs/explore?{params}"


def parse_next_data(html: str) -> dict | None:
    """Extract the __NEXT_DATA__ JSON payload from the search page HTML."""
    match = NEXT_DATA_RE.search(html)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def extract_jobs(payload: dict) -> list[dict]:
    """Pull the job list out of props.pageProps.initialJobs.jobsInPage."""
    page_props = (payload.get("props") or {}).get("pageProps") or {}
    initial = page_props.get("initialJobs")
    if isinstance(initial, dict):
        jobs = initial.get("jobsInPage")
    else:
        jobs = initial
    return jobs if isinstance(jobs, list) else []


def _location_name(value) -> str | None:
    """Render a Glints location, which may be a string or a nested dict.

    HierarchicalLocation dicts carry name/formattedName plus a parents list
    with level 1 (country), 2 (province), and 3 (city) entries.
    """
    if not value:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return str(value)
    name = value.get("formattedName") or value.get("name")
    city = None
    province = None
    for parent in value.get("parents") or []:
        if not isinstance(parent, dict):
            continue
        label = parent.get("formattedName") or parent.get("name")
        level = parent.get("level")
        if level == 3 and city is None:
            city = label
        elif level == 2 and province is None:
            province = label
    parts = [part for part in (name, city, province) if part]
    return ", ".join(dict.fromkeys(parts)) or None


def _format_salary_entry(entry) -> str | None:
    if isinstance(entry, str):
        return entry or None
    if not isinstance(entry, dict):
        return None
    minimum = entry.get("minimum") if entry.get("minimum") is not None else entry.get("min")
    maximum = entry.get("maximum") if entry.get("maximum") is not None else entry.get("max")
    currency = entry.get("currency") or ""
    if minimum is None and maximum is None:
        return None
    if minimum is not None and maximum is not None:
        return f"{currency} {minimum}-{maximum}".strip()
    return f"{currency} {minimum if minimum is not None else maximum}".strip()


def _format_salary(salaries) -> str | None:
    if not salaries:
        return None
    if isinstance(salaries, str):
        return salaries
    if isinstance(salaries, dict):
        return _format_salary_entry(salaries)
    if isinstance(salaries, list):
        entries = [entry for entry in (_format_salary_entry(item) for item in salaries) if entry]
        return "; ".join(entries) or None
    return None


def normalize_job(item: dict, *, max_min_experience: int = 1) -> DiscoveredJob | None:
    """Normalize one Glints SSR job; None when unusable or out of scope.

    Skips postings that clearly require more experience than a fresh graduate
    has, and foreign postings unless they are remote.
    """
    job_id = item.get("id")
    title = item.get("title")
    if not job_id or not title:
        return None

    min_years = item.get("minYearsOfExperience")
    if isinstance(min_years, (int, float)) and min_years > max_min_experience:
        return None

    country = item.get("country")
    if isinstance(country, dict):
        country = country.get("code")
    work_arrangement = item.get("workArrangementOption")
    if (
        country
        and str(country).upper() != "ID"
        and str(work_arrangement or "").upper() != "REMOTE"
    ):
        return None

    company_info = item.get("company") or {}
    company = company_info.get("name") if isinstance(company_info, dict) else None
    industry = company_info.get("industry") if isinstance(company_info, dict) else None

    location = _location_name(item.get("location")) or _location_name(item.get("city"))
    extras = []
    job_type = item.get("type")
    if job_type:
        extras.append(str(job_type).replace("_", " ").title())
    if work_arrangement:
        extras.append(str(work_arrangement).title())
    if extras:
        suffix = ", ".join(extras)
        location = f"{location} ({suffix})" if location else suffix

    skills = item.get("skills") or []
    skill_names = [
        str(skill.get("name") if isinstance(skill, dict) else skill)
        for skill in skills
        if skill
    ]
    summary_bits = []
    if industry:
        summary_bits.append(f"Industry: {industry}")
    if item.get("educationLevel"):
        summary_bits.append(f"Education: {item['educationLevel']}")
    if skill_names:
        summary_bits.append("Skills: " + ", ".join(skill_names))
    description = "; ".join(summary_bits)[:500] or None

    slug = kebab_slug(str(title)) or "job"
    return DiscoveredJob(
        url=f"{BASE_URL}/id/en/opportunities/jobs/{slug}/{job_id}",
        title=str(title),
        company=str(company) if company else None,
        salary=_format_salary(item.get("salaries")),
        description=description,
        location=str(location) if location else None,
        site="Glints",
        strategy="glints_http",
    )


def _discover_with_http(
    search_config: dict,
    http_get: Callable[[str], str],
    conn,
    sleep: Callable[[float], None],
) -> dict:
    settings = adapter_config(search_config, "glints")
    maximum_tier = int(settings.get("max_tier", 2))
    delay = max(0.0, float(settings.get("delay_seconds", 1.5)))
    max_min_experience = int(settings.get("max_min_experience", 1))
    queries = selected_queries(search_config, maximum_tier)

    found = new = existing = errors = 0
    seen: set[str] = set()

    for query_index, query in enumerate(queries):
        try:
            html = http_get(build_search_url(query))
        except Exception as exc:  # noqa: BLE001 - isolate each external query
            errors += 1
            log.warning("Glints query '%s' failed: %s", query, exc)
            continue

        payload = parse_next_data(html)
        if payload is None:
            errors += 1
            log.warning("Glints query '%s' returned no SSR payload", query)
            continue

        batch = []
        for item in extract_jobs(payload):
            job = normalize_job(item, max_min_experience=max_min_experience)
            if job is None or job.url in seen:
                continue
            seen.add(job.url)
            batch.append(job)

        found += len(batch)
        inserted, duplicates = store_discovered_jobs(conn, batch)
        new += inserted
        existing += duplicates

        if delay and query_index < len(queries) - 1:
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


def discover_glints(
    search_config: dict | None = None,
    *,
    http_get: Callable[[str], str] | None = None,
    conn=None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Discover Glints Indonesia fresh-graduate jobs and persist records."""
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
