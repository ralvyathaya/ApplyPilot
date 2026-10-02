"""Tests for resume tailoring validation and JSON extraction resilience."""

import json
from applypilot.scoring.tailor import extract_json, assemble_resume_text
from applypilot.scoring.validator import validate_json_fields


MOCK_PROFILE = {
    "personal": {
        "full_name": "Test Candidate",
        "email": "test@example.com",
    },
    "experience": {
        "target_role": "Junior Product Designer",
    },
    "skills_boundary": {
        "tools": ["Figma", "Git"],
    },
    "resume_facts": {
        "preserved_companies": ["Acme Corp"],
        "preserved_school": "University of Technology",
    },
}


def test_validate_json_fields_allows_empty_projects():
    data = {
        "title": "Brand Communication Intern",
        "summary": "Enthusiastic communicator with visual design skills.",
        "skills": {"Marketing": "Social Media, Content"},
        "experience": [
            {
                "header": "Marketing Intern at Acme Corp",
                "bullets": ["Managed social media campaigns."],
            }
        ],
        "projects": [],  # Empty projects list must be accepted!
        "education": "University of Technology | Bachelor Degree",
    }
    result = validate_json_fields(data, MOCK_PROFILE)
    assert result["passed"] is True
    assert result["errors"] == []


def test_validate_json_fields_maps_title_alias():
    data = {
        "job_title": "Content Creator Intern",
        "summary": "Skilled content creator with video editing background.",
        "skills": {"Tools": "Premiere, Figma"},
        "experience": [
            {
                "header": "Creator at Acme Corp",
                "bullets": ["Created video reels."],
            }
        ],
        "projects": [],
        "education": "University of Technology | Bachelor Degree",
    }
    result = validate_json_fields(data, MOCK_PROFILE)
    assert result["passed"] is True
    assert data["title"] == "Content Creator Intern"


def test_validate_json_fields_normalizes_skills_list():
    data = {
        "title": "Product Designer",
        "summary": "Passionate designer.",
        "skills": ["Figma", "UI/UX", "User Research"],  # List instead of dict
        "experience": [
            {
                "header": "Designer at Acme Corp",
                "bullets": ["Designed wireframes."],
            }
        ],
        "projects": [],
        "education": "University of Technology | Bachelor Degree",
    }
    result = validate_json_fields(data, MOCK_PROFILE)
    assert result["passed"] is True
    assert isinstance(data["skills"], dict)


def test_extract_json_tolerates_preamble_and_fences():
    text = (
        "Here is the tailored resume you requested:\n```json\n"
        '{"title": "Designer", "summary": "Great match"}\n```\nHope this helps!'
    )
    result = extract_json(text)
    assert result["title"] == "Designer"
    assert result["summary"] == "Great match"


def test_extract_json_salvages_trailing_comma():
    text = '{"title": "Designer", "summary": "Great match",}'
    result = extract_json(text)
    assert result["title"] == "Designer"


def test_extract_json_auto_closes_truncated_json():
    # Model cut off before closing braces
    text = 'Thinking process...\n{"title": "Designer", "summary": "Great match", "projects": ["Project 1"'
    result = extract_json(text)
    assert result["title"] == "Designer"
    assert result["summary"] == "Great match"


def test_assemble_resume_text_formats_dict_education():
    data = {
        "title": "Brand Intern",
        "summary": "Summary here.",
        "skills": {"Marketing": "Social Media"},
        "experience": [],
        "projects": [],
        "education": {
            "university": "University of Technology",
            "degree": "Bachelor of Design",
            "period": "2022 - 2026",
            "gpa": "3.8",
        },
    }
    text = assemble_resume_text(data, MOCK_PROFILE)
    assert "University of Technology | Bachelor of Design | 2022 - 2026 | GPA: 3.8" in text
    assert "PROJECTS" not in text  # Empty projects section is omitted
