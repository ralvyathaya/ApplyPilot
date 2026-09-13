"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight role-specific skills heavily, including UX research, user flows, prototyping, Figma, product reasoning, data, programming languages, frameworks, and tools
- Consider transferable experience and strong personal projects for internships, trainee, entry-level, and fresh-graduate roles
- For UI/UX and product design roles, do not penalize a candidate merely for not being a full-stack engineer
- Be realistic about experience level vs. job requirements (years of experience, seniority)

HARD CONSTRAINTS (apply BEFORE skill matching — any violation caps the score at 2):
- Candidate is an Indonesian fresh graduate (~0 years formal experience, internships only), willing to relocate to Jakarta/Bandung/Jabodetabek.
- Seniority: if the role requires 3+ years of experience, or the title says Senior/Lead/Manager/Head/Director/Principal/Staff, score 1-2. Roles asking 0-2 years are acceptable.
- Location: onsite/hybrid roles based outside Jakarta/Jabodetabek/Bandung/West Java (Indonesia) score 1-2. Foreign-based roles score 1-2 unless explicitly remote worldwide AND junior level; remote restricted to a foreign country/timezone/work authorization scores 1-2. When the location field is empty or vague, judge from the title and description.
- Field: finance/accounting/actuarial/audit/banking-operations roles score 1-2 (candidate excludes them). Government/civil-service (PNS/CPNS/kementerian) roles score 1-2.
- Hard requirements the candidate cannot meet (professional licenses, unrelated specific degrees, CPA, etc.) score 1-2.

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


# Batch scoring: free-tier quotas are tiny (Gemini free = 15 RPM and a low
# daily cap), so we score several jobs per LLM call to cut request volume ~10x.
_BATCH_SIZE = max(1, int(os.environ.get("LLM_SCORE_BATCH_SIZE", "8")))
_BATCH_DESC_CHARS = 1200

# Circuit breaker: when this many jobs in a row fail completely (each already
# exhausted the client's internal retries), assume the quota is gone and stop
# early instead of burning hours on guaranteed failures. Remaining jobs stay
# unscored and are retried by the next run.
_MAX_CONSECUTIVE_FAILURES = max(1, int(os.environ.get("LLM_MAX_CONSECUTIVE_FAILURES", "3")))

# Sentinel for batch slots that were never attempted (padding after the
# fallback short-circuits, or entries missing from an otherwise successful
# batch response). Distinct from None ("attempted and failed") so the circuit
# breaker only counts real LLM failures, not parsing gaps.
class _NotAttempted:
    """Sentinel type marking a batch slot that was never scored."""


_NOT_ATTEMPTED = _NotAttempted()

BATCH_SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and several job postings, score how well the candidate fits EACH role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight role-specific skills heavily, including UX research, user flows, prototyping, Figma, product reasoning, data, programming languages, frameworks, and tools
- Consider transferable experience and strong personal projects for internships, trainee, entry-level, and fresh-graduate roles
- For UI/UX and product design roles, do not penalize a candidate merely for not being a full-stack engineer
- Be realistic about experience level vs. job requirements (years of experience, seniority)

HARD CONSTRAINTS (apply BEFORE skill matching — any violation caps the score at 2):
- Candidate is an Indonesian fresh graduate (~0 years formal experience, internships only), willing to relocate to Jakarta/Bandung/Jabodetabek.
- Seniority: if the role requires 3+ years of experience, or the title says Senior/Lead/Manager/Head/Director/Principal/Staff, score 1-2. Roles asking 0-2 years are acceptable.
- Location: onsite/hybrid roles based outside Jakarta/Jabodetabek/Bandung/West Java (Indonesia) score 1-2. Foreign-based roles score 1-2 unless explicitly remote worldwide AND junior level; remote restricted to a foreign country/timezone/work authorization scores 1-2. When the location field is empty or vague, judge from the title and description.
- Field: finance/accounting/actuarial/audit/banking-operations roles score 1-2 (candidate excludes them). Government/civil-service (PNS/CPNS/kementerian) roles score 1-2.
- Hard requirements the candidate cannot meet (professional licenses, unrelated specific degrees, CPA, etc.) score 1-2.

RESPOND WITH ONLY A JSON ARRAY, one object per job, no other text:
[{"id": <job id>, "score": <1-10>, "keywords": "<comma-separated ATS keywords>", "reasoning": "<2-3 sentences>"}]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Tolerates common formatting drift (markdown bold like ``**SCORE:** 8``,
    stray bullets, extra whitespace) by matching labels anywhere at the start
    of a line rather than requiring an exact ``LABEL:`` prefix.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}. score is 0 when no
        usable SCORE label is found; callers treat < 1 as unparseable.
    """
    score = 0
    keywords = ""
    reasoning = response

    match = re.search(r"^\W*SCORE\W*(\d{1,2})", response, re.IGNORECASE | re.MULTILINE)
    if match:
        try:
            score = max(1, min(10, int(match.group(1))))
        except ValueError:
            score = 0

    match = re.search(r"^\W*KEYWORDS\W*(.+)$", response, re.IGNORECASE | re.MULTILINE)
    if match:
        keywords = match.group(1).strip()

    match = re.search(r"^\W*REASONING\W*(.+)$", response, re.IGNORECASE | re.MULTILINE)
    if match:
        reasoning = match.group(1).strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict | None:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}, or None when the
        job could not be scored (LLM error or unparseable response). None
        means "leave unscored", never a real zero.
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job.get('company') or job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
    except Exception as e:
        # Return None (not score=0): a failed call must leave the job unscored
        # so the next run retries it instead of persisting a bogus zero.
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return None
    parsed = _parse_score_response(response)
    if parsed["score"] < 1:
        log.warning("Unparseable score response for job '%s'", job.get("title", "?"))
        return None
    return parsed


def _extract_json_array(text: str) -> list:
    """Pull the first JSON array out of an LLM response, tolerating fences.

    When strict parsing fails (truncation, stray trailing commas, one
    corrupted object), individual parseable objects are salvaged so a single
    malformed entry doesn't discard the whole batch.
    """
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("[")
    if start == -1:
        raise ValueError("No JSON array found in response")
    end = cleaned.rfind("]")
    # A truncated response may have no closing bracket; salvage from the rest.
    body = cleaned[start : end + 1] if end > start else cleaned[start:]

    for candidate in (body, re.sub(r",\s*([}\]])", r"\1", body)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data

    # Salvage: parse each flat object individually and keep the valid ones.
    salvaged = []
    for match in re.finditer(r"\{[^{}]*\}", body):
        try:
            salvaged.append(json.loads(match.group()))
        except json.JSONDecodeError:
            continue
    if not salvaged:
        raise ValueError("Response JSON is not a parseable array")
    log.warning("Salvaged %d entries from malformed batch JSON", len(salvaged))
    return salvaged


def _job_block(index: int, job: dict) -> str:
    description = (job.get("full_description") or "")[:_BATCH_DESC_CHARS]
    return (
        f"=== JOB {index} ===\n"
        f"TITLE: {job.get('title', 'N/A')}\n"
        f"COMPANY: {job.get('company') or job.get('site') or 'N/A'}\n"
        f"LOCATION: {job.get('location') or 'N/A'}\n"
        f"DESCRIPTION:\n{description}"
    )


def score_jobs_batch(resume_text: str, jobs: list[dict]) -> list[dict | None | _NotAttempted]:
    """Score several jobs in a single LLM call.

    Returns one entry per input job: the parsed result dict, None when the
    LLM call itself failed, or _NOT_ATTEMPTED when the call succeeded but
    this job's entry was missing/invalid in the response.
    """
    if not jobs:
        return []
    if len(jobs) == 1:
        return [score_job(resume_text, jobs[0])]

    blocks = "\n\n".join(_job_block(i, job) for i, job in enumerate(jobs))
    messages = [
        {"role": "system", "content": BATCH_SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\n{blocks}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=3072, temperature=0.2)
        entries = _extract_json_array(response)
    except Exception as e:
        log.error("Batch scoring failed (%d jobs): %s", len(jobs), e)
        return [None] * len(jobs)

    by_id: dict[int, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        raw_score = entry.get("score")
        if raw_id is None or raw_score is None:
            continue
        try:
            entry_id = int(raw_id)
            score = max(1, min(10, int(raw_score)))
        except (TypeError, ValueError):
            continue
        by_id[entry_id] = {
            "score": score,
            "keywords": str(entry.get("keywords", "")),
            "reasoning": str(entry.get("reasoning", "")),
        }

    results: list[dict | None | _NotAttempted] = []
    for index, job in enumerate(jobs):
        result = by_id.get(index)
        if result is None:
            # The call itself succeeded, so this is a response gap rather than
            # an LLM failure: mark it not-attempted so the circuit breaker in
            # run_scoring doesn't count it against the quota.
            log.warning(
                "Batch response missing job %d ('%s'); leaving unscored",
                index, job.get("title", "?"),
            )
            results.append(_NOT_ATTEMPTED)
        else:
            results.append(result)
    return results


def run_scoring(
    limit: int = 0,
    rescore: bool = False,
    batch_size: int = _BATCH_SIZE,
    db_path=None,
) -> dict:
    """Score unscored jobs that have full descriptions.

    Jobs are scored in batches to stay within free-tier LLM quotas. Failed
    calls leave jobs unscored (fit_score NULL) so the next run retries them.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).
        batch_size: Jobs per LLM call (1 disables batching).
        db_path: Optional database path override (useful for testing).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection(db_path)

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "aborted": False, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    batch_size = max(1, batch_size)
    log.info("Scoring %d jobs in batches of %d...", len(jobs), batch_size)
    t0 = time.time()
    completed = 0
    errors = 0
    scored = 0
    consecutive_failures = 0
    aborted = False

    for start in range(0, len(jobs), batch_size):
        batch = jobs[start : start + batch_size]
        results = score_jobs_batch(resume_text, batch)

        # Fall back to one-at-a-time only if the whole batch call failed and
        # there was more than one job (a single-job batch already used it).
        if all(r is None for r in results) and len(batch) > 1:
            log.warning("Batch failed; retrying %d jobs individually", len(batch))
            fallback: list[dict | None] = []
            for job in batch:
                single = score_job(resume_text, job)
                fallback.append(single)
                if single is None:
                    # The batch already failed and so did this job: the quota
                    # is almost certainly gone. Don't burn retry budget on the
                    # rest of the batch -- they stay unscored for the next run.
                    break
            results = fallback + [_NOT_ATTEMPTED] * (len(batch) - len(fallback))

        now = datetime.now(timezone.utc).isoformat()
        for job, result in zip(batch, results):
            completed += 1
            if isinstance(result, _NotAttempted):
                # Never sent to the LLM (fallback padding) or missing from an
                # otherwise successful batch response. Leave unscored for the
                # next run without touching the circuit breaker.
                errors += 1
                log.warning(
                    "[%d/%d] UNSCORED (will retry next run)  %s",
                    completed, len(jobs), job.get("title", "?")[:60],
                )
                continue
            if result is None:
                errors += 1
                consecutive_failures += 1
                log.warning(
                    "[%d/%d] UNSCORED (will retry next run)  %s",
                    completed, len(jobs), job.get("title", "?")[:60],
                )
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    aborted = True
                    log.error(
                        "LLM quota appears exhausted (%d consecutive failures). "
                        "Stopping early; unscored jobs will retry on the next run.",
                        consecutive_failures,
                    )
                    break
                continue
            consecutive_failures = 0
            conn.execute(
                "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
                (result["score"], f"{result['keywords']}\n{result['reasoning']}", now, job["url"]),
            )
            scored += 1
            log.info(
                "[%d/%d] score=%d  %s",
                completed, len(jobs), result["score"], job.get("title", "?")[:60],
            )
        conn.commit()

        if aborted:
            break

    elapsed = time.time() - t0
    log.info(
        "Done: %d scored, %d unscored in %.1fs (%.1f jobs/min)%s",
        scored, errors, elapsed, scored / elapsed * 60 if elapsed > 0 else 0,
        " [ABORTED: quota exhausted]" if aborted else "",
    )

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": scored,
        "errors": errors,
        "elapsed": elapsed,
        "aborted": aborted,
        "distribution": distribution,
    }
