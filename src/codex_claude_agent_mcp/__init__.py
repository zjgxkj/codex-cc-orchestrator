"""Claude-Agent MCP: a STDIO MCP server that lets Codex delegate scoped
coding tasks to Claude Code via the Claude Agent SDK.

Entrypoints:
    python -m codex_claude_agent_mcp
    codex-claude-agent-mcp   (console script)
"""

from __future__ import annotations

__version__ = "0.3.0"


def main() -> None:
    """Console-script / ``python -m`` entrypoint."""
    from .server import main as _main

    _main()


__all__ = ["main", "__version__"]
