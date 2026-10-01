"""ApplyPilot Export Module — export jobs and application status to Excel and CSV.

Generates beautifully formatted, styled spreadsheets with:
  - Jobs sheet: Fit scores, freshness badges, application status, clickable links,
    auto-filters, freeze panes, score-based color coding.
  - Summary sheet: Pipeline metrics, score distribution, and site breakdown.
"""

from __future__ import annotations

import csv
import datetime
import logging
import os
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from applypilot.config import APP_DIR
from applypilot.database import get_connection, get_stats

log = logging.getLogger(__name__)


def _parse_date_safe(val: str | None) -> datetime.datetime | None:
    """Parse various timestamp/date string formats into a timezone-aware datetime."""
    if not val:
        return None
    val = val.strip()
    try:
        dt = datetime.datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        pass

    try:
        return parsedate_to_datetime(val)
    except Exception:
        pass

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(val, fmt).replace(tzinfo=datetime.timezone.utc)
        except Exception:
            pass

    return None


def _format_date(val: str | None) -> str:
    """Format a date/timestamp string into YYYY-MM-DD (or keep raw if unparseable)."""
    if not val:
        return ""
    dt = _parse_date_safe(val)
    if dt:
        return dt.strftime("%Y-%m-%d")
    return val[:10] if len(val) >= 10 else val


def _calculate_freshness(posted_val: str | None, ref_time: datetime.datetime | None = None) -> str:
    """Calculate freshness badge based on posted_at timestamp."""
    if not posted_val:
        return "-"

    posted_dt = _parse_date_safe(posted_val)
    if not posted_dt:
        return "-"

    now = ref_time or datetime.datetime.now(datetime.timezone.utc)
    # Ensure timezone match
    if posted_dt.tzinfo is None:
        posted_dt = posted_dt.replace(tzinfo=datetime.timezone.utc)

    delta = now - posted_dt
    seconds = delta.total_seconds()

    if seconds < 0:
        # Future/clock skewed, treat as recent
        return "< 24 Hours"
    if seconds <= 86400:  # <= 24 hours
        return "< 24 Hours"
    if seconds <= 3 * 86400:  # <= 3 days
        return "1 - 3 Days"
    if seconds <= 7 * 86400:  # <= 7 days
        return "Within 1 Week"
    if seconds <= 30 * 86400:  # <= 30 days
        return "1 - 4 Weeks"
    return "> 1 Month"


def _determine_status(row: dict[str, Any]) -> str:
    """Determine high-level application status from job record."""
    apply_status = (row.get("apply_status") or "").lower()
    if apply_status == "expired":
        return "Expired"

    if row.get("applied_at"):
        if apply_status == "failed":
            return "Apply Failed"
        return "Applied"

    if row.get("tailored_resume_path") and row.get("cover_letter_path"):
        return "Ready to Apply"

    if row.get("tailored_resume_path"):
        return "Tailored"

    if row.get("fit_score") is not None:
        return "Scored"

    if row.get("full_description"):
        return "Enriched"

    return "Discovered"


def fetch_jobs_for_export(
    min_score: int | None = None,
    status_filter: str | None = None,
    site_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Query jobs from SQLite database with optional filtering and priority ordering.

    Ordering priority:
      1. Fit score DESC (highest match first)
      2. Posted timestamp DESC (freshest jobs first)
      3. Discovered timestamp DESC
    """
    conn = get_connection()

    query = """
        SELECT
            url, title, company, location, site, salary,
            posted_at, discovered_at, fit_score, score_reasoning,
            tailored_resume_path, cover_letter_path,
            applied_at, apply_status, full_description
        FROM jobs
        WHERE 1=1
    """
    params: list[Any] = []

    if min_score is not None:
        query += " AND fit_score >= ?"
        params.append(min_score)

    if site_filter:
        query += " AND LOWER(site) = LOWER(?)"
        params.append(site_filter)

    if status_filter:
        s = status_filter.strip().lower()
        if s == "applied":
            query += " AND applied_at IS NOT NULL AND apply_status = 'applied'"
        elif s in ("ready", "ready_to_apply"):
            query += " AND tailored_resume_path IS NOT NULL AND applied_at IS NULL AND (apply_status IS NULL OR apply_status != 'expired')"
        elif s == "tailored":
            query += " AND tailored_resume_path IS NOT NULL AND (apply_status IS NULL OR apply_status != 'expired')"
        elif s == "scored":
            query += " AND fit_score IS NOT NULL AND (apply_status IS NULL OR apply_status != 'expired')"
        elif s == "enriched":
            query += " AND full_description IS NOT NULL AND (apply_status IS NULL OR apply_status != 'expired')"
        elif s == "expired":
            query += " AND apply_status = 'expired'"

    # Priority sorting:
    # 1. Fit score (NULLS LAST)
    # 2. Posted date (NULLS LAST)
    # 3. Discovered date
    query += """
        ORDER BY
            CASE WHEN fit_score IS NOT NULL THEN 0 ELSE 1 END ASC,
            fit_score DESC,
            CASE WHEN posted_at IS NOT NULL THEN 0 ELSE 1 END ASC,
            posted_at DESC,
            discovered_at DESC
    """

    cursor = conn.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]
    return rows


def export_to_csv(
    jobs: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    """Export jobs list to a standard CSV file with utf-8-sig (Excel compatible BOM)."""
    headers = [
        "Fit Score",
        "Freshness",
        "Status",
        "Job Title",
        "Company",
        "Location",
        "Source",
        "Job URL",
        "Score Reasoning",
        "Posted Date",
        "Discovered Date",
        "Tailored Resume",
        "Cover Letter",
        "Applied Date",
        "Salary",
    ]

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)

        for job in jobs:
            writer.writerow([
                job.get("fit_score") if job.get("fit_score") is not None else "",
                _calculate_freshness(job.get("posted_at")),
                _determine_status(job),
                job.get("title") or "",
                job.get("company") or "",
                job.get("location") or "",
                job.get("site") or "",
                job.get("url") or "",
                job.get("score_reasoning") or "",
                _format_date(job.get("posted_at")),
                _format_date(job.get("discovered_at")),
                Path(job["tailored_resume_path"]).name if job.get("tailored_resume_path") else "",
                Path(job["cover_letter_path"]).name if job.get("cover_letter_path") else "",
                _format_date(job.get("applied_at")),
                job.get("salary") or "",
            ])

    return output_path


def export_to_xlsx(
    jobs: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    """Export jobs list and pipeline statistics to a styled, professional Excel (.xlsx) file."""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()

    # -------------------------------------------------------------------------
    # Sheet 1: Jobs
    # -------------------------------------------------------------------------
    ws_jobs = wb.active
    ws_jobs.title = "Jobs"
    ws_jobs.views.sheetView[0].showGridLines = True

    headers = [
        "Fit Score",
        "Freshness",
        "Status",
        "Job Title",
        "Company",
        "Location",
        "Source",
        "Job URL",
        "Score Reasoning",
        "Posted Date",
        "Discovered Date",
        "Tailored Resume",
        "Cover Letter",
        "Applied Date",
        "Salary",
    ]

    # Styles
    font_header = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
    fill_header = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    align_header = Alignment(horizontal="center", vertical="center", wrap_text=True)

    border_thin = Border(
        left=Side(style="thin", color="E0E0E0"),
        right=Side(style="thin", color="E0E0E0"),
        top=Side(style="thin", color="E0E0E0"),
        bottom=Side(style="thin", color="E0E0E0"),
    )

    # Score fills & fonts
    fill_score_high = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")  # Light green
    font_score_high = Font(name="Segoe UI", size=11, bold=True, color="276A3C")

    fill_score_mid = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")   # Light yellow
    font_score_mid = Font(name="Segoe UI", size=11, bold=True, color="7F6000")

    fill_score_low = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")   # Soft red
    font_score_low = Font(name="Segoe UI", size=11, bold=False, color="C00000")

    fill_status_applied = PatternFill(start_color="D9EAD3", end_color="D9EAD3", fill_type="solid")
    fill_status_ready = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    fill_status_expired = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
    font_status_expired = Font(name="Segoe UI", size=10, color="7F7F7F")
    fill_fresh_24h = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
    font_fresh_24h = Font(name="Segoe UI", size=10, bold=True, color="276A3C")

    font_link = Font(name="Segoe UI", size=10, color="0563C1", underline="single")
    font_regular = Font(name="Segoe UI", size=10)

    # Write headers
    ws_jobs.row_dimensions[1].height = 26
    for col_idx, header in enumerate(headers, start=1):
        cell = ws_jobs.cell(row=1, column=col_idx, value=header)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_header
        cell.border = border_thin

    # Write data rows
    for row_idx, job in enumerate(jobs, start=2):
        ws_jobs.row_dimensions[row_idx].height = 20

        score = job.get("fit_score")
        freshness = _calculate_freshness(job.get("posted_at"))
        status = _determine_status(job)
        url = job.get("url") or ""
        resume_name = Path(job["tailored_resume_path"]).name if job.get("tailored_resume_path") else ""
        cover_name = Path(job["cover_letter_path"]).name if job.get("cover_letter_path") else ""

        row_data = [
            score if score is not None else "",
            freshness,
            status,
            job.get("title") or "",
            job.get("company") or "",
            job.get("location") or "",
            job.get("site") or "",
            "Open Link" if url else "",
            job.get("score_reasoning") or "",
            _format_date(job.get("posted_at")),
            _format_date(job.get("discovered_at")),
            resume_name,
            cover_name,
            _format_date(job.get("applied_at")),
            job.get("salary") or "",
        ]

        for col_idx, val in enumerate(row_data, start=1):
            cell = ws_jobs.cell(row=row_idx, column=col_idx, value=val)
            cell.font = font_regular
            cell.border = border_thin

            # Alignments
            if col_idx in (1, 2, 3, 7, 8, 10, 11, 14):
                cell.alignment = align_center
            else:
                cell.alignment = align_left

            # Custom styling per column
            if col_idx == 1 and score is not None:  # Fit Score
                if score >= 8:
                    cell.fill = fill_score_high
                    cell.font = font_score_high
                elif score >= 6:
                    cell.fill = fill_score_mid
                    cell.font = font_score_mid
                else:
                    cell.fill = fill_score_low
                    cell.font = font_score_low

            elif col_idx == 2:  # Freshness
                if freshness == "< 24 Hours":
                    cell.fill = fill_fresh_24h
                    cell.font = font_fresh_24h

            elif col_idx == 3:  # Status
                if status == "Applied":
                    cell.fill = fill_status_applied
                elif status == "Ready to Apply":
                    cell.fill = fill_status_ready
                elif status == "Expired":
                    cell.fill = fill_status_expired
                    cell.font = font_status_expired

            elif col_idx == 8 and url:  # Clickable URL
                cell.hyperlink = url
                cell.font = font_link

    # Freeze header row & add auto filter
    ws_jobs.freeze_panes = "A2"
    ws_jobs.auto_filter.ref = ws_jobs.dimensions

    # Auto-fit column widths with sensible bounds
    min_widths = {
        1: 11,   # Fit Score
        2: 15,   # Freshness
        3: 16,   # Status
        4: 28,   # Title
        5: 22,   # Company
        6: 18,   # Location
        7: 15,   # Source
        8: 13,   # Job URL
        9: 45,   # Score Reasoning
        10: 14,  # Posted Date
        11: 16,  # Discovered Date
        12: 24,  # Tailored Resume
        13: 24,  # Cover Letter
        14: 14,  # Applied Date
        15: 16,  # Salary
    }
    for col_idx in range(1, len(headers) + 1):
        col_letter = get_column_letter(col_idx)
        ws_jobs.column_dimensions[col_letter].width = min_widths.get(col_idx, 15)

    # -------------------------------------------------------------------------
    # Sheet 2: Summary Stats
    # -------------------------------------------------------------------------
    ws_stats = wb.create_sheet(title="Summary Stats")
    ws_stats.views.sheetView[0].showGridLines = True

    stats = get_stats()

    # Title Banner
    ws_stats["A1"] = "ApplyPilot Pipeline Summary"
    ws_stats["A1"].font = Font(name="Segoe UI", size=16, bold=True, color="1F4E78")
    ws_stats["A2"] = f"Export generated on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    ws_stats["A2"].font = Font(name="Segoe UI", size=10, italic=True, color="595959")

    # Overview Metrics Table
    ws_stats["A4"] = "Pipeline Metric"
    ws_stats["B4"] = "Count"
    for cell_ref in ("A4", "B4"):
        ws_stats[cell_ref].font = font_header
        ws_stats[cell_ref].fill = fill_header
        ws_stats[cell_ref].alignment = align_center

    metrics = [
        ("Total Jobs Discovered", stats.get("total", 0)),
        ("Enriched with Description", stats.get("with_description", 0)),
        ("Scored by AI", stats.get("scored", 0)),
        ("Pending Scoring", stats.get("unscored", 0)),
        ("Tailored Resumes Created", stats.get("tailored", 0)),
        ("Eligible for Tailoring (Score 7+)", stats.get("untailored_eligible", 0)),
        ("Ready to Apply", stats.get("ready_to_apply", 0)),
        ("Applications Submitted", stats.get("applied", 0)),
        ("Application Errors", stats.get("apply_errors", 0)),
    ]

    for idx, (label, val) in enumerate(metrics, start=5):
        cell_lbl = ws_stats.cell(row=idx, column=1, value=label)
        cell_val = ws_stats.cell(row=idx, column=2, value=val)
        cell_lbl.font = font_regular
        cell_lbl.border = border_thin
        cell_val.font = Font(name="Segoe UI", size=10, bold=True)
        cell_val.alignment = align_center
        cell_val.border = border_thin

    # Score Distribution Table
    start_row = len(metrics) + 7
    ws_stats.cell(row=start_row, column=1, value="Fit Score").font = font_header
    ws_stats.cell(row=start_row, column=1).fill = fill_header
    ws_stats.cell(row=start_row, column=1).alignment = align_center

    ws_stats.cell(row=start_row, column=2, value="Jobs Count").font = font_header
    ws_stats.cell(row=start_row, column=2).fill = fill_header
    ws_stats.cell(row=start_row, column=2).alignment = align_center

    for offset, (score_val, count) in enumerate(stats.get("score_distribution", []), start=1):
        r = start_row + offset
        c1 = ws_stats.cell(row=r, column=1, value=f"Score {score_val}")
        c2 = ws_stats.cell(row=r, column=2, value=count)
        c1.font = font_regular
        c1.alignment = align_center
        c1.border = border_thin
        c2.font = font_regular
        c2.alignment = align_center
        c2.border = border_thin

    # Jobs by Source Table
    ws_stats.cell(row=4, column=4, value="Job Source").font = font_header
    ws_stats.cell(row=4, column=4).fill = fill_header
    ws_stats.cell(row=4, column=4).alignment = align_center

    ws_stats.cell(row=4, column=5, value="Total Discovered").font = font_header
    ws_stats.cell(row=4, column=5).fill = fill_header
    ws_stats.cell(row=4, column=5).alignment = align_center

    for idx, (site, count) in enumerate(stats.get("by_site", []), start=5):
        c1 = ws_stats.cell(row=idx, column=4, value=site or "Unknown")
        c2 = ws_stats.cell(row=idx, column=5, value=count)
        c1.font = font_regular
        c1.border = border_thin
        c2.font = font_regular
        c2.alignment = align_center
        c2.border = border_thin

    ws_stats.column_dimensions["A"].width = 30
    ws_stats.column_dimensions["B"].width = 15
    ws_stats.column_dimensions["C"].width = 5
    ws_stats.column_dimensions["D"].width = 25
    ws_stats.column_dimensions["E"].width = 18

    # Save to disk
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path


def export_jobs(
    output_path: Path | str | None = None,
    fmt: str = "xlsx",
    min_score: int | None = None,
    status_filter: str | None = None,
    site_filter: str | None = None,
    auto_open: bool = True,
) -> Path:
    """Fetch jobs, export to requested format, and optionally open the file."""
    format_clean = fmt.lower().strip().lstrip(".")
    if format_clean not in ("xlsx", "csv"):
        raise ValueError(f"Unsupported format '{fmt}'. Choose 'xlsx' or 'csv'.")

    if output_path:
        out_file = Path(output_path)
    else:
        out_file = APP_DIR / f"jobs_export.{format_clean}"

    jobs = fetch_jobs_for_export(
        min_score=min_score,
        status_filter=status_filter,
        site_filter=site_filter,
    )

    if format_clean == "csv":
        export_to_csv(jobs, out_file)
    else:
        export_to_xlsx(jobs, out_file)

    if auto_open:
        try:
            if sys.platform == "win32":
                os.startfile(str(out_file))
            elif sys.platform == "darwin":
                import subprocess
                subprocess.run(["open", str(out_file)], check=False)
            else:
                import subprocess
                subprocess.run(["xdg-open", str(out_file)], check=False)
        except Exception as e:
            log.warning("Could not auto-open export file: %s", e)

    return out_file
