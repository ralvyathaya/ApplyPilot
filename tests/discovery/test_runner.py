from applypilot.discovery.runner import run_discovery_adapters


def test_runner_isolates_adapter_failures_and_preserves_order():
    calls = []

    def ok(search_config, workers):
        calls.append(("ok", workers))
        return {"new": 2, "existing": 1, "errors": 0}

    def broken(search_config, workers):
        calls.append(("broken", workers))
        raise RuntimeError("blocked")

    result = run_discovery_adapters(
        search_config={"discovery": {"adapters": ["ok", "broken"]}},
        workers=3,
        registry={"ok": ok, "broken": broken},
    )

    assert calls == [("ok", 3), ("broken", 3)]
    assert result["status"] == "partial"
    assert result["adapters"]["ok"]["status"] == "ok"
    assert result["adapters"]["broken"] == {
        "status": "error",
        "error": "blocked",
    }


def test_runner_rejects_unknown_adapters_before_network_work():
    result = run_discovery_adapters(
        search_config={"discovery": {"adapters": ["missing"]}},
        registry={},
    )

    assert result["status"] == "error"
    assert "Unknown discovery adapter" in result["adapters"]["missing"]["error"]
