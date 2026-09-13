"""Shared pytest fixtures.

Tests never call the real Claude API. They build an :class:`AppState` with a
:class:`FakeClaudeRunner`, a temporary SQLite DB, and ``allow_any_cwd=True``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from codex_claude_agent_mcp.claude_runner import FakeClaudeRunner
from codex_claude_agent_mcp.config import Config
from codex_claude_agent_mcp.scheduler import Scheduler
from codex_claude_agent_mcp.server import AppState
from codex_claude_agent_mcp.session_store import SessionStore


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def config(tmp_db_path: Path) -> Config:
    return Config(
        max_concurrency=2,
        default_task_timeout_sec=60,
        max_task_timeout_sec=120,
        db_path=tmp_db_path,
        cli_path=None,
        allowed_roots=[],
        allowed_models=[],
        allow_any_cwd=True,
    )


@pytest.fixture
def fake_runner() -> FakeClaudeRunner:
    return FakeClaudeRunner(execute_mode="success", review_mode="review_pass")


@pytest.fixture
async def app(config: Config, fake_runner: FakeClaudeRunner) -> AppState:
    store = SessionStore(config.db_path)
    await store.init()
    scheduler = Scheduler(config.max_concurrency)
    state = AppState(config=config, store=store, runner=fake_runner, scheduler=scheduler)
    yield state
    await state.close()


@pytest.fixture
def isolated_cwd(tmp_path: Path) -> str:
    """A real directory that exists, for cwd validation tests."""
    d = tmp_path / "project"
    d.mkdir()
    return str(d)
