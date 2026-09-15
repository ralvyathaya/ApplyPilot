from datetime import UTC, datetime, timedelta

from applypilot.discovery._util import (
    adapter_config,
    clean_html_text,
    kebab_slug,
    parse_published_date,
    published_within,
    selected_queries,
)

SEARCH_CONFIG = {
    "queries": [
        {"query": "UI/UX Designer", "tier": 1},
        {"query": "Product Analyst", "tier": 2},
        {"query": "Management Trainee", "tier": 3},
        {"query": "  UI/UX Designer ", "tier": 1},
        {"query": "", "tier": 1},
    ],
    "discovery": {"adapter_config": {"glints": {"max_tier": 2}}},
}


def test_selected_queries_filters_tier_and_dedupes():
    assert selected_queries(SEARCH_CONFIG, 2) == ["UI/UX Designer", "Product Analyst"]
    assert selected_queries(SEARCH_CONFIG, 3) == [
        "UI/UX Designer",
        "Product Analyst",
        "Management Trainee",
    ]
    assert selected_queries({}, 2) == []


def test_adapter_config_reads_named_block():
    assert adapter_config(SEARCH_CONFIG, "glints") == {"max_tier": 2}
    assert adapter_config(SEARCH_CONFIG, "missing") == {}
    assert adapter_config({}, "missing") == {}


def test_kebab_slug():
    assert kebab_slug("UI/UX Designer") == "ui-ux-designer"
    assert kebab_slug("  Product   Analyst ") == "product-analyst"
    assert kebab_slug("Associate Product Manager (APM)") == "associate-product-manager-apm"


def test_clean_html_text():
    assert clean_html_text("<p>Hello&nbsp;<b>world</b></p>") == "Hello\nworld"
    assert clean_html_text(None) is None
    assert clean_html_text("   ") is None


def test_parse_published_date_handles_iso_and_rfc822():
    iso = parse_published_date("2025-09-10T08:00:00Z")
    assert iso == datetime(2025, 9, 10, 8, 0, tzinfo=UTC)
    rfc = parse_published_date("Wed, 10 Sep 2025 08:00:00 +0000")
    assert rfc == datetime(2025, 9, 10, 8, 0, tzinfo=UTC)
    assert parse_published_date("not a date") is None
    assert parse_published_date(None) is None


def test_published_within():
    now = datetime(2025, 9, 15, tzinfo=UTC)
    recent = (now - timedelta(days=5)).isoformat()
    old = (now - timedelta(days=40)).isoformat()

    assert published_within(recent, 30, now=now)
    assert not published_within(old, 30, now=now)
    # days_back <= 0 disables filtering; undated postings always pass.
    assert published_within(old, 0, now=now)
    assert published_within(None, 30, now=now)
    assert published_within("garbage", 30, now=now)
