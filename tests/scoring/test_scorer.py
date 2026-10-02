"""Scoring behavior tests: batch scoring, failure handling, no bogus zeros,
lenient response parsing, and JSON salvage."""

import json

from applypilot.database import close_connection, init_db
from applypilot.scoring import scorer


RESUME = "UI/UX designer. Figma, prototyping, user research."


def _make_job(conn, url, title="Junior UI/UX Designer", company="Studio", score=None):
    conn.execute(
        "INSERT INTO jobs (url, title, company, site, full_description, fit_score) "
        "VALUES (?, ?, ?, 'linkedin', 'Design apps in Figma.', ?)",
        (url, title, company, score),
    )
    conn.commit()


class FakeClient:
    """Records calls; returns canned responses or raises."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _batch_json(pairs):
    return json.dumps(
        [
            {"id": i, "score": score, "keywords": "figma", "reasoning": "good match"}
            for i, score in pairs
        ]
    )


def test_score_job_returns_none_on_llm_error(monkeypatch):
    client = FakeClient([RuntimeError("HTTP 429")])
    monkeypatch.setattr(scorer, "get_client", lambda: client)

    result = scorer.score_job(RESUME, {"title": "Designer", "site": "linkedin"})

    assert result is None


def test_score_jobs_batch_parses_all_entries(monkeypatch):
    client = FakeClient([_batch_json([(0, 8), (1, 6)])])
    monkeypatch.setattr(scorer, "get_client", lambda: client)

    jobs = [
        {"title": "UI/UX Designer", "site": "linkedin"},
        {"title": "Product Analyst", "site": "indeed"},
    ]
    results = scorer.score_jobs_batch(RESUME, jobs)

    assert len(client.calls) == 1  # one LLM call for two jobs
    assert results[0] is not None and results[1] is not None
    assert results[0]["score"] == 8
    assert results[1]["score"] == 6


def test_extract_json_array_tolerates_code_fences():
    text = "Here you go:\n```json\n[{\"id\": 0, \"score\": 9}]\n```\nDone."
    assert scorer._extract_json_array(text) == [{"id": 0, "score": 9}]


def test_parse_score_response_lenient_markdown():
    """Models often bold or bullet the labels; parsing must not require the
    exact 'LABEL:' prefix."""
    parsed = scorer._parse_score_response(
        "**SCORE:** 8\n**KEYWORDS:** figma, prototyping\n**REASONING:** good"
    )
    assert parsed["score"] == 8
    assert parsed["keywords"] == "figma, prototyping"
    assert parsed["reasoning"] == "good"


def test_parse_score_response_unparseable_returns_zero():
    parsed = scorer._parse_score_response("I cannot evaluate this job posting.")
    assert parsed["score"] == 0


def test_extract_json_array_strips_trailing_commas():
    text = '[{"id": 0, "score": 8,}, {"id": 1, "score": 7,},]'
    entries = scorer._extract_json_array(text)
    assert [e["id"] for e in entries] == [0, 1]


def test_extract_json_array_salvages_valid_objects():
    """One corrupted entry (unescaped quote) must not nuke the whole batch."""
    text = (
        '[{"id": 0, "score": 8, "keywords": "figma", "reasoning": "good"},'
        ' {"id": 1, "score": 6, "keywords": "sql", "reasoning": "said "hi" ok"},'
        ' {"id": 2, "score": 9, "keywords": "ux", "reasoning": "great"}]'
    )
    entries = scorer._extract_json_array(text)
    ids = [e["id"] for e in entries]
    assert 0 in ids and 2 in ids
    assert 1 not in ids


def test_extract_json_array_salvages_truncated_response():
    text = (
        '[{"id": 0, "score": 8, "keywords": "figma", "reasoning": "good"}, '
        '{"id": 1, "score": 6, "keyw'
    )
    entries = scorer._extract_json_array(text)
    assert entries == [
        {"id": 0, "score": 8, "keywords": "figma", "reasoning": "good"}
    ]


def test_run_scoring_persists_scores_and_skips_failures(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    _make_job(conn, "https://example.com/1", score=None)
    _make_job(conn, "https://example.com/2", title="Data Analyst", score=None)
    monkeypatch.setattr(scorer, "RESUME_PATH", tmp_path / "resume.txt")
    (tmp_path / "resume.txt").write_text(RESUME, encoding="utf-8")

    # First call: batch fails. Then individual fallback: job 1 ok, job 2 errors.
    client = FakeClient(
        [
            RuntimeError("HTTP 429"),          # batch attempt
            "SCORE: 8\nKEYWORDS: figma\nREASONING: strong",  # fallback job 1
            RuntimeError("HTTP 429"),          # fallback job 2
        ]
    )
    monkeypatch.setattr(scorer, "get_client", lambda: client)

    result = scorer.run_scoring(batch_size=2, db_path=db_path)

    assert result["scored"] == 1
    assert result["errors"] == 1

    row1 = conn.execute(
        "SELECT fit_score FROM jobs WHERE url='https://example.com/1'"
    ).fetchone()
    row2 = conn.execute(
        "SELECT fit_score, score_reasoning FROM jobs WHERE url='https://example.com/2'"
    ).fetchone()

    # Successful job persisted...
    assert row1[0] == 8
    # ...failed job left NULL (NOT a bogus zero) so it retries next run.
    assert row2[0] is None
    assert row2[1] is None

    close_connection(db_path)


def test_run_scoring_batch_success_single_call(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    _make_job(conn, "https://example.com/a", score=None)
    _make_job(conn, "https://example.com/b", title="UX Researcher", score=None)
    monkeypatch.setattr(scorer, "RESUME_PATH", tmp_path / "resume.txt")
    (tmp_path / "resume.txt").write_text(RESUME, encoding="utf-8")

    client = FakeClient([_batch_json([(0, 7), (1, 9)])])
    monkeypatch.setattr(scorer, "get_client", lambda: client)

    result = scorer.run_scoring(batch_size=8, db_path=db_path)

    assert result["scored"] == 2
    assert result["errors"] == 0
    assert len(client.calls) == 1  # two jobs scored with ONE LLM request

    scores = dict(
        conn.execute("SELECT url, fit_score FROM jobs").fetchall()
    )
    assert scores["https://example.com/a"] == 7
    assert scores["https://example.com/b"] == 9

    close_connection(db_path)


def test_run_scoring_aborts_on_consecutive_failures(tmp_path, monkeypatch):
    """Circuit breaker: when the LLM keeps failing (quota exhausted), the run
    aborts early instead of grinding through every job, and nothing bogus is
    persisted -- all jobs stay NULL for the next run to retry.

    Un-attempted padding jobs must NOT feed the breaker: only real per-batch
    failures do, so with batch_size=2 each cycle burns exactly one failure
    and the run trips after 3 cycles (6 LLM calls)."""
    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    urls = [f"https://example.com/{i}" for i in range(1, 13)]
    for url in urls:
        _make_job(conn, url, score=None)
    monkeypatch.setattr(scorer, "RESUME_PATH", tmp_path / "resume.txt")
    (tmp_path / "resume.txt").write_text(RESUME, encoding="utf-8")
    monkeypatch.setattr(scorer, "_MAX_CONSECUTIVE_FAILURES", 3)

    client = FakeClient([RuntimeError("HTTP 429")] * 12)
    monkeypatch.setattr(scorer, "get_client", lambda: client)

    result = scorer.run_scoring(batch_size=2, db_path=db_path)

    assert result["aborted"] is True
    assert result["scored"] == 0
    # Per cycle: 1 batch call + 1 fallback call (fallback breaks on the first
    # failure; the padded job is _NOT_ATTEMPTED and doesn't feed the breaker).
    # Breaker trips on the 3rd cycle => 3 * 2 = 6 calls.
    assert len(client.calls) == 6

    rows = conn.execute(
        "SELECT fit_score, score_reasoning FROM jobs"
    ).fetchall()
    assert len(rows) == 12
    assert all(row[0] is None for row in rows)
    assert all(row[1] is None for row in rows)

    close_connection(db_path)


def test_run_scoring_missing_batch_entry_does_not_abort(tmp_path, monkeypatch):
    """A response gap (batch call succeeded but one job's entry is missing)
    is a parsing artifact, not quota death: the job stays unscored, the
    breaker is untouched, and the run completes without aborting or
    retry-storming."""
    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    urls = [f"https://example.com/{i}" for i in range(1, 7)]
    for url in urls:
        _make_job(conn, url, score=None)
    monkeypatch.setattr(scorer, "RESUME_PATH", tmp_path / "resume.txt")
    (tmp_path / "resume.txt").write_text(RESUME, encoding="utf-8")
    monkeypatch.setattr(scorer, "_MAX_CONSECUTIVE_FAILURES", 3)

    # Every batch response omits job index 1.
    responses = [
        json.dumps([{"id": 0, "score": 8, "keywords": "figma", "reasoning": "ok"}])
        for _ in range(3)
    ]
    client = FakeClient(responses)
    monkeypatch.setattr(scorer, "get_client", lambda: client)

    result = scorer.run_scoring(batch_size=2, db_path=db_path)

    assert result["aborted"] is False
    assert result["scored"] == 3
    assert result["errors"] == 3
    assert len(client.calls) == 3  # no individual-fallback storm

    scores = dict(conn.execute("SELECT url, fit_score FROM jobs").fetchall())
    assert scores["https://example.com/1"] == 8
    assert scores["https://example.com/2"] is None
    assert scores["https://example.com/3"] == 8
    assert scores["https://example.com/4"] is None

    close_connection(db_path)


def test_default_batch_size_is_one():
    assert scorer._BATCH_SIZE == 1


def test_parse_score_response_numbered_lines():
    text = (
        "1. SCORE: 9\n"
        "2. KEYWORDS: Figma, UI/UX, Design\n"
        "3. REASONING: Strong match for fresh graduate."
    )
    parsed = scorer._parse_score_response(text)
    assert parsed["score"] == 9
    assert "Figma" in parsed["keywords"]
    assert "Strong match" in parsed["reasoning"]


def test_parse_score_response_fit_score_variant():
    text = (
        "Fit Score: 8/10\n"
        "Keywords: React, TypeScript\n"
        "Reasoning: Candidate has good skills."
    )
    parsed = scorer._parse_score_response(text)
    assert parsed["score"] == 8
    assert "React" in parsed["keywords"]
    assert "Candidate has good skills." in parsed["reasoning"]


def test_parse_score_response_json_format():
    text = json.dumps({
        "score": 7,
        "keywords": "Python, SQL",
        "reasoning": "Moderate fit for data role."
    })
    parsed = scorer._parse_score_response(text)
    assert parsed["score"] == 7
    assert "Python" in parsed["keywords"]
    assert "Moderate fit" in parsed["reasoning"]


def test_parse_score_response_multiline_reasoning():
    text = (
        "SCORE: 8\n"
        "KEYWORDS: Figma\n"
        "REASONING: The candidate has strong foundations in Figma.\n"
        "Furthermore, internship experience is highly relevant.\n"
        "Overall a solid hire."
    )
    parsed = scorer._parse_score_response(text)
    assert parsed["score"] == 8
    assert "internship experience is highly relevant" in parsed["reasoning"]
    assert "Overall a solid hire." in parsed["reasoning"]

