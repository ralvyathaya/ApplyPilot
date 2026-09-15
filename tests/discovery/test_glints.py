import json

from applypilot.database import close_connection, init_db
from applypilot.discovery.glints import (
    build_search_url,
    discover_glints,
    extract_jobs,
    normalize_job,
    parse_next_data,
)

SAMPLE_ITEM = {
    "id": "abc-123",
    "title": "UI/UX Designer",
    "company": {"name": "Example Studio", "industry": "Technology"},
    "city": "Jakarta",
    "country": {"code": "ID"},
    "location": "Jakarta, Indonesia",
    "salaries": [{"minimum": 5000000, "maximum": 7000000, "currency": "IDR"}],
    "skills": [{"name": "Figma"}, {"name": "User Research"}],
    "type": "FULL_TIME",
    "workArrangementOption": "HYBRID",
    "minYearsOfExperience": 0,
    "maxYearsOfExperience": 1,
    "educationLevel": "BACHELOR",
    "createdAt": "2025-09-10",
    "status": "PUBLISHED",
}


def _html(jobs):
    payload = {
        "props": {
            "pageProps": {
                "initialJobs": {"jobsInPage": jobs, "hasMore": False},
            }
        }
    }
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></body></html>"
    )


def test_build_search_url_targets_indonesia_fresh_grads():
    url = build_search_url("UI/UX Designer")

    assert url.startswith("https://glints.com/id/en/opportunities/jobs/explore?")
    assert "country=ID" in url
    assert "yearsOfExperienceRanges=FRESH_GRAD" in url
    assert "keyword=UI%2FUX+Designer" in url or "keyword=UI%2FUX%20Designer" in url


def test_parse_next_data_and_extract_jobs():
    assert extract_jobs(parse_next_data(_html([SAMPLE_ITEM]))) == [SAMPLE_ITEM]
    assert parse_next_data("<html>no data</html>") is None
    assert parse_next_data('<script id="__NEXT_DATA__" type="application/json">{broken</script>') is None
    assert extract_jobs({"props": {"pageProps": {"initialJobs": None}}}) == []


def test_normalize_job_maps_all_fields():
    job = normalize_job(SAMPLE_ITEM)

    assert job is not None
    assert job.url == "https://glints.com/id/en/opportunities/jobs/ui-ux-designer/abc-123"
    assert job.title == "UI/UX Designer"
    assert job.company == "Example Studio"
    assert job.salary == "IDR 5000000-7000000"
    assert job.location == "Jakarta, Indonesia (Full Time, Hybrid)"
    assert job.site == "Glints"
    assert job.strategy == "glints_http"
    assert "Industry: Technology" in job.description
    assert "Skills: Figma, User Research" in job.description
    # No description from search results; enrichment must fetch it.
    assert job.full_description is None


def test_normalize_job_skips_experienced_roles():
    experienced = dict(SAMPLE_ITEM, minYearsOfExperience=3)

    assert normalize_job(experienced) is None
    # The threshold is configurable.
    assert normalize_job(experienced, max_min_experience=3) is not None


def test_normalize_job_skips_foreign_onsite_but_keeps_remote():
    foreign_onsite = dict(SAMPLE_ITEM, country={"code": "SG"}, workArrangementOption="ONSITE")
    foreign_remote = dict(SAMPLE_ITEM, country={"code": "SG"}, workArrangementOption="REMOTE")

    assert normalize_job(foreign_onsite) is None
    assert normalize_job(foreign_remote) is not None


def test_normalize_job_renders_hierarchical_location_dict():
    hierarchical = dict(
        SAMPLE_ITEM,
        location={
            "__typename": "HierarchicalLocation",
            "name": "Serpong",
            "formattedName": "Serpong",
            "level": 4,
            "parents": [
                {"level": 3, "formattedName": "Tangerang Selatan"},
                {"level": 2, "formattedName": "Banten"},
                {"level": 1, "formattedName": "Indonesia"},
            ],
        },
        city=None,
    )
    job = normalize_job(hierarchical)

    assert job is not None
    assert job.location == "Serpong, Tangerang Selatan, Banten (Full Time, Hybrid)"


def test_normalize_job_skips_unusable_items():
    assert normalize_job({"title": "No id"}) is None
    assert normalize_job({"id": "x"}) is None


def test_discover_glints_stores_jobs(tmp_path):
    calls = []

    def http_get(url):
        calls.append(url)
        return _html([SAMPLE_ITEM])

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    result = discover_glints(
        {
            "queries": [{"query": "UI/UX Designer", "tier": 1}],
            "discovery": {"adapter_config": {"glints": {"max_tier": 1, "delay_seconds": 0}}},
        },
        http_get=http_get,
        conn=conn,
        sleep=lambda _: None,
    )

    assert len(calls) == 1
    assert "keyword=UI%2FUX" in calls[0]
    assert result == {
        "status": "ok",
        "found": 1,
        "new": 1,
        "existing": 0,
        "errors": 0,
        "queries": 1,
    }
    row = conn.execute("SELECT title, company, salary, site FROM jobs").fetchone()
    assert tuple(row) == ("UI/UX Designer", "Example Studio", "IDR 5000000-7000000", "Glints")
    close_connection(db_path)


def test_discover_glints_partial_when_payload_missing(tmp_path):
    def http_get(url):
        if "UI" in url:
            return _html([SAMPLE_ITEM])
        return "<html>client-rendered, no SSR payload</html>"

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    result = discover_glints(
        {
            "queries": [
                {"query": "UI/UX Designer", "tier": 1},
                {"query": "Product Analyst", "tier": 1},
            ],
            "discovery": {"adapter_config": {"glints": {"max_tier": 1, "delay_seconds": 0}}},
        },
        http_get=http_get,
        conn=conn,
        sleep=lambda _: None,
    )

    assert result["status"] == "partial"
    assert result["errors"] == 1
    assert result["new"] == 1
    close_connection(db_path)
