from applypilot.database import close_connection, init_db
from applypilot.discovery.hiringcafe import (
    build_search_url,
    discover_hiringcafe,
    normalize_hit,
)


SAMPLE_HIT = {
    "objectID": "hc-123",
    "requisition_id": "req-123",
    "apply_url": "https://jobs.example.com/apply/123",
    "source": "greenhouse",
    "job_information": {
        "title": "Junior UI/UX Designer",
        "description": "<p>Design user flows and prototypes in Figma.</p>" * 8,
        "company_info": {"name": "Example Studio"},
    },
    "v5_processed_job_data": {
        "core_job_title": "UI/UX Designer",
        "formatted_workplace_location": "Jakarta, Indonesia",
        "workplace_type": "Hybrid",
        "commitment": ["Full Time"],
        "technical_tools": ["Figma"],
        "requirements_summary": "Fresh graduates with a portfolio are welcome.",
    },
}


def test_normalize_hit_uses_direct_ats_url_and_clean_description():
    job = normalize_hit(SAMPLE_HIT)

    assert job is not None
    assert job.url == "https://hiring.cafe/job/req-123"
    assert job.title == "Junior UI/UX Designer"
    assert job.company == "Example Studio"
    assert job.site == "HiringCafe"
    assert job.application_url == "https://jobs.example.com/apply/123"
    assert "<p>" not in job.full_description
    assert "Figma" in job.full_description


def test_build_search_url_contains_indonesia_and_query():
    url = build_search_url("UI/UX Designer", days=7)

    assert url.startswith("https://hiring.cafe/?searchState=")
    assert "page=0" in url
    assert "UI%2FUX" in url
    assert "Indonesia" in url


def test_discover_hiringcafe_paginates_and_stores_jobs(tmp_path):
    calls = []

    def load_page(url):
        calls.append(url)
        return {
            "ssrHits": [SAMPLE_HIT],
            "ssrIsLastPage": True,
            "ssrPage": 0,
        }

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    result = discover_hiringcafe(
        {
            "queries": [{"query": "UI/UX Designer", "tier": 1}],
            "discovery": {
                "adapter_config": {
                    "hiringcafe": {
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

    assert len(calls) == 1
    assert result == {
        "status": "ok",
        "found": 1,
        "new": 1,
        "existing": 0,
        "errors": 0,
        "queries": 1,
    }
    row = conn.execute("SELECT company, application_url FROM jobs").fetchone()
    assert tuple(row) == ("Example Studio", "https://jobs.example.com/apply/123")
    close_connection(db_path)
