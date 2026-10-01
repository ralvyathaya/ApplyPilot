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
    close_connection(db_path)


def test_mark_all_jobs_expired_and_reactivate(tmp_path):
    from applypilot.database import get_jobs_by_stage, get_stats, mark_all_jobs_expired

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)

    job = DiscoveredJob(
        url="https://example.com/job/1",
        title="Software Engineer",
        company="Startup Inc",
        location="Jakarta",
        site="linkedin",
        strategy="direct",
        full_description="Backend developer needed",
        application_url="https://example.com/apply",
    )
    store_discovered_jobs(conn, [job])

    # Initially 1 pending score
    assert len(get_jobs_by_stage(conn, "pending_score")) == 1
    stats_before = get_stats(conn)
    assert stats_before["unscored"] == 1
    assert stats_before["expired"] == 0

    # Mark expired
    count = mark_all_jobs_expired(conn)
    assert count == 1
    stats_after = get_stats(conn)
    assert stats_after["unscored"] == 0
    assert stats_after["expired"] == 1
    assert len(get_jobs_by_stage(conn, "pending_score")) == 0

    # Re-discovering the same job reactivates it
    new_count, dup_count = store_discovered_jobs(conn, [job])
    assert new_count == 1
    assert dup_count == 0

    stats_reactivated = get_stats(conn)
    assert stats_reactivated["unscored"] == 1
    assert stats_reactivated["expired"] == 0
    assert len(get_jobs_by_stage(conn, "pending_score")) == 1

    close_connection(db_path)
