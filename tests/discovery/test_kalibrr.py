import json
import pytest
from unittest.mock import MagicMock, patch

from applypilot.database import close_connection, init_db
from applypilot.discovery.kalibrr import (
    build_search_url,
    discover_kalibrr,
    extract_jobs_from_html,
    format_location,
    format_salary,
    is_kalibrr_job_eligible,
    normalize_kalibrr_job,
)
from applypilot.discovery.models import DiscoveredJob


def test_build_search_url():
    url = build_search_url("Junior UI/UX Designer", page=2)
    assert url == "https://www.kalibrr.com/id-ID/job-board/te/junior-ui-ux-designer/2"


def test_extract_jobs_from_html():
    raw_payload = {
        "props": {
            "pageProps": {
                "count": 42,
                "jobs": [{"id": 1, "name": "UI Designer"}],
            }
        }
    }
    html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(raw_payload)}</script></body></html>'
    jobs, count = extract_jobs_from_html(html)
    assert count == 42
    assert len(jobs) == 1
    assert jobs[0]["name"] == "UI Designer"

    # Malformed HTML returns empty
    jobs, count = extract_jobs_from_html("<html>No data</html>")
    assert jobs == []
    assert count == 0


def test_format_salary():
    assert format_salary({"salaryShown": False, "baseSalary": 5000000}) is None
    assert format_salary({"salaryShown": True, "baseSalary": 5000000, "maximumSalary": 7000000, "salaryCurrency": "IDR"}) == "IDR 5,000,000 - 7,000,000"
    assert format_salary({"salaryShown": True, "baseSalary": 6000000, "maximumSalary": None, "salaryCurrency": "IDR"}) == "IDR 6,000,000"


def test_format_location():
    item_wfh = {
        "isWorkFromHome": True,
        "googleLocation": {"addressComponents": {"city": "Jakarta"}},
    }
    assert format_location(item_wfh) == "Jakarta (Remote / WFH)"

    item_onsite = {
        "isWorkFromHome": False,
        "googleLocation": {"addressComponents": {"city": "Bandung"}},
    }
    assert format_location(item_onsite) == "Bandung"


def test_normalize_kalibrr_job():
    raw_item = {
        "id": 12345,
        "name": "Junior UX Designer",
        "slug": "junior-ux-designer-1",
        "companyInfo": {"name": "PT Tech Nusantara", "code": "pt-tech-nusantara"},
        "description": "<p>Build great UX flows.</p>",
        "qualifications": "<p>Figma knowledge.</p>",
        "isWorkFromHome": True,
        "salaryShown": True,
        "baseSalary": 6000000,
        "maximumSalary": 8000000,
        "salaryCurrency": "IDR",
        "googleLocation": {"addressComponents": {"city": "Jakarta"}},
    }
    job = normalize_kalibrr_job(raw_item)
    assert job is not None
    assert job.title == "Junior UX Designer"
    assert job.company == "PT Tech Nusantara"
    assert job.site == "kalibrr"
    assert job.strategy == "kalibrr_api"
    assert job.url == "https://www.kalibrr.com/c/pt-tech-nusantara/jobs/12345/junior-ux-designer-1"
    assert "Figma knowledge" in (job.full_description or "")
    assert job.salary == "IDR 6,000,000 - 8,000,000"
    assert job.location == "Jakarta (Remote / WFH)"


def test_is_kalibrr_job_eligible():
    raw_item_id = {
        "isWorkFromHome": False,
        "googleLocation": {"addressComponents": {"city": "Jakarta"}},
    }
    job_id = DiscoveredJob(
        url="https://kalibrr.com/1",
        title="UI/UX Designer",
        site="kalibrr",
        strategy="kalibrr_api",
        location="Jakarta",
        posted_at="2026-09-26T00:00:00Z",
    )
    assert is_kalibrr_job_eligible(job_id, raw_item_id, accept_locations=["Jakarta"], max_days_old=7)

    # Cutoff test: posting older than 7 days is rejected
    job_old = DiscoveredJob(
        url="https://kalibrr.com/old",
        title="UI/UX Designer",
        site="kalibrr",
        strategy="kalibrr_api",
        location="Jakarta",
        posted_at="2020-01-01T00:00:00Z",
    )
    assert not is_kalibrr_job_eligible(job_old, raw_item_id, max_days_old=7)

    # Unrelated title rejected
    job_truck = DiscoveredJob(
        url="https://kalibrr.com/2",
        title="Forklift Operator",
        site="kalibrr",
        strategy="kalibrr_api",
        location="Jakarta",
    )
    assert not is_kalibrr_job_eligible(job_truck, raw_item_id)


def test_discover_kalibrr_mocked_network(tmp_path):
    test_db = tmp_path / "test_kalibrr.db"
    conn = init_db(test_db)

    sample_job = {
        "id": 999,
        "name": "Junior UI Designer",
        "slug": "junior-ui-designer",
        "companyInfo": {"name": "Test Studio", "code": "test-studio"},
        "description": "<p>Design systems</p>",
        "isWorkFromHome": True,
        "googleLocation": {"addressComponents": {"city": "Bandung"}},
    }
    raw_html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{{"props": {{"pageProps": {{"count": 1, "jobs": [{json.dumps(sample_job)}]}}}}}}</script></body></html>'

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = raw_html

    with patch("httpx.Client.get", return_value=mock_resp):
        search_config = {
            "discovery": {
                "adapter_config": {
                    "kalibrr": {"max_tier": 1, "max_pages_per_query": 1, "delay_seconds": 0}
                }
            },
            "queries": [{"query": "Junior UI Designer", "tier": 1}],
            "location_accept": ["Bandung"],
        }
        res = discover_kalibrr(search_config, conn=conn)
        assert res["status"] == "ok"
        assert res["discovered"] == 1
        assert res["new"] == 1

    close_connection(test_db)
