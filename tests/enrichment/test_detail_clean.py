from applypilot.enrichment.detail import _clean_url


def test_clean_url_passes_real_urls_through():
    assert _clean_url("https://example.com/apply") == "https://example.com/apply"
    assert _clean_url("  https://example.com/a  ") == "https://example.com/a"


def test_clean_url_rejects_sentinel_strings():
    for bad in ("None", "none", "null", "NULL", "", "   "):
        assert _clean_url(bad) is None


def test_clean_url_rejects_non_strings():
    assert _clean_url(None) is None
    assert _clean_url(123) is None
    assert _clean_url({"url": "https://x"}) is None
