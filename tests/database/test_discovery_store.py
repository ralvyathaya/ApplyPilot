from applypilot.database import close_connection, init_db, store_discovered_jobs
from applypilot.discovery.models import DiscoveredJob


def test_store_discovered_jobs_preserves_direct_apply_data(tmp_path):
    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    job = DiscoveredJob(
        url="https://hiring.cafe/job/abc",
        title="Junior Product Designer",
        company="Example Co",
        location="Jakarta, Indonesia",
        site="HiringCafe",
        strategy="hiringcafe_browser",
        full_description="A" * 250,
        application_url="https://jobs.example.com/apply/abc",
    )

    assert store_discovered_jobs(conn, [job]) == (1, 0)
    assert store_discovered_jobs(conn, [job]) == (0, 1)

    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (job.url,)).fetchone()
    assert row["company"] == "Example Co"
    assert row["site"] == "HiringCafe"
    assert row["full_description"] == "A" * 250
    assert row["application_url"] == "https://jobs.example.com/apply/abc"
    assert row["detail_scraped_at"] is not None
    close_connection(db_path)
