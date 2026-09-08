"""Tests for Claude Code CLI resolution in the apply launcher.

Regression coverage for WinError 2 on Windows: the bare name "claude"
is not resolvable by CreateProcess because the npm-installed CLI is
claude.cmd, so the launcher must resolve through shutil.which.
"""

import shutil

import pytest

from applypilot.apply import launcher


def test_claude_binary_returns_which_result(monkeypatch):
    resolved = r"C:\nvm4w\nodejs\claude.cmd"
    monkeypatch.setattr(shutil, "which", lambda name: resolved)
    assert launcher._claude_binary() == resolved


def test_claude_binary_raises_when_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="Claude Code CLI not found"):
        launcher._claude_binary()
