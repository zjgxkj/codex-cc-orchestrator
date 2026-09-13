"""Logging for the Claude-Agent MCP server.

Every line goes to **stderr**. stdout is reserved exclusively for MCP protocol
messages (spec §5). Logs include ``pid`` because multiple MCP server subprocesses
may exist simultaneously under different Codex threads.

Secrets (API keys, auth headers, full sensitive env vars) are never logged.
"""

from __future__ import annotations

import logging
import os
import sys


class _StderrFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Attach structured fields if the caller passed them via extra=.
        pid = os.getpid()
        fields = []
        for name in ("request_id", "job_id", "phase", "session_id", "duration_ms", "status"):
            val = getattr(record, name, None)
            if val is not None:
                fields.append(f"{name}={val}")
        base = super().format(record)
        if fields:
            return f"[pid={pid} {' '.join(fields)}] {base}"
        return f"[pid={pid}] {base}"


_configured = False


def configure_logging(level: int | str | None = None) -> logging.Logger:
    """Configure the root ``codex_claude_agent_mcp`` logger to write to stderr."""
    global _configured
    logger = logging.getLogger("codex_claude_agent_mcp")
    if _configured:
        return logger

    if level is None:
        level = os.environ.get("CODEX_CLAUDE_AGENT_MCP_LOG_LEVEL", "INFO")
    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_StderrFormatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False  # never reach the root logger / stdout
    _configured = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    configure_logging()
    base = logging.getLogger("codex_claude_agent_mcp")
    return base.getChild(name) if name else base


def make_claude_stderr_sink(logger: logging.Logger, job_id: str | None = None):
    """Build a callback for ``ClaudeAgentOptions(stderr=...)``.

    Routes Claude Code subprocess stderr into our structured stderr logger.
    """
    prefix = f"[claude job={job_id}] " if job_id else "[claude] "

    def _sink(line: str) -> None:
        if not line:
            return
        logger.debug("%s%s", prefix, line.rstrip())

    return _sink
