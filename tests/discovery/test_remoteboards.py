import json

from applypilot.database import close_connection, init_db
from applypilot.discovery.remoteboards import (
    build_remotive_url,
    discover_remoteboards,
    location_is_worldwide,
    normalize_remoteok_job,
    normalize_remotive_job,
    normalize_wwr_job,
    parse_wwr_feed,
    title_is_relevant,
)

REMOTIVE_JOB = {
    "id": 111,
    "url": "https://remotive.com/remote-jobs/design/junior-product-designer-111",
    "title": "Junior Product Designer",
    "company_name": "Example Co",
    "category": "Design",
    "tags": ["design", "figma"],
    "job_type": "full_time",
    "publication_date": "2025-09-10T08:00:00",
    "candidate_required_location": "Worldwide",
    "salary": "$40k - $60k",
    "description": "<p>Design beautiful product experiences.</p>",
}

REMOTEOK_JOB = {
    "id": 222,
    "date": "2025-09-10T08:00:00+00:00",
    "company": "Example Labs",
    "position": "UX Researcher",
    "tags": ["research", "ux"],
    "url": "https://remoteok.com/remote-jobs/remote-ux-researcher-222",
    "apply_url": "https://remoteok.com/remote-jobs/remote-ux-researcher-222",
    "description": "<b>Run user interviews</b> and usability tests.",
    "salary_min": 50000,
    "salary_max": 70000,
    "salary_currency": "USD",
    "location": "Worldwide",
    "slug": "remote-ux-researcher-222",
}

REMOTEOK_LEGAL = "Access to the RemoteOK API is granted only for personal, non-commercial use."

WWR_DESIGN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Example Studio: UI/UX Designer</title>
      <link>https://weworkremotely.com/remote-jobs/example-studio-ui-ux-designer</link>
      <region>Anywhere in the World</region>
      <type>Full-Time</type>
      <category>Design</category>
      <description>&lt;p&gt;Join our design team and ship delightful UX.&lt;/p&gt;</description>
      <pubDate>Wed, 10 Sep 2025 08:00:00 +0000</pubDate>
    </item>
    <item>
      <title>USA Co: Senior Backend Engineer</title>
      <link>https://weworkremotely.com/remote-jobs/usa-co-senior-backend</link>
      <region>USA Only</region>
      <type>Full-Time</type>
      <description>Backend work</description>
      <pubDate>Wed, 10 Sep 2025 08:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""


def test_location_is_worldwide():
    assert location_is_worldwide("Worldwide")
    assert location_is_worldwide("Worldwide, USA, Europe")
    assert location_is_worldwide("Anywhere in the World")
    assert location_is_worldwide("Indonesia")
    assert location_is_worldwide(None)
    assert not location_is_worldwide("USA Only")
    assert not location_is_worldwide("APAC")


def test_title_is_relevant_is_word_based():
    assert title_is_relevant("UI/UX Designer")
    assert title_is_relevant("Product Analyst")
    assert title_is_relevant("Junior Data Engineer")
    assert title_is_relevant("Marketing Intern")
    assert title_is_relevant("Motion Graphic Artist", tags=["design"])
    # Short tokens must not match inside unrelated words.
    assert not title_is_relevant("Build Engineer")
    assert not title_is_relevant("Full Stack Engineer")
    assert not title_is_relevant("Guided Tour Coordinator")


def test_build_remotive_url():
    assert (
        build_remotive_url("UI/UX Designer", 50)
        == "https://remotive.com/api/remote-jobs?search=UI%2FUX+Designer&limit=50"
    )


def test_normalize_remotive_job():
    job = normalize_remotive_job(REMOTIVE_JOB)

    assert job is not None
    assert job.site == "Remotive"
    assert job.strategy == "remoteboards_http"
    assert job.company == "Example Co"
    assert job.salary == "$40k - $60k"
    assert job.full_description == "Design beautiful product experiences."
    assert job.location == "Worldwide"


def test_normalize_remotive_job_filters():
    region_locked = dict(REMOTIVE_JOB, candidate_required_location="USA Only")
    irrelevant = dict(REMOTIVE_JOB, title="Full Stack Engineer", tags=["go", "kubernetes"])

    assert normalize_remotive_job(region_locked) is None
    assert normalize_remotive_job(irrelevant) is None
    assert normalize_remotive_job({"title": "No url"}) is None


def test_normalize_remoteok_job_skips_legal_notice():
    assert normalize_remoteok_job(REMOTEOK_LEGAL) is None
    assert normalize_remoteok_job({"company": "No position"}) is None


def test_normalize_remoteok_job():
    job = normalize_remoteok_job(REMOTEOK_JOB)

    assert job is not None
    assert job.site == "RemoteOK"
    assert job.title == "UX Researcher"
    assert job.salary == "USD 50000-70000"
    assert job.full_description == "Run user interviews\nand usability tests."
    # apply_url identical to the listing URL adds nothing.
    assert job.application_url is None

    external = dict(REMOTEOK_JOB, apply_url="https://boards.example.com/apply/222")
    assert normalize_remoteok_job(external).application_url == (
        "https://boards.example.com/apply/222"
    )


def test_normalize_remoteok_job_filters():
    assert normalize_remoteok_job(dict(REMOTEOK_JOB, location="USA")) is None
    assert normalize_remoteok_job(dict(REMOTEOK_JOB, position="Rust Engineer", tags=[])) is None


def test_parse_wwr_feed():
    items = parse_wwr_feed(WWR_DESIGN_XML)

    assert len(items) == 2
    assert items[0]["title"] == "Example Studio: UI/UX Designer"
    assert items[0]["region"] == "Anywhere in the World"
    assert parse_wwr_feed("<not xml") == []


def test_normalize_wwr_job():
    items = parse_wwr_feed(WWR_DESIGN_XML)
    job = normalize_wwr_job(items[0])

    assert job is not None
    assert job.site == "WeWorkRemotely"
    assert job.strategy == "remoteboards_rss"
    assert job.title == "UI/UX Designer"
    assert job.company == "Example Studio"
    assert job.location == "Anywhere in the World - Full-Time"
    # Short RSS descriptions are teasers; enrichment must fetch the page.
    assert job.full_description is None
    assert "ship delightful UX" in job.description


def test_normalize_wwr_job_keeps_long_description_as_full():
    long_description = "<p>" + ("Requirement text. " * 40) + "</p>"
    xml = (
        "<rss><channel><item>"
        "<title>Example Studio: Product Designer</title>"
        "<link>https://weworkremotely.com/remote-jobs/x</link>"
        "<region>Anywhere in the World</region>"
        f"<description>{long_description.replace('<', '&lt;').replace('>', '&gt;')}</description>"
        "</item></channel></rss>"
    )
    job = normalize_wwr_job(parse_wwr_feed(xml)[0])

    assert job is not None
    assert job.full_description is not None
    assert len(job.full_description) >= 400


def test_normalize_wwr_job_filters():
    items = parse_wwr_feed(WWR_DESIGN_XML)

    # Region-locked and irrelevant title.
    assert normalize_wwr_job(items[1]) is None
    assert normalize_wwr_job({"title": "No link", "link": ""}) is None
    worldwide_engineer = dict(
        items[0],
        title="Example Co: Senior Backend Engineer",
    )
    assert normalize_wwr_job(worldwide_engineer) is None


def test_discover_remoteboards_stores_all_boards(tmp_path):
    calls = []

    def http_get(url):
        calls.append(url)
        if url.startswith("https://remotive.com/api"):
            return json.dumps({"job-count": 1, "jobs": [REMOTIVE_JOB]})
        if url.startswith("https://remoteok.com/api"):
            return json.dumps([REMOTEOK_LEGAL, REMOTEOK_JOB])
        if "remote-design-jobs" in url:
            return WWR_DESIGN_XML
        if "remote-product-jobs" in url:
            return "<rss><channel></channel></rss>"
        raise AssertionError(f"unexpected URL {url}")

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    result = discover_remoteboards(
        {
            "queries": [{"query": "UI/UX Designer", "tier": 1}],
            "discovery": {
                "adapter_config": {
                    "remoteboards": {"max_tier": 1, "days_back": 0, "delay_seconds": 0}
                }
            },
        },
        http_get=http_get,
        conn=conn,
        sleep=lambda _: None,
    )

    assert result["status"] == "ok"
    assert result == {
        "status": "ok",
        "found": 3,
        "new": 3,
        "existing": 0,
        "errors": 0,
        "boards": {
            "remotive": {"status": "ok", "errors": 0},
            "remoteok": {"status": "ok", "errors": 0},
            "weworkremotely": {"status": "ok", "errors": 0},
        },
    }
    sites = {row[0] for row in conn.execute("SELECT site FROM jobs")}
    assert sites == {"Remotive", "RemoteOK", "WeWorkRemotely"}
    # Remotive/RemoteOK include full descriptions and are marked enriched;
    # the WWR teaser is not.
    enriched = {
        row[0] for row in conn.execute("SELECT site FROM jobs WHERE full_description IS NOT NULL")
    }
    assert enriched == {"Remotive", "RemoteOK"}
    assert "remote-design-jobs" in "".join(calls)
    close_connection(db_path)


def test_discover_remoteboards_isolates_board_failures(tmp_path):
    def http_get(url):
        if url.startswith("https://remotive.com/api"):
            return json.dumps({"job-count": 1, "jobs": [REMOTIVE_JOB]})
        if url.startswith("https://remoteok.com/api"):
            raise RuntimeError("HTTP 503")
        if "remote-design-jobs" in url:
            return WWR_DESIGN_XML
        raise RuntimeError("HTTP 503")

    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    result = discover_remoteboards(
        {
            "queries": [{"query": "UI/UX Designer", "tier": 1}],
            "discovery": {
                "adapter_config": {
                    "remoteboards": {"max_tier": 1, "days_back": 0, "delay_seconds": 0}
                }
            },
        },
        http_get=http_get,
        conn=conn,
        sleep=lambda _: None,
    )

    assert result["status"] == "partial"
    assert result["boards"]["remotive"] == {"status": "ok", "errors": 0}
    assert result["boards"]["remoteok"] == {"status": "error", "errors": 1}
    assert result["boards"]["weworkremotely"] == {"status": "partial", "errors": 1}
    assert result["new"] == 2  # remotive + wwr design feed
    close_connection(db_path)
