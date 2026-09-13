"""Claude execution layer.

``ClaudeRunner`` is the seam between the MCP server and the Claude Agent SDK.
The server depends only on the ABC, so tests use :class:`FakeClaudeRunner` and
never touch the real SDK (spec §23.3).

Real execution uses :func:`claude_agent_sdk.query`:

* execution  -> Claude Code preset prompt + coding tools, captures session_id
* review     -> fresh session, read-only tools, PASS/FAIL verdict
* continue   -> ``resume`` the original execution session with feedback

Claude's free-form output is captured and parsed into a structured JSON block
the agent is instructed to emit; raw output is never piped to MCP stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .compact import EXEC_OUTPUT_FORMAT, REVIEW_OUTPUT_FORMAT, OUTPUT_BUDGET_APPEND
from .usage import Usage, extract_usage
from .config import DEFAULT_MAX_BUFFER_SIZE
from .errors import (
    CLAUDE_AUTH_ERROR,
    CLAUDE_BILLING_ERROR,
    CLAUDE_PROVIDER_ERROR,
    CLAUDE_PROTOCOL_ERROR,
    CLAUDE_SESSION_ERROR,
    INTERNAL_ERROR,
    MCPError,
    RATE_LIMITED,
    TASK_TIMEOUT,
)
from .logging import get_logger, make_claude_stderr_sink

log = get_logger("claude_runner")

VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Callback invoked with the earliest session id observed in the stream.
SessionCallback = Callable[[str], Awaitable[None]]

# --- Prompts ----------------------------------------------------------------

_EXEC_SYSTEM_APPEND = """\
You are CC-1, a scoped execution agent. Do only the task and acceptance; do not
expand scope or modify unrelated code.

Own necessary validation for changed code, proportional to scope and risk. For
a Bug with unclear trigger or cause, reproduce before editing and rerun the same
condition afterward. A reliable failing test or clear error evidence already
counts; do not add a ceremonial reproduction. Report only checks actually run.

Return BLOCKED rather than guess if a critical requirement is missing, a
material architecture/contract decision is required, or project reality
invalidates the task.

Finish with StructuredOutput without duplicating it as assistant text. If that
tool is unavailable, output only this JSON (no Markdown):

{"status": "COMPLETED", "summary": "<one paragraph>", "files_changed": ["..."], "validation": ["..."]}

`status`: `COMPLETED`, `BLOCKED`, or `FAILED`; always include every field.
"""

_REVIEW_SYSTEM_APPEND = """\
You are CC-2, a fresh independent reviewer. Judge the current project against
the original task and every acceptance criterion; inspect code rather than
trusting completion claims. Check logic, boundaries, assumptions, regressions,
scope/contract/safety, and whether reported validation supports the conclusion.

Use Read, Grep, and Glob only. Never use shell, external MCP tools, or write
tools; never repair. If runtime evidence is insufficient, FAIL and name the
smallest targeted check CC-1 should run. StructuredOutput is only for verdict.

Finish with StructuredOutput without duplicating it as assistant text. If that
tool is unavailable, output only this JSON (no Markdown):

{"verdict": "PASS", "summary": "<one paragraph>", "unmet_criteria": ["..."], "evidence": ["..."]}

`verdict`: `PASS` or `FAIL`. On FAIL, list unmet criteria and concrete file:line
or observational evidence; on PASS, `unmet_criteria` is empty.
"""

_CONTINUE_PROMPT_TEMPLATE = """\
REVIEWER / ORCHESTRATOR FEEDBACK:
{feedback}

Apply it within the original scope. Run the smallest necessary validation; for
a Bug, rerun its original failing condition/test. Finish with StructuredOutput
without duplicate text, or matching JSON only if the tool is unavailable.
"""

_TASK_PROMPT_TEMPLATE = "TASK:\n{task}\n\nACCEPTANCE:\n{acceptance}\n\nCWD: {cwd}\n"

_REVIEW_PROMPT_TEMPLATE = "TASK:\n{task}\n\nACCEPTANCE:\n{acceptance}\n\n{execution_line}Verify each criterion against current state.\n"

_EXEC_SYSTEM_APPEND += OUTPUT_BUDGET_APPEND
_REVIEW_SYSTEM_APPEND += OUTPUT_BUDGET_APPEND
_EXEC_OUTPUT_FORMAT = EXEC_OUTPUT_FORMAT
_REVIEW_OUTPUT_FORMAT = REVIEW_OUTPUT_FORMAT


# --- Result dataclasses -----------------------------------------------------

@dataclass
class ExecutionResult:
    status: str = "FAILED"  # COMPLETED | BLOCKED | FAILED
    completed: bool = False  # valid terminal executor payload was received
    session_id: str | None = None
    session_confirmed: bool = False
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    error: MCPError | None = None
    usage: Usage | None = None
    output_truncated: bool = False
    omitted: dict[str, int] = field(default_factory=dict)


@dataclass
class ReviewResult:
    status: str = "FAILED"  # PASS | FAIL | FAILED
    completed: bool = False  # valid PASS/FAIL payload was received
    session_id: str | None = None
    session_confirmed: bool = False
    summary: str = ""
    unmet_criteria: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    error: MCPError | None = None
    usage: Usage | None = None
    output_truncated: bool = False
    omitted: dict[str, int] = field(default_factory=dict)


# --- JSON extraction --------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

_JSON_KEYS = ('"status"', '"verdict"')


def _balanced_brace_spans(text: str) -> list[str]:
    """Return every top-level ``{...}`` span, skipping braces inside strings.

    Unlike a regex, this correctly handles values that themselves contain
    braces (e.g. ``"summary": "see {foo}"``): the closing brace of the object
    is found by depth counting with JSON string/escape awareness.
    """
    spans: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        escape = False
        j = i
        while j < n:
            c = text[j]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    spans.append(text[i : j + 1])
                    i = j
                    break
            j += 1
        else:
            # Unterminated object: no matching close brace.
            return spans
        i += 1
    return spans


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Extract the last JSON object from a fenced block or bare text."""
    if not text:
        return None
    matches = list(_FENCE_RE.finditer(text))
    if matches:
        candidate = matches[-1].group(1)
    else:
        # fall back to balanced {...} spans containing status/verdict
        candidates = [s for s in _balanced_brace_spans(text) if any(k in s for k in _JSON_KEYS)]
        candidate = candidates[-1] if candidates else None
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        # try progressively smaller trailing substrings starting with {
        for start in range(len(candidate)):
            sub = candidate[start:]
            brace = sub.find("{")
            if brace < 0:
                break
            sub = sub[brace:]
            try:
                obj = json.loads(sub)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


# --- Session id extraction --------------------------------------------------

def _message_session_id(msg: Any) -> str | None:
    """Best-effort session id from any streamed message.

    Typed messages (StreamEvent/AssistantMessage/ResultMessage/...) carry a
    ``session_id`` attribute; generic ``SystemMessage`` (e.g. ``init``) carries
    the raw CLI dict in ``data``, which includes ``session_id``. This lets a
    session be captured from the earliest message instead of only the terminal
    ResultMessage.
    """
    sid = getattr(msg, "session_id", None)
    if isinstance(sid, str) and sid:
        return sid
    data = getattr(msg, "data", None)
    if isinstance(data, dict):
        sid = data.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
    return None


# --- Effective CLI resolution (no provider call) -----------------------------

def _bundled_cli_path() -> str | None:
    """Path of the CLI bundled inside the SDK package, if present."""
    try:
        import claude_agent_sdk
    except Exception:  # pragma: no cover - SDK always installed in prod
        return None
    name = "claude.exe" if os.name == "nt" else "claude"
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return str(bundled) if bundled.is_file() else None


def resolve_cli_path(cli_path: str | None) -> str | None:
    """The CLI path the SDK transport would effectively use.

    Mirrors the SDK resolution order (explicit option, then bundled CLI, then
    PATH) without spawning anything.
    """
    if cli_path:
        return cli_path
    bundled = _bundled_cli_path()
    if bundled:
        return bundled
    return shutil.which("claude") or shutil.which("claude.exe")


def resolve_cli_version(cli_path: str | None) -> str | None:
    """Effective CLI version string, or None when it cannot be determined.

    The bundled CLI's version is known from the SDK package; an external CLI is
    queried with a short, bounded ``--version`` subprocess (no provider call).
    """
    path = resolve_cli_path(cli_path)
    if not path:
        return None
    bundled = _bundled_cli_path()
    if bundled and os.path.abspath(path) == os.path.abspath(bundled):
        try:
            from claude_agent_sdk import _cli_version

            return str(_cli_version.__cli_version__)
        except Exception:  # pragma: no cover - defensive for SDK changes
            pass
    try:
        proc = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5,
        )
        text = (proc.stdout or proc.stderr or "").strip()
        return text.splitlines()[0] if text else None
    except Exception:
        return None


def sdk_version() -> str:
    """The installed ``claude-agent-sdk`` package version."""
    try:
        import claude_agent_sdk

        return str(getattr(claude_agent_sdk, "__version__", "") or "")
    except Exception:  # pragma: no cover - defensive
        return ""


# --- ABC --------------------------------------------------------------------

class ClaudeRunner(ABC):
    @abstractmethod
    async def execute(
        self,
        *,
        task: str,
        cwd: str,
        acceptance: list[str],
        model: str | None,
        effort: str | None,
        timeout_sec: int,
        session_id: str | None = None,
        job_id: str | None = None,
        on_session_id: SessionCallback | None = None,
    ) -> ExecutionResult: ...

    @abstractmethod
    async def review(
        self,
        *,
        original_task: str,
        acceptance: list[str],
        cwd: str,
        execution_summary: str | None,
        model: str | None,
        timeout_sec: int,
        session_id: str | None = None,
        job_id: str | None = None,
        on_session_id: SessionCallback | None = None,
    ) -> ReviewResult: ...

    @abstractmethod
    async def continue_session(
        self,
        *,
        execution_session_id: str,
        feedback: str,
        cwd: str,
        model: str | None,
        effort: str | None,
        timeout_sec: int,
        job_id: str | None = None,
        on_session_id: SessionCallback | None = None,
    ) -> ExecutionResult: ...


# --- Real runner ------------------------------------------------------------

class RealClaudeRunner(ClaudeRunner):
    """Runs Claude via the Agent SDK ``query()`` API."""

    def __init__(
        self,
        cli_path: str | None = None,
        max_buffer_size: int = DEFAULT_MAX_BUFFER_SIZE,
    ) -> None:
        self.cli_path = cli_path
        self.max_buffer_size = max_buffer_size

    # -- options builders ----------------------------------------------------

    def _exec_options(
        self, *, cwd: str, model: str | None, effort: str | None,
        job_id: str | None, resume: str | None, session_id: str | None = None,
    ):
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": _EXEC_SYSTEM_APPEND},
            cwd=cwd,
            model=model,
            effort=effort,
            resume=resume,
            session_id=session_id,
            allowed_tools=[
                "Read", "Grep", "Glob", "Edit", "Write", "Bash",
            ],
            disallowed_tools=[
                "Task", "WebFetch", "WebSearch", "NotebookEdit", "MultiEdit",
            ],
            permission_mode="dontAsk",  # non-interactive: deny unlisted, never prompt
            output_format=_EXEC_OUTPUT_FORMAT,
            # Default SDK transport buffer is 1MB per streamed JSON line; large
            # tool_result payloads (base64 screenshots, contact sheets) exceed
            # it and kill the session. Env-configurable, default 20MB.
            max_buffer_size=self.max_buffer_size,
            cli_path=self.cli_path,
            stderr=make_claude_stderr_sink(log, job_id),
        )

    def _review_options(
        self, *, cwd: str, model: str | None, job_id: str | None,
        session_id: str | None = None,
    ):
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": _REVIEW_SYSTEM_APPEND},
            cwd=cwd,
            model=model,
            session_id=session_id,
            allowed_tools=["Read", "Grep", "Glob"],  # read-only
            disallowed_tools=[
                "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Task",
                "WebFetch", "WebSearch",
            ],
            permission_mode="dontAsk",
            output_format=_REVIEW_OUTPUT_FORMAT,
            max_buffer_size=self.max_buffer_size,  # see _exec_options
            cli_path=self.cli_path,
            stderr=make_claude_stderr_sink(log, job_id),
        )

    # -- core driver ---------------------------------------------------------

    async def _run_query(
        self,
        prompt: str,
        options,
        timeout_sec: int,
        on_session_id: SessionCallback | None = None,
        *,
        expected_session_id: str | None = None,
        stage: str = "execution",
    ) -> tuple[Any, str, str | None, str | None]:
        """Consume the query stream.

        Returns ``(ResultMessage|None, text, observed_session_id, stream_error)``.
        Fresh sessions are preallocated by the server; observing that same id
        confirms a resumable transcript before a terminal result exists.
        """
        from claude_agent_sdk import ResultMessage, TextBlock, query

        collected: list[str] = []
        message_errors: list[str] = []
        result_msg = None
        session_seen: str | None = None

        async def _consume(stream) -> Any:
            nonlocal result_msg, session_seen
            async for msg in stream:
                sid = _message_session_id(msg)
                if sid:
                    if expected_session_id and sid != expected_session_id:
                        raise MCPError(
                            CLAUDE_PROTOCOL_ERROR,
                            "claude returned a different session id than requested",
                            details={
                                "stage": stage,
                                "expected_session_id": expected_session_id,
                                "observed_session_id": sid,
                            },
                        )
                    if session_seen is None:
                        session_seen = sid
                        if on_session_id is not None:
                            await on_session_id(sid)
                    elif sid != session_seen:
                        raise MCPError(
                            CLAUDE_PROTOCOL_ERROR,
                            "claude changed session id within one run",
                            details={"stage": stage, "first_session_id": session_seen, "observed_session_id": sid},
                        )
                # ResultMessage is the terminal message.
                if isinstance(msg, ResultMessage):
                    result_msg = msg
                    continue
                # AssistantMessage carries content blocks; collect text.
                content = getattr(msg, "content", None)
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, TextBlock):
                            collected.append(block.text)
                message_error = getattr(msg, "error", None)
                if message_error:
                    message_errors.append(str(message_error))
            return result_msg

        stream = query(prompt=prompt, options=options)
        stream_error: str | None = None
        try:
            await asyncio.wait_for(_consume(stream), timeout=timeout_sec)
        except asyncio.TimeoutError as exc:
            sid = session_seen or expected_session_id
            details = {
                "stage": stage,
                "session_id": sid,
                "session_confirmed": session_seen is not None,
            }
            raise MCPError(
                TASK_TIMEOUT, f"claude task timed out after {timeout_sec}s",
                retryable=True, details=details,
            ) from exc
        except MCPError:
            raise
        except Exception as exc:
            # The SDK can yield a terminal ResultMessage and then raise a
            # trailing ProcessError because the CLI intentionally exits nonzero.
            # Keep the structured result and use this text only for diagnosis.
            if result_msg is not None:
                stream_error = str(exc)
            else:
                diagnostic = "\n".join([*message_errors[-8:], *collected[-8:], str(exc)])
                classified = self._classify_error(
                    None,
                    diagnostic=diagnostic,
                    stage=stage,
                    session_id=session_seen or expected_session_id,
                    session_confirmed=session_seen is not None,
                )
                if classified is not None:
                    raise classified from exc
                raise MCPError(
                    CLAUDE_SESSION_ERROR,
                    f"{type(exc).__name__}: {exc}",
                    retryable=self._looks_transient(diagnostic),
                    details={
                        "stage": stage,
                        "session_id": session_seen or expected_session_id,
                        "session_confirmed": session_seen is not None,
                    },
                ) from exc
        finally:
            # ``async for`` does NOT close its iterator when the loop body is
            # cancelled or raises (PEP 533 was deferred). Close the query
            # generator explicitly so the CLI subprocess is torn down promptly
            # instead of waiting for GC finalization (the SDK does the same
            # for its inner generator in process_query).
            try:
                await stream.aclose()
            except Exception as exc:  # cleanup must not mask the real outcome
                log.debug("claude query stream close failed: %s", exc)
        diagnostic_text = "\n".join([*message_errors, *collected])
        return result_msg, diagnostic_text, session_seen, stream_error

    @staticmethod
    def _provider_error_text(result_msg, diagnostic: str = "") -> str:
        """Return a concise, sanitized provider error line."""
        parts: list[str] = []
        result = getattr(result_msg, "result", None) if result_msg is not None else None
        if isinstance(result, str) and result.strip():
            parts.append(result.strip())
        errors = (getattr(result_msg, "errors", None) or []) if result_msg is not None else []
        for e in errors:
            if str(e).strip():
                parts.append(str(e).strip())
        parts.extend(line.strip() for line in diagnostic.splitlines() if line.strip())
        normalized = [" ".join(part.split()) for part in parts]
        markers = re.compile(
            r"api error|insufficient|balance|billing|payment|unauthor|forbidden|rate limit|overload|service unavailable",
            re.IGNORECASE,
        )
        selected = next(
            (part for part in normalized if re.search(r"API Error:\s*\d{3}|\bHTTP\s+\d{3}\b", part, re.IGNORECASE)),
            None,
        ) or next((part for part in normalized if markers.search(part)), None)
        return (selected or (normalized[0] if normalized else ""))[:300]

    @staticmethod
    def _looks_transient(text: str) -> bool:
        return bool(re.search(
            r"\b(?:408|429|500|502|503|504|529)\b|timed? out|timeout|connection (?:reset|closed)|"
            r"network|temporar(?:y|ily)|service unavailable|overload|econnreset",
            text,
            re.IGNORECASE,
        ))

    @staticmethod
    def _classify_error(
        result_msg,
        *,
        diagnostic: str = "",
        stage: str = "execution",
        session_id: str | None = None,
        session_confirmed: bool = False,
    ) -> MCPError | None:
        """Classify terminal or transport/provider failures."""
        api_status = getattr(result_msg, "api_error_status", None) if result_msg is not None else None
        errors = (getattr(result_msg, "errors", None) or []) if result_msg is not None else []
        result = getattr(result_msg, "result", None) if result_msg is not None else None
        subtype = getattr(result_msg, "subtype", None) if result_msg is not None else None
        terminal_reason = getattr(result_msg, "terminal_reason", None) if result_msg is not None else None
        raw = "\n".join([*(str(e) for e in errors), str(result or ""), diagnostic])
        if api_status is None:
            match = re.search(r"(?:API Error:\s*|\bHTTP\s+)(\d{3})\b", raw, re.IGNORECASE)
            if match:
                api_status = int(match.group(1))
        err_text = raw.lower()
        provider = RealClaudeRunner._provider_error_text(result_msg, diagnostic)
        details = {
            "stage": stage,
            "status": api_status,
            "subtype": subtype,
            "terminal_reason": terminal_reason,
            "session_id": session_id,
            "session_confirmed": session_confirmed,
        }
        if api_status == 429 or re.search(r"\brate[_ ]limit(?:ed)?\b", err_text):
            return MCPError(RATE_LIMITED, provider or "claude api rate limited", retryable=True, details=details)
        # Provider billing failures (HTTP 402 / insufficient balance) are
        # deterministic: retrying the same request cannot succeed.
        if (
            api_status == 402
            or re.search(
                r"insufficient (?:balance|credits?)|credit balance|billing[_ ]error|payment",
                err_text,
            )
        ):
            message = f"claude provider billing error: {provider}" if provider else "claude provider billing error"
            details["provider_error"] = provider
            return MCPError(CLAUDE_BILLING_ERROR, message, retryable=False, details=details)
        if api_status in (401, 403) or re.search(
            r"authentication[_ ]failed|unauthori[sz]ed|forbidden|invalid api key",
            err_text,
        ):
            details["provider_error"] = provider
            return MCPError(CLAUDE_AUTH_ERROR, provider or "claude authentication/authorization failed", details=details)
        if api_status in (408, 500, 502, 503, 504, 529) or (isinstance(api_status, int) and 500 <= api_status <= 599):
            details["provider_error"] = provider
            return MCPError(CLAUDE_PROVIDER_ERROR, provider or "claude provider unavailable", retryable=True, details=details)
        if re.search(r"\bserver[_ ]error\b|service unavailable|overload", err_text):
            details["provider_error"] = provider
            return MCPError(CLAUDE_PROVIDER_ERROR, provider or "claude provider unavailable", retryable=True, details=details)
        if isinstance(api_status, int) and 400 <= api_status <= 499:
            details["provider_error"] = provider
            return MCPError(CLAUDE_PROVIDER_ERROR, provider or f"claude provider error {api_status}", details=details)
        if re.search(r"\binvalid[_ ]request\b", err_text):
            details["provider_error"] = provider
            return MCPError(CLAUDE_PROVIDER_ERROR, provider or "claude provider rejected the request", details=details)
        if re.search(r"maximum buffer size|json message exceeded|jsondecode|error result:\s*success", err_text):
            return MCPError(CLAUDE_PROTOCOL_ERROR, provider or "claude sdk/cli protocol error", details=details)
        return None

    # -- public API ----------------------------------------------------------

    async def execute(
        self, *, task, cwd, acceptance, model, effort, timeout_sec,
        session_id=None, job_id=None, on_session_id=None,
    ) -> ExecutionResult:
        prompt = _TASK_PROMPT_TEMPLATE.format(
            task=task,
            acceptance="\n".join(f"- {c}" for c in acceptance) if acceptance else "(none provided)",
            cwd=cwd,
        )
        options = self._exec_options(
            cwd=cwd, model=model, effort=effort, job_id=job_id,
            resume=None, session_id=session_id,
        )
        return await self._execute_with(
            prompt, options, timeout_sec, job_id, kind="execution",
            expected_session_id=session_id, on_session_id=on_session_id,
        )

    async def continue_session(self, *, execution_session_id, feedback, cwd, model, effort, timeout_sec, job_id=None, on_session_id=None) -> ExecutionResult:
        prompt = _CONTINUE_PROMPT_TEMPLATE.format(feedback=feedback)
        options = self._exec_options(
            cwd=cwd, model=model, effort=effort, job_id=job_id,
            resume=execution_session_id, session_id=None,
        )
        return await self._execute_with(
            prompt, options, timeout_sec, job_id, kind="execution",
            expected_session_id=execution_session_id, on_session_id=on_session_id,
        )

    async def _execute_with(
        self, prompt, options, timeout_sec, job_id, kind,
        *, expected_session_id=None, on_session_id=None,
    ) -> ExecutionResult:
        log.info("claude %s start job=%s", kind, job_id)
        try:
            result_msg, text, stream_session, stream_error = await self._run_query(
                prompt, options, timeout_sec, on_session_id,
                expected_session_id=expected_session_id, stage=kind,
            )
        except MCPError as exc:
            log.warning("claude %s error job=%s code=%s", kind, job_id, exc.code)
            sid = (exc.details or {}).get("session_id") or expected_session_id
            return ExecutionResult(
                status="FAILED", session_id=sid,
                session_confirmed=bool((exc.details or {}).get("session_confirmed")),
                error=exc,
            )
        except Exception as exc:  # SDK transport / CLI errors
            log.exception("claude %s unexpected error job=%s", kind, job_id)
            return ExecutionResult(
                status="FAILED", session_id=expected_session_id,
                error=MCPError(
                    CLAUDE_SESSION_ERROR, f"{type(exc).__name__}: {exc}",
                    retryable=self._looks_transient(str(exc)),
                    details={"stage": kind, "session_id": expected_session_id, "session_confirmed": False},
                ),
            )

        session_id = stream_session or expected_session_id or (getattr(result_msg, "session_id", None) if result_msg else None)
        session_confirmed = stream_session is not None
        usage = extract_usage(result_msg) if result_msg is not None else None

        diagnostic = "\n".join(part for part in (text, stream_error or "") if part)
        classified = self._classify_error(
            result_msg,
            diagnostic=diagnostic,
            stage=kind,
            session_id=session_id,
            session_confirmed=session_confirmed,
        ) if (result_msg is not None and getattr(result_msg, "is_error", False)) or stream_error else None
        if classified is not None:
            return ExecutionResult(
                status="FAILED", session_id=session_id,
                session_confirmed=session_confirmed, error=classified, usage=usage,
            )

        if result_msg is None:
            return ExecutionResult(
                status="FAILED", session_id=session_id,
                session_confirmed=session_confirmed,
                error=MCPError(
                    CLAUDE_PROTOCOL_ERROR, "claude stream ended without a terminal result",
                    details={"stage": kind, "session_id": session_id,
                             "session_confirmed": session_confirmed},
                ),
            )

        if getattr(result_msg, "is_error", False):
            provider_text = self._provider_error_text(result_msg, diagnostic)
            err = MCPError(
                CLAUDE_SESSION_ERROR,
                provider_text or "claude execution reported an error",
                retryable=self._looks_transient("\n".join((provider_text, diagnostic))),
                details={
                    "stage": kind, "session_id": session_id,
                    "session_confirmed": session_confirmed,
                },
            )
            return ExecutionResult(
                status="FAILED", session_id=session_id,
                session_confirmed=session_confirmed, error=err, usage=usage,
            )

        structured = getattr(result_msg, "structured_output", None) if result_msg else None
        obj = structured if isinstance(structured, dict) else _extract_json_block(
            getattr(result_msg, "result", None) or text
        )
        if obj is None:
            return ExecutionResult(
                status="FAILED",
                session_id=session_id,
                session_confirmed=session_confirmed,
                summary="Worker output was not a valid structured result.",
                # Deterministic: the same output will fail to parse again.
                error=MCPError(CLAUDE_PROTOCOL_ERROR, "could not parse structured status from claude output"),
                usage=usage,
            )

        status = obj.get("status")
        summary = obj.get("summary")
        files_changed = obj.get("files_changed")
        validation = obj.get("validation")
        valid = (
            status in ("COMPLETED", "BLOCKED", "FAILED")
            and isinstance(summary, str)
            and isinstance(files_changed, list)
            and all(isinstance(item, str) for item in files_changed)
            and isinstance(validation, list)
            and all(isinstance(item, str) for item in validation)
        )
        if not valid:
            return ExecutionResult(
                status="FAILED",
                session_id=session_id,
                session_confirmed=session_confirmed,
                error=MCPError(CLAUDE_PROTOCOL_ERROR, "claude returned an invalid execution result"),
                usage=usage,
            )
        return ExecutionResult(
            status=status,
            completed=True,
            session_id=session_id,
            session_confirmed=session_confirmed,
            summary=summary,
            files_changed=files_changed,
            validation=validation,
            usage=usage,
        )

    async def review(
        self, *, original_task, acceptance, cwd, execution_summary, model,
        timeout_sec, session_id=None, job_id=None, on_session_id=None,
    ) -> ReviewResult:
        execution_line = f"EXECUTOR REPORTED:\n{execution_summary}\n" if execution_summary else ""
        prompt = _REVIEW_PROMPT_TEMPLATE.format(
            task=original_task,
            acceptance="\n".join(f"- {c}" for c in acceptance),
            execution_line=execution_line,
        )
        options = self._review_options(
            cwd=cwd, model=model, job_id=job_id, session_id=session_id,
        )
        log.info("claude review start job=%s", job_id)
        try:
            result_msg, text, stream_session, stream_error = await self._run_query(
                prompt, options, timeout_sec, on_session_id,
                expected_session_id=session_id, stage="review",
            )
        except MCPError as exc:
            sid = (exc.details or {}).get("session_id") or session_id
            return ReviewResult(
                status="FAILED", session_id=sid,
                session_confirmed=bool((exc.details or {}).get("session_confirmed")),
                error=exc,
            )
        except Exception as exc:
            log.exception("claude review unexpected error job=%s", job_id)
            return ReviewResult(
                status="FAILED", session_id=session_id,
                error=MCPError(
                    CLAUDE_SESSION_ERROR, f"{type(exc).__name__}: {exc}",
                    retryable=self._looks_transient(str(exc)),
                    details={"stage": "review", "session_id": session_id, "session_confirmed": False},
                ),
            )

        session_id = stream_session or session_id or (getattr(result_msg, "session_id", None) if result_msg else None)
        session_confirmed = stream_session is not None
        usage = extract_usage(result_msg) if result_msg is not None else None

        diagnostic = "\n".join(part for part in (text, stream_error or "") if part)
        classified = self._classify_error(
            result_msg,
            diagnostic=diagnostic,
            stage="review",
            session_id=session_id,
            session_confirmed=session_confirmed,
        ) if (result_msg is not None and getattr(result_msg, "is_error", False)) or stream_error else None
        if classified is not None:
            return ReviewResult(
                status="FAILED", session_id=session_id,
                session_confirmed=session_confirmed, error=classified, usage=usage,
            )

        if result_msg is None:
            return ReviewResult(
                status="FAILED", session_id=session_id,
                session_confirmed=session_confirmed,
                error=MCPError(
                    CLAUDE_PROTOCOL_ERROR, "claude stream ended without a terminal result",
                    details={"stage": "review", "session_id": session_id,
                             "session_confirmed": session_confirmed},
                ),
            )

        if getattr(result_msg, "is_error", False):
            provider_text = self._provider_error_text(result_msg, diagnostic)
            err = MCPError(
                CLAUDE_SESSION_ERROR,
                provider_text or "claude review reported an error",
                retryable=self._looks_transient("\n".join((provider_text, diagnostic))),
                details={
                    "stage": "review", "session_id": session_id,
                    "session_confirmed": session_confirmed,
                },
            )
            return ReviewResult(
                status="FAILED", session_id=session_id,
                session_confirmed=session_confirmed, error=err, usage=usage,
            )

        structured = getattr(result_msg, "structured_output", None) if result_msg else None
        obj = structured if isinstance(structured, dict) else _extract_json_block(
            getattr(result_msg, "result", None) or text
        )
        if obj is None:
            return ReviewResult(
                status="FAILED",
                session_id=session_id,
                session_confirmed=session_confirmed,
                summary="Worker output was not a valid structured result.",
                # Deterministic: the same output will fail to parse again.
                error=MCPError(CLAUDE_PROTOCOL_ERROR, "could not parse structured verdict from claude output"),
                usage=usage,
            )

        verdict = obj.get("verdict")
        summary = obj.get("summary")
        unmet_criteria = obj.get("unmet_criteria")
        evidence = obj.get("evidence")
        valid = (
            verdict in ("PASS", "FAIL")
            and isinstance(summary, str)
            and isinstance(unmet_criteria, list)
            and all(isinstance(item, str) for item in unmet_criteria)
            and isinstance(evidence, list)
            and all(isinstance(item, str) for item in evidence)
        )
        if not valid:
            return ReviewResult(
                status="FAILED",
                session_id=session_id,
                session_confirmed=session_confirmed,
                error=MCPError(CLAUDE_PROTOCOL_ERROR, "claude returned an invalid review result"),
                usage=usage,
            )
        # Contradictory payloads violate the review contract (PASS means no
        # unmet criteria; FAIL must name them) and are deterministic.
        if (
            (verdict == "PASS" and unmet_criteria)
            or (verdict == "FAIL" and (not unmet_criteria or not evidence))
        ):
            return ReviewResult(
                status="FAILED",
                session_id=session_id,
                session_confirmed=session_confirmed,
                summary=summary,
                error=MCPError(
                    CLAUDE_PROTOCOL_ERROR,
                    f"claude review verdict contradicts its criteria list ({verdict})",
                ),
                usage=usage,
            )
        return ReviewResult(
            status=verdict,
            completed=True,
            session_id=session_id,
            session_confirmed=session_confirmed,
            summary=summary,
            unmet_criteria=unmet_criteria,
            evidence=evidence,
            usage=usage,
        )


# --- Fake runner (for tests) ------------------------------------------------

class FakeClaudeRunner(ClaudeRunner):
    """Deterministic runner for tests (spec §23.3).

    Modes: ``success`` | ``blocked`` | ``failure`` | ``timeout`` | ``slow`` |
    ``review_pass`` | ``review_fail``.

    A mode may be a single string (used for every call) or a list cycled per
    call kind. ``timeout`` makes execute/review raise a TASK_TIMEOUT MCPError.
    """

    def __init__(self, execute_mode: str = "success", review_mode: str = "review_pass") -> None:
        self.execute_mode = execute_mode
        self.review_mode = review_mode
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._counter = 0

    def _next_session_id(self) -> str:
        self._counter += 1
        return f"00000000-0000-0000-0000-{self._counter:012d}"

    async def _emit_session(self, on_session_id, session_id: str) -> None:
        if on_session_id is not None:
            await on_session_id(session_id)

    async def execute(
        self, *, task, cwd, acceptance, model, effort, timeout_sec,
        session_id=None, job_id=None, on_session_id=None,
    ) -> ExecutionResult:
        self.calls.append(("execute", {"task": task, "cwd": cwd, "job_id": job_id}))
        mode = self.execute_mode
        if mode == "timeout":
            raise MCPError(TASK_TIMEOUT, "fake timeout", retryable=True)
        if mode == "slow":
            await asyncio.sleep(30)
            return ExecutionResult(status="COMPLETED", session_id=self._next_session_id(), summary="slow completed")
        if mode == "failure":
            sid = session_id or self._next_session_id()
            await self._emit_session(on_session_id, sid)
            return ExecutionResult(status="FAILED", session_id=sid, session_confirmed=True, error=MCPError(CLAUDE_SESSION_ERROR, "fake failure", retryable=True))
        if mode == "blocked":
            sid = session_id or self._next_session_id()
            await self._emit_session(on_session_id, sid)
            return ExecutionResult(status="BLOCKED", completed=True, session_id=sid, session_confirmed=True, summary="fake blocked")
        sid = session_id or self._next_session_id()
        await self._emit_session(on_session_id, sid)
        return ExecutionResult(
            status="COMPLETED",
            completed=True,
            session_id=sid,
            session_confirmed=True,
            summary="fake completed",
            files_changed=["fake/file.py"],
            validation=["fake test passed"],
        )

    async def review(
        self, *, original_task, acceptance, cwd, execution_summary, model,
        timeout_sec, session_id=None, job_id=None, on_session_id=None,
    ) -> ReviewResult:
        self.calls.append(("review", {"job_id": job_id}))
        mode = self.review_mode
        if mode == "timeout":
            raise MCPError(TASK_TIMEOUT, "fake review timeout", retryable=True)
        if mode == "slow":
            await asyncio.sleep(30)
            return ReviewResult(status="PASS", session_id=self._next_session_id(), summary="slow pass")
        if mode == "review_fail":
            sid = session_id or self._next_session_id()
            await self._emit_session(on_session_id, sid)
            return ReviewResult(
                status="FAIL",
                completed=True,
                session_id=sid,
                session_confirmed=True,
                summary="fake fail",
                unmet_criteria=["criterion A not met"],
                evidence=["evidence.py:10"],
            )
        sid = session_id or self._next_session_id()
        await self._emit_session(on_session_id, sid)
        return ReviewResult(
            status="PASS",
            completed=True,
            session_id=sid,
            session_confirmed=True,
            summary="fake pass",
            unmet_criteria=[],
            evidence=["all good"],
        )

    async def continue_session(self, *, execution_session_id, feedback, cwd, model, effort, timeout_sec, job_id=None, on_session_id=None) -> ExecutionResult:
        self.calls.append(("continue", {"execution_session_id": execution_session_id, "job_id": job_id}))
        mode = self.execute_mode
        if mode == "timeout":
            raise MCPError(TASK_TIMEOUT, "fake continue timeout", retryable=True)
        if mode == "slow":
            await asyncio.sleep(30)
            return ExecutionResult(status="COMPLETED", session_id=execution_session_id, summary="slow continue completed")
        if mode == "failure":
            await self._emit_session(on_session_id, execution_session_id)
            return ExecutionResult(status="FAILED", session_id=execution_session_id, session_confirmed=True, error=MCPError(CLAUDE_SESSION_ERROR, "fake continue failure", retryable=True))
        if mode == "blocked":
            await self._emit_session(on_session_id, execution_session_id)
            return ExecutionResult(status="BLOCKED", completed=True, session_id=execution_session_id, session_confirmed=True, summary="fake blocked after continue")
        await self._emit_session(on_session_id, execution_session_id)
        return ExecutionResult(
            status="COMPLETED",
            completed=True,
            session_id=execution_session_id,
            session_confirmed=True,
            summary="fake completed after continue",
            files_changed=["fake/file.py"],
            validation=["fake test passed"],
        )
