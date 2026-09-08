"""Canonical records shared by discovery adapters and persistence."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiscoveredJob:
    """A job normalized enough for the rest of the ApplyPilot pipeline."""

    url: str
    title: str
    site: str
    strategy: str
    company: str | None = None
    salary: str | None = None
    description: str | None = None
    location: str | None = None
    full_description: str | None = None
    application_url: str | None = None
    detail_error: str | None = None
