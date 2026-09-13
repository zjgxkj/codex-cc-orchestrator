"""Configuration for the Claude-Agent MCP server.

All configuration is read from environment variables so that multiple STDIO MCP
server subprocesses (one per Codex thread) can be launched with the same launch
command while still being tuned individually if needed.

See ``CODEX_CLAUDE_AGENT_MCP_SPEC.md`` §13 (concurrency), §16 (security), §17 (timeout).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import INVALID_ARGUMENT, INVALID_CWD, MCPError

# Default per-stream JSON-line buffer passed to ClaudeAgentOptions. The SDK
# default is 1 MiB; large tool_result payloads (base64 screenshots, contact
# sheets) exceed it and kill the session.
DEFAULT_MAX_BUFFER_SIZE = 20 * 1024 * 1024


@dataclass(frozen=True)
class CwdAuthorization:
    """A resolved ``cwd`` plus the authorization basis it was checked against.

    ``project_root`` is ``None`` when the legacy ``ALLOWED_PROJECT_ROOTS`` basis
    was used, and the resolved job-scoped root otherwise.
    """

    cwd: Path
    project_root: Path | None = None


def _split_paths(raw: str) -> list[Path]:
    """Split a path-list env var on ``;`` (Windows) or ``:`` (POSIX).

    Drives like ``D:\\`` contain a colon, so on Windows we must split on ``;``.
    """
    if os.name == "nt":
        parts = raw.split(";")
    else:
        parts = raw.split(":")
    return [Path(p).resolve() for p in parts if p.strip()]


@dataclass
class Config:
    """Resolved configuration for one MCP server process."""

    max_concurrency: int
    default_task_timeout_sec: int
    max_task_timeout_sec: int
    db_path: Path
    cli_path: str | None
    max_buffer_size: int = DEFAULT_MAX_BUFFER_SIZE
    allowed_roots: list[Path] = field(default_factory=list)
    allowed_models: list[str] = field(default_factory=list)
    # Explicit test/development escape hatch. ``from_env`` never enables it:
    # production requires either a job-scoped root or a matching long-term root.
    allow_any_cwd: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        max_concurrency = int(os.environ.get("CLAUDE_MAX_CONCURRENCY", "4"))
        if max_concurrency < 1:
            raise ValueError("CLAUDE_MAX_CONCURRENCY must be >= 1")

        default_timeout = int(os.environ.get("DEFAULT_TASK_TIMEOUT_SEC", "3600"))
        max_timeout = int(os.environ.get("MAX_TASK_TIMEOUT_SEC", "7200"))
        if default_timeout > max_timeout:
            raise ValueError("DEFAULT_TASK_TIMEOUT_SEC must be <= MAX_TASK_TIMEOUT_SEC")

        db_env = os.environ.get("CODEX_CLAUDE_AGENT_MCP_DB_PATH", "")
        if db_env.strip():
            db_path = Path(db_env).expanduser().resolve()
        else:
            # Shared, multi-process accessible default. All MCP subprocesses that
            # do not override this path read/write the same store, so session
            # resume works across Codex threads.
            db_path = Path.home() / ".codex-claude-agent-mcp" / "state.db"

        cli_path = os.environ.get("CLAUDE_CLI_PATH") or None

        buffer_raw = os.environ.get("CLAUDE_SDK_MAX_BUFFER_SIZE", "")
        if buffer_raw.strip():
            max_buffer_size = int(buffer_raw)
        else:
            max_buffer_size = DEFAULT_MAX_BUFFER_SIZE
        if max_buffer_size < 1024:
            raise ValueError("CLAUDE_SDK_MAX_BUFFER_SIZE must be >= 1024 bytes")

        # Optional long-term trust list. An empty value is valid: it grants no
        # permission at all (never a drive-wide fallback), and callers then rely
        # on an explicit job-scoped ``project_root`` per task.
        roots_raw = os.environ.get("ALLOWED_PROJECT_ROOTS", "")
        allowed_roots = _split_paths(roots_raw) if roots_raw.strip() else []
        missing_roots = [str(root) for root in allowed_roots if not root.is_dir()]
        if missing_roots:
            raise ValueError(f"ALLOWED_PROJECT_ROOTS contains missing directories: {missing_roots}")
        allow_any_cwd = False

        models_raw = os.environ.get("ALLOWED_MODELS", "")
        allowed_models = [m.strip() for m in models_raw.split(",") if m.strip()]

        return cls(
            max_concurrency=max_concurrency,
            default_task_timeout_sec=default_timeout,
            max_task_timeout_sec=max_timeout,
            db_path=db_path,
            cli_path=cli_path,
            max_buffer_size=max_buffer_size,
            allowed_roots=allowed_roots,
            allowed_models=allowed_models,
            allow_any_cwd=allow_any_cwd,
        )

    # -- validation ----------------------------------------------------------

    def resolve_project_root(self, project_root: str | None) -> Path | None:
        """Resolve an optional job-scoped authorization root.

        The caller (Codex) supplies this explicitly; it is **never** inferred
        from the MCP process cwd. ``None`` means "no job-scoped root" and keeps
        the legacy ``ALLOWED_PROJECT_ROOTS`` behavior. A supplied value must be
        an existing absolute directory.

        Raises :class:`MCPError` (INVALID_CWD) otherwise.
        """
        if project_root is None:
            return None
        try:
            raw_path = Path(project_root).expanduser()
            if not raw_path.is_absolute():
                raise MCPError(
                    INVALID_CWD,
                    "project_root must be an absolute path",
                    details={"field": "project_root", "project_root": project_root},
                )
            resolved = raw_path.resolve()
        except (OSError, ValueError) as exc:  # malformed path
            raise MCPError(
                INVALID_CWD, f"invalid project_root: {exc}", details={"field": "project_root"}
            ) from exc

        if not resolved.is_dir():
            raise MCPError(
                INVALID_CWD,
                f"project_root is not an existing directory: {resolved}",
                details={"field": "project_root", "project_root": str(resolved)},
            )
        return resolved

    def authorize_cwd(self, cwd: str | None, project_root: Path | None = None) -> CwdAuthorization:
        """Resolve ``cwd`` and authorize it against a job-scoped or permanent root.

        With ``project_root`` the resolved cwd must be that directory or a child
        of it; the root applies even when it is outside ``ALLOWED_PROJECT_ROOTS``.
        Without it, the legacy ``ALLOWED_PROJECT_ROOTS`` check applies.

        Raises :class:`MCPError` (INVALID_CWD) on traversal, a missing
        directory, or an unauthorized root.
        """
        if not cwd:
            raise MCPError(INVALID_CWD, "cwd is required", details={"field": "cwd"})

        try:
            raw_path = Path(cwd).expanduser()
            if not raw_path.is_absolute() and not self.allow_any_cwd:
                raise MCPError(INVALID_CWD, "cwd must be an absolute path", details={"cwd": cwd})
            resolved = raw_path.resolve()
        except (OSError, ValueError) as exc:  # malformed path / traversal
            raise MCPError(INVALID_CWD, f"invalid cwd: {exc}") from exc

        if not resolved.is_dir():
            raise MCPError(INVALID_CWD, f"cwd is not an existing directory: {resolved}")

        # Path traversal: reject after resolution if it escaped the input root.
        # ``resolve()`` already collapses ``..``; a normalized path that is not
        # under the governing root is rejected below.
        if project_root is not None:
            # An explicit job-scoped root is a caller requirement, so it is
            # enforced even when ALLOWED_PROJECT_ROOTS is empty and even for the
            # allow_any_cwd test escape hatch.
            try:
                resolved.relative_to(project_root)
            except ValueError:
                raise MCPError(
                    INVALID_CWD,
                    f"cwd '{resolved}' is not inside project_root '{project_root}'",
                    details={"cwd": str(resolved), "project_root": str(project_root)},
                ) from None
            return CwdAuthorization(cwd=resolved, project_root=project_root)

        if self.allow_any_cwd:
            return CwdAuthorization(cwd=resolved)

        for root in self.allowed_roots:
            try:
                resolved.relative_to(root)
                return CwdAuthorization(cwd=resolved)
            except ValueError:
                continue

        raise MCPError(
            INVALID_CWD,
            f"cwd '{resolved}' is not under any ALLOWED_PROJECT_ROOTS",
            details={"cwd": str(resolved), "allowed_roots": [str(r) for r in self.allowed_roots]},
        )

    def validate_cwd(self, cwd: str | None) -> Path:
        """Resolve and authorize ``cwd`` against the permanent allowed roots."""
        return self.authorize_cwd(cwd).cwd

    def validate_model(self, model: str | None) -> str | None:
        if model is None:
            return None
        if self.allowed_models and model not in self.allowed_models:
            raise MCPError(
                INVALID_ARGUMENT,
                f"model '{model}' is not in ALLOWED_MODELS",
                details={"model": model, "allowed": self.allowed_models},
            )
        return model

    def clamp_timeout(self, timeout_sec: int | None) -> int:
        if timeout_sec is None:
            return self.default_task_timeout_sec
        if timeout_sec < 1:
            raise MCPError(INVALID_ARGUMENT, "timeout_sec must be >= 1", details={"field": "timeout_sec"})
        return min(timeout_sec, self.max_task_timeout_sec)


_config: Config | None = None


def get_config() -> Config:
    """Return the process-wide config, lazily built from the environment."""
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


def reset_config() -> None:
    """Reset the cached config (used by tests that mutate the environment)."""
    global _config
    _config = None
