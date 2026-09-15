import json

from applypilot.database import close_connection, init_db
from applypilot.discovery.jobstreet import (
    build_search_url,
    discover_jobstreet,
    extract_jobs,
    normalize_job,
    parse_redux_data,
)

SAMPLE_ITEM = {
    "id": 987654321,
    "title": "Junior UI/UX Designer",
    "advertiser": {"id": 123, "description": "Example Studio"},
    "companyName": None,
    "locations": ["Jakarta"],
    "salaryLabel": "IDR 5,000,000 - 7,000,000",
    "workTypes": ["Full time"],
    "workArrangements": [{"id": "workFromHome", "label": "Work from home"}],
    "listingDate": "2025-09-10T00:00:00Z",
    "teaser": "Design user flows and prototypes in Figma.",
    "bulletPoints": ["Portfolio required", "Fresh graduates welcome"],
    "classifications": [
        {"description": "Design", "subClassification": "UX & Interaction Design"}
    ],
}


def _payload(items):
    return {"results": {"totalCount": len(items), "results": {"jobs": items}}}


def test_build_search_url_kebab_and_pagination():
    assert build_search_url("UI/UX Designer") == "https://www.jobstreet.co.id/ui-ux-designer-jobs?page=1"
    assert (
        build_search_url("Product Analyst", page=2, location_slug="Jakarta")
        == "https://www.jobstreet.co.id/product-analyst-jobs/in-Jakarta?page=2"
    )


def test_parse_redux_data_extracts_blob_with_trailing_content():
    blob = {"results": {"totalCount": 0, "results": {"jobs": []}}}
    html = (
        "<html><head><script>window.SEEK_REDUX_DATA = "
        + json.dumps(blob)
        + ";</script><script>var other = 1;</script></head><body>hi</body></html>"
    )
    assert parse_redux_data(html) == blob
    assert parse_redux_data("<html>no marker here</html>") is None
    assert parse_redux_data("window.SEEK_REDUX_DATA = broken{") is None


def test_extract_jobs_reads_nested_results():
    assert extract_jobs(_payload([SAMPLE_ITEM])) == [SAMPLE_ITEM]
    assert extract_jobs({}) == []
    assert extract_jobs({"results": {"results": {}}}) == []


def test_normalize_job_maps_all_fields():
    job = normalize_job(SAMPLE_ITEM)

    assert job is not None
    assert job.url == "https://www.jobstreet.co.id/job/987654321"
    assert job.title == "Junior UI/UX Designer"
    assert job.company == "Example Studio"
    assert job.salary == "IDR 5,000,000 - 7,000,000"
    assert job.location == "Jakarta (Full time, Remote)"
    assert job.site == "JobStreet"
    assert job.strategy == "jobstreet_browser"
    assert "Design user flows" in job.description
    assert "Portfolio required" in job.description
    # No full description from search results; enrichment must fetch it.
    assert job.full_description is None


def test_normalize_job_handles_real_seek_shapes():
    item = {
        "id": 555,
        "title": "UI/UX Designer",
        "advertiser": {"id": 9, "description": "Bank Mega"},
        "companyName": None,
        "locations": [
            {
                "countryCode": "ID",
                "label": "South Jakarta, Jakarta",
                "seoHierarchy": [{"contextualName": "South Jakarta Jakarta"}],
            }
        ],
        "workTypes": ["Full time"],
        "workArrangements": {
            "data": [{"id": "2", "label": {"text": "Hybrid"}}],
            "displayText": "Hybrid",
        },
        "teaser": "Design things.",
        "bulletPoints": [],
    }
    job = normalize_job(item)

    assert job is not None
    assert job.company == "Bank Mega"
    assert job.location == "South Jakarta, Jakarta (Full time, Hybrid)"


def test_normalize_job_skips_unusable_items():
    assert normalize_job({"title": "No id"}) is None
    assert normalize_job({"id": 1}) is None
    assert normalize_job({}) is None


def test_discover_jobstreet_paginates_and_dedupes(tmp_path):
    calls = []

    def load_page(url):
        calls.append(url)
        # Full pages (PAGE_SIZE) trigger the next page; repeated items test dedupe.
        return _payload([SAMPLE_ITEM] * 30)

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    result = discover_jobstreet(
        {
            "queries": [{"query": "UI/UX Designer", "tier": 1}],
            "discovery": {
                "adapter_config": {
                    "jobstreet": {
                        "max_tier": 1,
                        "max_pages_per_query": 2,
                        "delay_seconds": 0,
                    }
                }
            },
        },
        page_loader=load_page,
        conn=conn,
        sleep=lambda _: None,
    )

    assert calls == [
        "https://www.jobstreet.co.id/ui-ux-designer-jobs?page=1",
        "https://www.jobstreet.co.id/ui-ux-designer-jobs?page=2",
    ]
    assert result == {
        "status": "ok",
        "found": 1,
        "new": 1,
        "existing": 0,
        "errors": 0,
        "queries": 1,
    }
    row = conn.execute("SELECT title, company, location, full_description FROM jobs").fetchone()
    assert tuple(row)[:3] == ("Junior UI/UX Designer", "Example Studio", "Jakarta (Full time, Remote)")
    assert row[3] is None
    close_connection(db_path)


def test_discover_jobstreet_error_when_all_queries_fail(tmp_path):
    attempts = []

    def load_page(url):
        attempts.append(url)
        raise RuntimeError("JobStreet refused automated access (HTTP 403)")

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    result = discover_jobstreet(
        {
            "queries": [
                {"query": "UI/UX Designer", "tier": 1},
                {"query": "Product Analyst", "tier": 1},
            ],
            "discovery": {
                "adapter_config": {
                    "jobstreet": {"max_tier": 1, "delay_seconds": 0}
                }
            },
        },
        page_loader=load_page,
        conn=conn,
        sleep=lambda _: None,
    )

    assert len(attempts) == 2
    assert result["status"] == "error"
    assert result["errors"] == 2
    close_connection(db_path)
