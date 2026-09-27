"""Discovery adapter registry and failure-isolated orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable

from applypilot import config

log = logging.getLogger(__name__)

AdapterRunner = Callable[[dict, int], dict]
DEFAULT_ADAPTERS = ("jobspy", "workday", "smartextract")


def _run_jobspy(search_config: dict, workers: int) -> dict:
    from applypilot.discovery.jobspy import run_discovery

    return run_discovery(search_config)


def _run_workday(search_config: dict, workers: int) -> dict:
    from applypilot.discovery.workday import run_workday_discovery

    return run_workday_discovery(workers=workers)


def _run_smartextract(search_config: dict, workers: int) -> dict:
    from applypilot.discovery.smartextract import run_smart_extract

    return run_smart_extract(workers=workers)


def _run_hiringcafe(search_config: dict, workers: int) -> dict:
    from applypilot.discovery.hiringcafe import discover_hiringcafe

    return discover_hiringcafe(search_config)


def _run_jobstreet(search_config: dict, workers: int) -> dict:
    from applypilot.discovery.jobstreet import discover_jobstreet

    return discover_jobstreet(search_config)


def _run_glints(search_config: dict, workers: int) -> dict:
    from applypilot.discovery.glints import discover_glints

    return discover_glints(search_config)


def _run_remoteboards(search_config: dict, workers: int) -> dict:
    from applypilot.discovery.remoteboards import discover_remoteboards

    return discover_remoteboards(search_config)


def _run_direct_ats(search_config: dict, workers: int) -> dict:
    from applypilot.discovery.direct_ats import discover_direct_ats

    return discover_direct_ats(search_config)


def _run_kalibrr(search_config: dict, workers: int) -> dict:
    from applypilot.discovery.kalibrr import discover_kalibrr

    return discover_kalibrr(search_config)


ADAPTERS: dict[str, AdapterRunner] = {
    "jobspy": _run_jobspy,
    "workday": _run_workday,
    "smartextract": _run_smartextract,
    "hiringcafe": _run_hiringcafe,
    "jobstreet": _run_jobstreet,
    "glints": _run_glints,
    "remoteboards": _run_remoteboards,
    "direct_ats": _run_direct_ats,
    "kalibrr": _run_kalibrr,
}


def configured_adapter_names(search_config: dict) -> list[str]:
    """Resolve configured adapter names while preserving legacy defaults."""
    discovery = search_config.get("discovery") or {}
    if "adapters" not in discovery:
        return list(DEFAULT_ADAPTERS)
    return list(discovery.get("adapters") or [])


def run_discovery_adapters(
    search_config: dict | None = None,
    workers: int = 1,
    registry: dict[str, AdapterRunner] | None = None,
) -> dict:
    """Run selected adapters in order and isolate failures per adapter."""
    search_config = search_config if search_config is not None else config.load_search_config()
    registry = ADAPTERS if registry is None else registry
    names = configured_adapter_names(search_config)
    results: dict[str, dict] = {}

    for name in names:
        runner = registry.get(name)
        if runner is None:
            results[name] = {
                "status": "error",
                "error": f"Unknown discovery adapter: {name}",
            }
            continue

        log.info("Discovery adapter '%s' starting", name)
        try:
            raw = runner(search_config, workers) or {}
            errors = int(raw.get("errors", 0) or 0)
            status = raw.get("status") or ("partial" if errors else "ok")
            results[name] = {"status": status, **raw}
        except Exception as exc:
            log.exception("Discovery adapter '%s' failed", name)
            results[name] = {"status": "error", "error": str(exc)}

    statuses = [result["status"] for result in results.values()]
    if not statuses:
        status = "ok"
    elif all(item == "error" for item in statuses):
        status = "error"
    elif any(item in ("error", "partial") for item in statuses):
        status = "partial"
    else:
        status = "ok"

    return {"status": status, "adapters": results}
