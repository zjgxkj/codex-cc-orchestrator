"""Structured error model for the Claude-Agent MCP server.

All errors surfaced to the MCP client are structured objects with a stable
``code``. Raw stack traces are written to stderr (see :mod:`codex_claude_agent_mcp.logging`)
and never used as the tool-result body.

See ``CODEX_CLAUDE_AGENT_MCP_SPEC.md`` §18 (Error model).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- Error codes (spec §18) -------------------------------------------------

INVALID_ARGUMENT = "INVALID_ARGUMENT"
REVIEW_REQUIRES_ACCEPTANCE = "REVIEW_REQUIRES_ACCEPTANCE"
INVALID_CWD = "INVALID_CWD"
CLAUDE_SESSION_ERROR = "CLAUDE_SESSION_ERROR"
CLAUDE_AUTH_ERROR = "CLAUDE_AUTH_ERROR"
CLAUDE_BILLING_ERROR = "CLAUDE_BILLING_ERROR"
CLAUDE_PROVIDER_ERROR = "CLAUDE_PROVIDER_ERROR"
CLAUDE_PROTOCOL_ERROR = "CLAUDE_PROTOCOL_ERROR"
RATE_LIMITED = "RATE_LIMITED"
TASK_TIMEOUT = "TASK_TIMEOUT"
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
SESSION_MISMATCH = "SESSION_MISMATCH"
JOB_CONTEXT_MISMATCH = "JOB_CONTEXT_MISMATCH"
JOB_STATE_CONFLICT = "JOB_STATE_CONFLICT"
CANCELLED = "CANCELLED"
STATE_STORE_ERROR = "STATE_STORE_ERROR"
INTERNAL_ERROR = "INTERNAL_ERROR"

# Codes that are safe to retry without changing the request.
_RETRYABLE_CODES = frozenset({RATE_LIMITED, TASK_TIMEOUT})


@dataclass
class MCPError(Exception):
    """An error that maps 1:1 to the structured error object returned to Codex.

    Attributes:
        code: Stable machine-readable error code (one of the module constants).
        message: Human-readable description; safe to return to the client.
        retryable: Whether the caller may retry the same request as-is.
        details: Optional structured extra context.
    """

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Exception.__init__ expects a message string; keep the traceback happy.
        super().__init__(self.message)
        if self.code in _RETRYABLE_CODES and not self.retryable:
            self.retryable = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


def internal_error(message: str, details: dict[str, Any] | None = None) -> MCPError:
    return MCPError(INTERNAL_ERROR, message, retryable=False, details=details or {})
