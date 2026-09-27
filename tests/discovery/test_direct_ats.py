import pytest
from unittest.mock import MagicMock, patch

from applypilot.database import close_connection, init_db
from applypilot.discovery.direct_ats import (
    discover_direct_ats,
    fetch_ashby_jobs,
    fetch_greenhouse_jobs,
    fetch_lever_jobs,
    is_job_eligible,
    load_companies,
)
from applypilot.discovery.models import DiscoveredJob


def test_load_companies_returns_bundled_registry():
    companies = load_companies()
    assert isinstance(companies, dict)
    assert len(companies) > 0
    assert "gitlab" in companies
    assert companies["gitlab"]["ats"] == "greenhouse"
    assert "linear" in companies
    assert companies["linear"]["ats"] == "ashby"
    assert "palantir" in companies
    assert companies["palantir"]["ats"] == "lever"


def test_is_job_eligible_title_and_location():
    # Eligible Indonesian role
    assert is_job_eligible(
        title="Junior UI/UX Designer",
        location_text="Jakarta, Indonesia",
        description_text="Design user interfaces",
    )

    # Eligible Remote / Worldwide role
    assert is_job_eligible(
        title="Product Designer",
        location_text="Remote - Worldwide",
        description_text="Work from anywhere with our team",
    )

    # Ineligible role due to region lock in description
    assert not is_job_eligible(
        title="UX Researcher",
        location_text="Remote",
        description_text="Must be based in the United States. US work authorization required.",
    )

    # Ineligible role due to locked location
    assert not is_job_eligible(
        title="UI Designer",
        location_text="San Francisco, CA, USA",
        description_text="Onsite design work",
        reject_locations=["USA", "United States"],
    )

    # Ineligible role due to irrelevant title
    assert not is_job_eligible(
        title="Senior Heavy Duty Truck Driver",
        location_text="Jakarta",
        description_text="Transportation and logistics",
    )

    # Cutoff test: old job exceeding max_days_old is rejected
    assert not is_job_eligible(
        title="Product Designer",
        location_text="Remote",
        description_text="Design flows",
        posted_at="2020-01-01T00:00:00Z",
        max_days_old=30,
    )


def test_fetch_greenhouse_jobs_parsing():
    sample_gh_payload = {
        "jobs": [
            {
                "id": 12345,
                "title": "Junior Product Designer",
                "absolute_url": "https://boards.greenhouse.io/example/jobs/12345",
                "location": {"name": "Remote, Indonesia"},
                "content": "<p>We are looking for a product designer.</p>",
                "updated_at": "2026-09-20T12:00:00Z",
            }
        ]
    }
    client = MagicMock()
    response = MagicMock()
    response.json.return_value = sample_gh_payload
    client.get.return_value = response

    jobs = fetch_greenhouse_jobs(client, "example", "Example Co")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Junior Product Designer"
    assert job.company == "Example Co"
    assert job.site == "greenhouse"
    assert job.strategy == "direct_ats"
    assert job.location == "Remote, Indonesia"
    assert "product designer" in (job.full_description or "")
    assert job.application_url == "https://boards.greenhouse.io/example/jobs/12345"
    assert job.posted_at == "2026-09-20T12:00:00Z"


def test_fetch_ashby_jobs_parsing():
    sample_ashby_payload = {
        "jobs": [
            {
                "id": "ashby-99",
                "title": "UI Designer",
                "jobUrl": "https://jobs.ashbyhq.com/example/ashby-99",
                "applyUrl": "https://jobs.ashbyhq.com/example/ashby-99/apply",
                "location": "Jakarta",
                "isRemote": True,
                "descriptionPlain": "Full job description text",
                "publishedAt": "2026-09-22T08:00:00Z",
            }
        ]
    }
    client = MagicMock()
    response = MagicMock()
    response.json.return_value = sample_ashby_payload
    client.get.return_value = response

    jobs = fetch_ashby_jobs(client, "example", "Example Co")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "UI Designer"
    assert job.site == "ashby"
    assert job.location == "Jakarta (Remote)"
    assert job.application_url == "https://jobs.ashbyhq.com/example/ashby-99/apply"
    assert job.full_description == "Full job description text"
    assert job.posted_at == "2026-09-22T08:00:00Z"


def test_fetch_lever_jobs_parsing():
    sample_lever_payload = [
        {
            "id": "lever-11",
            "text": "Product Designer, Associate",
            "hostedUrl": "https://jobs.lever.co/example/lever-11",
            "applyUrl": "https://jobs.lever.co/example/lever-11/apply",
            "categories": {"location": "Remote"},
            "workplaceType": "remote",
            "description": "<p>Design systems and components.</p>",
            "createdAt": 1726000000000,
        }
    ]
    client = MagicMock()
    response = MagicMock()
    response.json.return_value = sample_lever_payload
    client.get.return_value = response

    jobs = fetch_lever_jobs(client, "example", "Example Co")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Product Designer, Associate"
    assert job.site == "lever"
    assert job.location == "Remote (Remote)" or "Remote" in (job.location or "")
    assert job.application_url == "https://jobs.lever.co/example/lever-11/apply"
    assert "Design systems" in (job.full_description or "")
    assert job.posted_at is not None


def test_discover_direct_ats_with_mocked_network(tmp_path):
    test_db = tmp_path / "test.db"
    conn = init_db(test_db)

    mock_job = DiscoveredJob(
        url="https://boards.greenhouse.io/mock/jobs/1",
        title="Junior UI/UX Designer",
        site="greenhouse",
        strategy="direct_ats",
        company="Mock Co",
        location="Jakarta, Indonesia",
        full_description="Great junior role",
        application_url="https://boards.greenhouse.io/mock/jobs/1",
    )

    with patch("applypilot.discovery.direct_ats.fetch_company_jobs", return_value=[mock_job]):
        search_config = {
            "discovery": {
                "adapter_config": {
                    "direct_ats": {"companies": ["gitlab"], "max_tier": 1}
                }
            },
            "queries": [{"query": "Junior UI/UX Designer", "tier": 1}],
            "location_accept": ["Jakarta"],
        }
        res = discover_direct_ats(search_config, conn=conn)
        assert res["status"] == "ok"
        assert res["discovered"] == 1
        assert res["new"] == 1
        assert res["duplicates"] == 0

    close_connection(test_db)
