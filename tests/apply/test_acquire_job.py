from applypilot.apply import launcher
from applypilot.database import close_connection, init_db, store_discovered_jobs
from applypilot.discovery.models import DiscoveredJob


def _seed_job(tmp_path, monkeypatch, application_url=None):
    """Create a fresh tailored job (apply_status NULL) and point launcher at it."""
    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    job = DiscoveredJob(
        url="https://www.linkedin.com/jobs/view/42",
        title="User Experience Designer",
        company="Example Co",
        site="linkedin",
        strategy="jobspy",
        application_url=application_url,
    )
    store_discovered_jobs(conn, [job])
    conn.execute(
        "UPDATE jobs SET tailored_resume_path = ?, fit_score = 9 WHERE url = ?",
        ("resume.txt", job.url),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    return conn, job, db_path


def test_acquire_job_url_mode_finds_fresh_job(tmp_path, monkeypatch):
    """Regression: NULL apply_status failed the != 'in_progress' check,
    so --url mode silently matched nothing."""
    conn, job, db_path = _seed_job(tmp_path, monkeypatch)

    acquired = launcher.acquire_job(target_url=job.url)

    assert acquired is not None
    assert acquired["url"] == job.url
    row = conn.execute(
        "SELECT apply_status FROM jobs WHERE url = ?", (job.url,)
    ).fetchone()
    assert row["apply_status"] == "in_progress"
    close_connection(db_path)


def test_acquire_job_url_mode_matches_by_application_url(tmp_path, monkeypatch):
    conn, job, db_path = _seed_job(
        tmp_path, monkeypatch, application_url="https://jobs.example.com/apply/42"
    )

    acquired = launcher.acquire_job(target_url="https://jobs.example.com/apply/42")

    assert acquired is not None
    assert acquired["url"] == job.url
    assert acquired["application_url"] == "https://jobs.example.com/apply/42"
    close_connection(db_path)


def test_acquire_job_sanitizes_none_string_url(tmp_path, monkeypatch):
    """A literal 'None' application_url must never reach the agent prompt."""
    conn, job, db_path = _seed_job(tmp_path, monkeypatch, application_url="None")

    acquired = launcher.acquire_job(target_url=job.url)

    assert acquired is not None
    assert acquired["application_url"] is None
    close_connection(db_path)
