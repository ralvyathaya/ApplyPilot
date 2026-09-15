import pandas as pd

from applypilot.database import close_connection, init_db
from applypilot.discovery.jobspy import _clean_str, _location_ok, store_jobspy_results


def _row(**overrides):
    base = {
        "job_url": "https://www.linkedin.com/jobs/view/123",
        "title": "UI/UX Designer",
        "company": "Example Co",
        "location": "Jakarta",
        "description": "d" * 250,
        "site": "linkedin",
        "job_url_direct": None,
        "min_amount": None,
        "max_amount": None,
        "interval": None,
        "currency": None,
    }
    base.update(overrides)
    return base


def test_clean_str_rejects_sentinel_values():
    for bad in (None, "None", "none", "nan", "NaN", "null", "NaT", "", "   "):
        assert _clean_str(bad) is None
    assert _clean_str("None", "") == ""
    assert _clean_str(float("nan")) is None
    assert _clean_str("https://x.com/apply") == "https://x.com/apply"
    assert _clean_str("  padded  ") == "padded"


def test_store_jobspy_results_none_direct_url_stays_null(tmp_path):
    """Regression: job_url_direct=None used to be stored as the string 'None'."""
    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    df = pd.DataFrame([_row()])

    new, existing = store_jobspy_results(conn, df, "linkedin")
    assert (new, existing) == (1, 0)

    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["application_url"] is None
    assert row["title"] == "UI/UX Designer"
    assert row["company"] == "Example Co"
    close_connection(db_path)


def test_store_jobspy_results_keeps_real_direct_url(tmp_path):
    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    df = pd.DataFrame([_row(job_url_direct="https://jobs.example.com/apply/1")])

    store_jobspy_results(conn, df, "indeed")

    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["application_url"] == "https://jobs.example.com/apply/1"
    close_connection(db_path)


ACCEPT_LOCS = ["Jakarta", "Jabodetabek", "Bandung", "West Java", "Indonesia"]
REJECT_LOCS = ["United States", "USA", "Canada", "Spain", "India", "Australia"]


def test_location_ok_rejects_foreign_regions_even_when_remote():
    """Regression: remote-labeled foreign regions used to bypass the reject list."""
    assert not _location_ok("Remote - Spain", ACCEPT_LOCS, REJECT_LOCS)
    assert not _location_ok("Canada (Remote)", ACCEPT_LOCS, REJECT_LOCS)
    assert not _location_ok("United States", ACCEPT_LOCS, REJECT_LOCS)


def test_location_ok_keeps_unlabeled_remote_and_local_matches():
    assert _location_ok("Remote", ACCEPT_LOCS, REJECT_LOCS)
    assert _location_ok("Anywhere", ACCEPT_LOCS, REJECT_LOCS)
    assert _location_ok("Work from home", ACCEPT_LOCS, REJECT_LOCS)
    assert _location_ok(None, ACCEPT_LOCS, REJECT_LOCS)
    assert _location_ok("Jakarta, Indonesia", ACCEPT_LOCS, REJECT_LOCS)
    # "India" must not match inside "Indonesia".
    assert _location_ok("Bandung, West Java, Indonesia", ACCEPT_LOCS, REJECT_LOCS)
    assert not _location_ok("Surabaya, East Java", ACCEPT_LOCS, REJECT_LOCS)
