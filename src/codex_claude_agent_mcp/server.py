"""Claude-Agent MCP STDIO server (spec §11).

Tools:
    ping           - liveness / protocol sanity check + runtime metadata
    execute_task   - run a scoped task via Claude, capture session_id (no auto review)
    review_task    - fresh read-only Claude session judges PASS/FAIL
    continue_task  - resume an execution session with feedback
    run_job        - execute (+ optional review) as one deterministic flow
    run_jobs       - batch of independently-scoped jobs, bounded concurrency
    get_job_status - persisted job state for post-timeout recovery

Core logic lives in ``_execute_task`` / ``_review_task`` / ... so tests can call
them directly with an injected :class:`AppState` (FakeClaudeRunner, temp DB).
The ``@mcp.tool`` wrappers are thin and only adapt MCP I/O.
"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import asynccontextmanager
import json
import os
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .claude_runner import (
    VALID_EFFORTS,
    ClaudeRunner,
    ExecutionResult,
    FakeClaudeRunner,
    RealClaudeRunner,
    ReviewResult,
    resolve_cli_path,
    resolve_cli_version,
    sdk_version,
)
from .config import Config, get_config
from .errors import (
    CANCELLED,
    CLAUDE_PROTOCOL_ERROR,
    INVALID_ARGUMENT,
    INTERNAL_ERROR,
    JOB_CONTEXT_MISMATCH,
    JOB_STATE_CONFLICT,
    MCPError,
    REVIEW_REQUIRES_ACCEPTANCE,
    SESSION_MISMATCH,
    SESSION_NOT_FOUND,
    STATE_STORE_ERROR,
    internal_error,
)
from .logging import configure_logging, get_logger
from .models import (
    ContinueTaskInput,
    ContinueTaskResult,
    Criterion,
    CriteriaRequired,
    Effort,
    ErrorInfo,
    ExecuteTaskInput,
    ExecuteTaskResult,
    JobSpec,
    JobId,
    FeedbackText,
    JobStatusResult,
    PathText,
    PositiveSeconds,
    ProjectRoot,
    PingResult,
    ReviewTaskInput,
    ReviewTaskResult,
    RunJobInput,
    RunJobResult,
    RunJobsInput,
    RunJobsResult,
    TaskText,
)
from .compact import compact_payload
from .ownership import ProcessOwner
from .scheduler import Scheduler
from .session_store import SessionStore

log = get_logger("server")


# --- Application state ------------------------------------------------------

class AppState:
    """Process-wide dependencies, injectable for tests."""

    def __init__(self, config: Config, store: SessionStore, runner: ClaudeRunner, scheduler: Scheduler) -> None:
        self.config = config
        self.store = store
        self.runner = runner
        self.scheduler = scheduler
        self.owner = ProcessOwner(store.db_path)
        self.background_tasks: set[asyncio.Task] = set()
        self.closing = False

    @classmethod
    async def create(cls, config: Config | None = None, runner: ClaudeRunner | None = None) -> "AppState":
        config = config or get_config()
        store = SessionStore(config.db_path)
        await store.init()
        runner = runner or RealClaudeRunner(cli_path=config.cli_path, max_buffer_size=config.max_buffer_size)
        scheduler = Scheduler(config.max_concurrency)
        state = cls(config=config, store=store, runner=runner, scheduler=scheduler)
        try:
            await store.reconcile(state.owner.is_dead)
        except BaseException:
            state.owner.close()
            await store.close()
            raise
        return state

    async def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        tasks = list(self.background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.store.reconcile(self.owner.is_dead, owner_id=self.owner.owner_id)
        finally:
            await self.store.close()
            self.owner.close()

    def spawn(self, coro, job_id: str) -> asyncio.Task:
        # A fresh context prevents request cancellation/deadline contextvars
        # from leaking into the detached execution.
        task = asyncio.create_task(coro, name=f"execute:{job_id}", context=contextvars.Context())
        self.background_tasks.add(task)
        def done(task):
            self.background_tasks.discard(task)
            if not task.cancelled():
                error = task.exception()  # Always retrieve exceptions.
                if error:
                    log.error("background task failed job=%s", job_id,
                              exc_info=(type(error), error, error.__traceback__))
        task.add_done_callback(done)
        return task


_app: AppState | None = None
_app_lock = asyncio.Lock()


async def get_app() -> AppState:
    global _app
    async with _app_lock:
        if _app is None:
            _app = await AppState.create()
        return _app


async def set_app(app: AppState) -> None:
    """Inject a custom state (used by tests)."""
    global _app
    if _app is not None:
        await _app.close()
    _app = app


# --- helpers ----------------------------------------------------------------

def _err(exc: MCPError) -> ErrorInfo:
    return ErrorInfo(code=exc.code, message=exc.message, retryable=exc.retryable, details=exc.details)


def _exec_to_result(job_id: str, r: ExecutionResult) -> ExecuteTaskResult:
    return ExecuteTaskResult(
        job_id=job_id,
        status=r.status,
        execution_completed=r.completed,
        execution_session_id=r.session_id,
        execution_session_confirmed=r.session_confirmed,
        summary=r.summary,
        files_changed=r.files_changed,
        validation=r.validation,
        error=_err(r.error) if r.error else None,
        usage=r.usage,
        output_truncated=r.output_truncated,
        omitted=r.omitted,
    )


def _review_to_result(job_id: str, r: ReviewResult) -> ReviewTaskResult:
    completed = r.completed and r.status in ("PASS", "FAIL") and r.error is None
    return ReviewTaskResult(
        job_id=job_id,
        status=r.status,
        review_session_id=r.session_id,
        review_session_confirmed=r.session_confirmed,
        summary=r.summary,
        review_completed=completed,
        # An incomplete review has no verdict payload: null the criteria/
        # evidence so the caller cannot mistake absence for "nothing unmet".
        unmet_criteria=r.unmet_criteria if completed else None,
        evidence=r.evidence if completed else None,
        error=_err(r.error) if r.error else None,
        usage=r.usage,
        output_truncated=r.output_truncated,
        omitted=r.omitted,
    )


def _validate_effort(effort: str | None) -> str | None:
    if effort is None:
        return None
    if effort not in VALID_EFFORTS:
        raise MCPError(
            INVALID_ARGUMENT,
            f"effort must be one of {VALID_EFFORTS}",
            details={"effort": effort},
        )
    return effort


def _require_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MCPError(INVALID_ARGUMENT, f"{field} must be a non-empty string", details={"field": field})
    return value.strip()


def _validate_criteria(acceptance: list[str] | None, *, required: bool) -> list[str]:
    values = acceptance or []
    if required and not values:
        raise MCPError(REVIEW_REQUIRES_ACCEPTANCE, "review requires non-empty acceptance criteria")
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise MCPError(INVALID_ARGUMENT, "acceptance criteria must be non-empty strings")
    return [item.strip() for item in values]


def _request_id() -> str:
    return uuid.uuid4().hex


def _context_mismatch(job_id: str, fields: list[str]) -> MCPError:
    return MCPError(
        JOB_CONTEXT_MISMATCH,
        f"request context does not match the persisted job: {', '.join(fields)}",
        details={"job_id": job_id, "mismatched_fields": fields},
    )


def _stored_project_root(job: dict[str, Any]) -> Path | None:
    """The job's persisted authorization root (``None`` = permanent roots basis).

    A resume or review inherits this value; it is never replaced by (or widened
    to) a newly supplied ``project_root``.
    """
    stored = job.get("project_root")
    return Path(stored).resolve() if stored else None


def _state_conflict(job_id: str, status: str | None, operation: str) -> MCPError:
    return MCPError(
        JOB_STATE_CONFLICT,
        f"cannot start {operation}; job is currently in state {status!r}",
        retryable=True,
        details={"job_id": job_id, "status": status, "operation": operation},
    )


async def _mark_cancelled(app: AppState, job_id: str, expected_status: str, stage: str) -> None:
    """Best-effort stage-aware update from a cancelled request task."""
    try:
        await asyncio.shield(app.store.mark_cancelled(
            job_id, expected_status, stage, app.owner.owner_id))
    except asyncio.CancelledError:
        # A second cancellation must not turn cancellation cleanup into a tool
        # failure or overwrite a state already completed by another coroutine.
        pass
    except Exception:
        log.exception("failed to persist cancellation job=%s", job_id)


def _session_confirmation_binding(app: AppState, job_id: str, kind: str):
    """Mark a preallocated id confirmed when the SDK stream echoes it."""
    async def _confirm(session_id: str) -> None:
        await app.store.confirm_session(job_id, kind, session_id)

    return _confirm


def _error_session_id(exc: MCPError) -> str | None:
    sid = (exc.details or {}).get("session_id")
    return sid if isinstance(sid, str) and sid else None


def _stage_error(exc: MCPError, stage: str, session_id: str | None, confirmed: bool) -> MCPError:
    exc.details.setdefault("stage", stage)
    exc.details.setdefault("session_id", session_id)
    exc.details.setdefault("session_confirmed", confirmed)
    return exc


# --- core logic (testable) --------------------------------------------------

async def _invoke(app: AppState, job_id: str, operation: str, session_id: str, call):
    """One journal row per actual runner invocation, including failed calls."""
    stage = "review" if operation == "review" else "execution"
    invocation_id = uuid.uuid4().hex
    if operation == "execute":
        await app.store.transition_status(job_id, from_statuses={"QUEUED"}, to_status="EXECUTING")
        await app.store.update_fields(job_id, execution_status="EXECUTING")
    await app.store.start_invocation(invocation_id, job_id, stage, operation,
                                     session_id, app.owner.owner_id)
    try:
        result = await call()
    except BaseException:
        await asyncio.shield(app.store.finish_invocation(invocation_id, "INCOMPLETE", None))
        raise
    await app.store.finish_invocation(invocation_id,
        result.status if result.completed else "INCOMPLETE", result.usage)
    fields = ("summary", "unmet_criteria", "evidence") if stage == "review" else ("summary", "files_changed", "validation")
    payload = compact_payload({**{key: getattr(result, key) for key in fields},
                               "output_truncated": result.output_truncated,
                               "omitted": result.omitted}, stage)
    for key, value in payload.items():
        setattr(result, key, value)
    return result


async def _execute_task_core(
    app: AppState, job_id: str, task: str, cwd: str,
    acceptance: list[str] | None, model: str | None,
    effort: str | None, timeout_sec: int | None,
    project_root: str | None = None, background: bool = False,
) -> ExecuteTaskResult:
    coro = _execute_task_impl(app, job_id, task, cwd, acceptance, model, effort,
                             timeout_sec, project_root, background)
    if background:
        # Admission is managed too: cancellation during SQLite creation must
        # not leave a committed QUEUED job without a worker in a live process.
        return await asyncio.shield(app.spawn(coro, job_id))
    return await coro


async def _execute_task_impl(
    app: AppState, job_id: str, task: str, cwd: str,
    acceptance: list[str] | None, model: str | None,
    effort: str | None, timeout_sec: int | None,
    project_root: str | None = None,
    background: bool = False,
) -> ExecuteTaskResult:
    cfg = app.config
    if app.closing:
        return ExecuteTaskResult(job_id=job_id, error=_err(MCPError(CANCELLED, "MCP is shutting down")))
    try:
        job_id = _require_text(job_id, "job_id")
        task = _require_text(task, "task")
        acceptance = _validate_criteria(acceptance, required=False)
        # An explicit project_root authorizes this job only; without one the
        # permanent ALLOWED_PROJECT_ROOTS check still applies.
        root = cfg.resolve_project_root(project_root)
        resolved = cfg.authorize_cwd(cwd, root).cwd
        model = cfg.validate_model(model)
        effort = _validate_effort(effort)
        timeout = cfg.clamp_timeout(timeout_sec)
    except MCPError as exc:
        log.warning("execute_task invalid args job=%s code=%s", job_id, exc.code)
        return ExecuteTaskResult(job_id=job_id, error=_err(exc))

    execution_session_id = str(uuid.uuid4())
    try:
        await app.store.create_job(
            job_id, task, acceptance, str(resolved), status="QUEUED" if background else "EXECUTING",
            owner_id=app.owner.owner_id,
            project_root=str(root) if root is not None else None,
            execution_status="QUEUED" if background else "EXECUTING",
            execution_session_id=execution_session_id,
        )
    except MCPError as exc:
        return ExecuteTaskResult(job_id=job_id, error=_err(exc))

    if background:
        app.spawn(_background_execute(app, job_id, task, resolved, acceptance, model,
                                     effort, timeout, execution_session_id), job_id)
        return ExecuteTaskResult(job_id=job_id, status="QUEUED",
                                 execution_session_id=execution_session_id)
    return await _execute_admitted(app, job_id, task, resolved, acceptance, model,
                                   effort, timeout, execution_session_id)


async def _background_execute(app, job_id, *args):
    try:
        await _execute_admitted(app, job_id, *args)
    except asyncio.CancelledError:
        await _mark_cancelled(app, job_id, "QUEUED", "execution")
        raise
    except Exception as exc:
        log.exception("background execution failed job=%s", job_id)
        await app.store.set_execution_session(
            job_id, None, "EXECUTION_INCOMPLETE", None, execution_status="INCOMPLETE",
            error=_err(internal_error(str(exc))).model_dump())


async def _execute_admitted(app, job_id, task, resolved, acceptance, model, effort,
                            timeout, execution_session_id):
    try:
        r: ExecutionResult = await app.scheduler.run(
            lambda: _invoke(app, job_id, "execute", execution_session_id, lambda: app.runner.execute(
                task=task, cwd=str(resolved), acceptance=acceptance,
                model=model, effort=effort, timeout_sec=timeout, job_id=job_id,
                session_id=execution_session_id,
                on_session_id=_session_confirmation_binding(app, job_id, "execution"),
            )),
            label=f"execute:{job_id}",
        )
    except asyncio.CancelledError:
        await _mark_cancelled(app, job_id, "EXECUTING", "execution")
        await _mark_cancelled(app, job_id, "QUEUED", "execution")
        raise
    except MCPError as exc:
        stored = await app.store.get_job(job_id)
        confirmed = bool(stored and stored.get("execution_session_confirmed"))
        exc = _stage_error(exc, "execution", execution_session_id, confirmed)
        await app.store.set_execution_session(
            job_id, execution_session_id, "EXECUTION_INCOMPLETE", None,
            execution_status="INCOMPLETE", session_confirmed=confirmed,
            error=_err(exc).model_dump(),
        )
        return ExecuteTaskResult(
            job_id=job_id, status="FAILED", execution_session_id=execution_session_id,
            execution_session_confirmed=confirmed, error=_err(exc),
        )
    except Exception as exc:
        log.exception("execute_task unexpected job=%s", job_id)
        stored = await app.store.get_job(job_id)
        confirmed = bool(stored and stored.get("execution_session_confirmed"))
        err = _stage_error(
            internal_error(f"{type(exc).__name__}: {exc}"),
            "execution", execution_session_id, confirmed,
        )
        await app.store.set_execution_session(
            job_id, execution_session_id, "EXECUTION_INCOMPLETE", None,
            execution_status="INCOMPLETE", session_confirmed=confirmed,
            error=_err(err).model_dump(),
        )
        return ExecuteTaskResult(
            job_id=job_id, status="FAILED", execution_session_id=execution_session_id,
            execution_session_confirmed=confirmed, error=_err(err),
        )

    if r.session_id and r.session_id != execution_session_id:
        r = ExecutionResult(
            status="FAILED", session_id=execution_session_id,
            session_confirmed=False,
            error=MCPError(
                CLAUDE_PROTOCOL_ERROR, "runner returned an unexpected execution session id",
                details={"observed_session_id": r.session_id},
            ),
        )
    r.session_id = execution_session_id
    stored = await app.store.get_job(job_id)
    r.session_confirmed = r.session_confirmed or bool(stored and stored.get("execution_session_confirmed"))
    if r.error:
        r.error = _stage_error(r.error, "execution", execution_session_id, r.session_confirmed)
    stage_status = r.status if r.completed else "INCOMPLETE"
    overall_status = r.status if r.completed else "EXECUTION_INCOMPLETE"
    await app.store.set_execution_session(
        job_id, execution_session_id, overall_status, r.summary,
        execution_status=stage_status, session_confirmed=r.session_confirmed,
        error=_err(r.error).model_dump() if r.error else None,
        result=_exec_to_result(job_id, r).model_dump(),
    )
    return _exec_to_result(job_id, r)


async def _review_task_core(
    app: AppState, job_id: str, original_task: str, acceptance: list[str],
    cwd: str, execution_summary: str | None, model: str | None, timeout_sec: int | None,
    project_root: str | None = None,
) -> ReviewTaskResult:
    cfg = app.config
    try:
        job_id = _require_text(job_id, "job_id")
        original_task = _require_text(original_task, "original_task")
        acceptance = _validate_criteria(acceptance, required=True)
        supplied_root = cfg.resolve_project_root(project_root)
        model = cfg.validate_model(model)
        timeout = cfg.clamp_timeout(timeout_sec)
    except MCPError as exc:
        log.warning("review_task invalid args job=%s code=%s", job_id, exc.code)
        return ReviewTaskResult(job_id=job_id, error=_err(exc))

    # Link to an existing job only when the caller repeats the exact persisted
    # task package.  This prevents a reused job_id from reviewing another task
    # or acceptance list while the store still identifies the original job.
    review_session_id = str(uuid.uuid4())
    existing = await app.store.get_job(job_id)
    if existing is None:
        # A review-only job may establish its own project_root.
        try:
            resolved = cfg.authorize_cwd(cwd, supplied_root).cwd
            await app.store.create_job(
                job_id, original_task, acceptance, str(resolved), status="REVIEWING",
                project_root=str(supplied_root) if supplied_root is not None else None,
                review_status="REVIEWING",
                owner_id=app.owner.owner_id,
                review_session_id=review_session_id,
            )
        except MCPError as exc:
            return ReviewTaskResult(job_id=job_id, error=_err(exc))
    else:
        # An existing job owns its authorization root: review inherits the
        # stored root, and a supplied project_root must match it exactly. It can
        # neither add a root to a permanent-roots job nor switch/widen the root.
        stored_root = _stored_project_root(existing)
        mismatches: list[str] = []
        if existing["task"] != original_task:
            mismatches.append("original_task")
        if existing["acceptance"] != acceptance:
            mismatches.append("acceptance")
        if supplied_root is not None and supplied_root != stored_root:
            mismatches.append("project_root")
        if mismatches:
            return ReviewTaskResult(job_id=job_id, error=_err(_context_mismatch(job_id, mismatches)))
        try:
            resolved = cfg.authorize_cwd(cwd, stored_root).cwd
        except MCPError as exc:
            return ReviewTaskResult(job_id=job_id, error=_err(exc))
        if Path(existing["cwd"]).resolve() != resolved:
            return ReviewTaskResult(job_id=job_id, error=_err(_context_mismatch(job_id, ["cwd"])))
        claimed = await app.store.transition_status(
            job_id,
            from_statuses={"COMPLETED", "BLOCKED", "FAILED", "REVIEW_FAILED",
                           "REVIEW_INCOMPLETE", "CANCELLED"},
            to_status="REVIEWING",
            owner_id=app.owner.owner_id,
        )
        if not claimed:
            current = await app.store.get_job(job_id)
            return ReviewTaskResult(
                job_id=job_id,
                error=_err(_state_conflict(job_id, current["status"] if current else None, "review")),
            )
        await app.store.update_fields(
            job_id,
            review_status="REVIEWING",
            review_session_id=review_session_id,
            review_session_confirmed=0,
            review_error_json=None,
            review_result_json=None,
        )

    try:
        r: ReviewResult = await app.scheduler.run(
            lambda: _invoke(app, job_id, "review", review_session_id, lambda: app.runner.review(
                original_task=original_task, acceptance=acceptance, cwd=str(resolved),
                execution_summary=execution_summary, model=model,
                timeout_sec=timeout, job_id=job_id,
                session_id=review_session_id,
                on_session_id=_session_confirmation_binding(app, job_id, "review"),
            )),
            label=f"review:{job_id}",
        )
    except asyncio.CancelledError:
        await _mark_cancelled(app, job_id, "REVIEWING", "review")
        raise
    except MCPError as exc:
        stored = await app.store.get_job(job_id)
        confirmed = bool(stored and stored.get("review_session_confirmed"))
        exc = _stage_error(exc, "review", review_session_id, confirmed)
        await app.store.set_review_session(
            job_id, review_session_id, "REVIEW_INCOMPLETE", None,
            review_status="INCOMPLETE", session_confirmed=confirmed,
            error=_err(exc).model_dump(),
        )
        return ReviewTaskResult(
            job_id=job_id, status="FAILED", review_session_id=review_session_id,
            review_session_confirmed=confirmed, error=_err(exc),
        )
    except Exception as exc:
        log.exception("review_task unexpected job=%s", job_id)
        stored = await app.store.get_job(job_id)
        confirmed = bool(stored and stored.get("review_session_confirmed"))
        err = _stage_error(
            internal_error(f"{type(exc).__name__}: {exc}"),
            "review", review_session_id, confirmed,
        )
        await app.store.set_review_session(
            job_id, review_session_id, "REVIEW_INCOMPLETE", None,
            review_status="INCOMPLETE", session_confirmed=confirmed,
            error=_err(err).model_dump(),
        )
        return ReviewTaskResult(
            job_id=job_id, status="FAILED", review_session_id=review_session_id,
            review_session_confirmed=confirmed, error=_err(err),
        )

    # Map the review verdict to the job-level state machine (spec §12):
    #   PASS -> COMPLETED, FAIL -> REVIEW_FAILED,
    #   FAILED(error) -> REVIEW_INCOMPLETE (never masks completed execution)
    if r.session_id and r.session_id != review_session_id:
        r = ReviewResult(
            status="FAILED", session_id=review_session_id,
            error=MCPError(
                CLAUDE_PROTOCOL_ERROR, "runner returned an unexpected review session id",
                details={"observed_session_id": r.session_id},
            ),
        )
    r.session_id = review_session_id
    stored = await app.store.get_job(job_id)
    r.session_confirmed = r.session_confirmed or bool(stored and stored.get("review_session_confirmed"))
    if r.error:
        r.error = _stage_error(r.error, "review", review_session_id, r.session_confirmed)

    if r.completed and r.status == "PASS":
        job_status = "COMPLETED"
        review_status = "PASS"
    elif r.completed and r.status == "FAIL":
        job_status = "REVIEW_FAILED"
        review_status = "FAIL"
    else:
        job_status = "REVIEW_INCOMPLETE"
        review_status = "INCOMPLETE"
    await app.store.set_review_session(
        job_id, review_session_id, job_status, r.summary,
        review_status=review_status, session_confirmed=r.session_confirmed,
        error=_err(r.error).model_dump() if r.error else None,
        result=_review_to_result(job_id, r).model_dump(),
    )
    return _review_to_result(job_id, r)


async def _continue_task_core(
    app: AppState, job_id: str, execution_session_id: str, feedback: str,
    cwd: str, model: str | None, effort: str | None, timeout_sec: int | None,
) -> ContinueTaskResult:
    cfg = app.config
    try:
        job_id = _require_text(job_id, "job_id")
        execution_session_id = _require_text(execution_session_id, "execution_session_id")
        feedback = _require_text(feedback, "feedback")
        model = cfg.validate_model(model)
        effort = _validate_effort(effort)
        timeout = cfg.clamp_timeout(timeout_sec)
    except MCPError as exc:
        return ContinueTaskResult(job_id=job_id, error=_err(exc))

    existing = await app.store.get_job(job_id)
    if existing is None:
        return ContinueTaskResult(
            job_id=job_id,
            error=_err(MCPError(SESSION_NOT_FOUND, f"job not found: {job_id}", details={"job_id": job_id})),
        )
    # A resume has no project_root input: it inherits the stored root (or the
    # permanent ALLOWED_PROJECT_ROOTS basis for jobs created without one), so it
    # can never enlarge this job's authorization.
    try:
        resolved = cfg.authorize_cwd(cwd, _stored_project_root(existing)).cwd
    except MCPError as exc:
        return ContinueTaskResult(job_id=job_id, error=_err(exc))
    mismatches: list[str] = []
    if Path(existing["cwd"]).resolve() != resolved:
        mismatches.append("cwd")
    if mismatches:
        return ContinueTaskResult(job_id=job_id, error=_err(_context_mismatch(job_id, mismatches)))

    stored_session_id = existing.get("execution_session_id")
    if not stored_session_id:
        return ContinueTaskResult(
            job_id=job_id,
            error=_err(MCPError(
                SESSION_NOT_FOUND,
                f"job has no resumable execution session: {job_id}",
                details={"job_id": job_id},
            )),
        )
    if not existing.get("execution_session_confirmed"):
        return ContinueTaskResult(
            job_id=job_id,
            execution_session_id=stored_session_id,
            execution_session_confirmed=False,
            error=_err(MCPError(
                SESSION_NOT_FOUND,
                "execution session was allocated but never confirmed by Claude",
                details={
                    "job_id": job_id,
                    "session_id": stored_session_id,
                    "session_confirmed": False,
                },
            )),
        )
    if execution_session_id != stored_session_id:
        return ContinueTaskResult(
            job_id=job_id,
            error=_err(MCPError(
                SESSION_MISMATCH,
                "execution_session_id does not belong to this job",
                details={"job_id": job_id},
            )),
        )

    claimed = await app.store.transition_status(
        job_id,
        from_statuses={"COMPLETED", "BLOCKED", "FAILED", "EXECUTION_INCOMPLETE",
                       "REVIEW_FAILED", "REVIEW_INCOMPLETE", "CANCELLED"},
        to_status="RESUMING_EXECUTION",
        owner_id=app.owner.owner_id,
    )
    if not claimed:
        current = await app.store.get_job(job_id)
        return ContinueTaskResult(
            job_id=job_id,
            error=_err(_state_conflict(job_id, current["status"] if current else None, "resume")),
        )
    fields: dict[str, Any] = {
        "execution_status": "EXECUTING",
        "execution_error_json": None,
        "execution_result_json": None,
    }
    if existing.get("review_status") or existing.get("review_session_id"):
        fields["review_status"] = "STALE"
    await app.store.update_fields(job_id, **fields)

    try:
        r: ExecutionResult = await app.scheduler.run(
            lambda: _invoke(app, job_id, "continue", execution_session_id, lambda: app.runner.continue_session(
                execution_session_id=execution_session_id, feedback=feedback,
                cwd=str(resolved), model=model, effort=effort,
                timeout_sec=timeout, job_id=job_id,
                on_session_id=_session_confirmation_binding(app, job_id, "execution"),
            )),
            label=f"continue:{job_id}",
        )
    except asyncio.CancelledError:
        await _mark_cancelled(app, job_id, "RESUMING_EXECUTION", "execution")
        raise
    except MCPError as exc:
        stored = await app.store.get_job(job_id)
        confirmed = bool(stored and stored.get("execution_session_confirmed"))
        exc = _stage_error(exc, "execution", execution_session_id, confirmed)
        await app.store.set_execution_session(
            job_id, execution_session_id, "EXECUTION_INCOMPLETE", None,
            execution_status="INCOMPLETE", session_confirmed=confirmed,
            error=_err(exc).model_dump(),
        )
        return ContinueTaskResult(
            job_id=job_id, status="FAILED", execution_session_id=execution_session_id,
            execution_session_confirmed=confirmed, error=_err(exc),
        )
    except Exception as exc:
        log.exception("continue_task unexpected job=%s", job_id)
        stored = await app.store.get_job(job_id)
        confirmed = bool(stored and stored.get("execution_session_confirmed"))
        err = _stage_error(
            internal_error(f"{type(exc).__name__}: {exc}"),
            "execution", execution_session_id, confirmed,
        )
        await app.store.set_execution_session(
            job_id, execution_session_id, "EXECUTION_INCOMPLETE", None,
            execution_status="INCOMPLETE", session_confirmed=confirmed,
            error=_err(err).model_dump(),
        )
        return ContinueTaskResult(
            job_id=job_id, status="FAILED", execution_session_id=execution_session_id,
            execution_session_confirmed=confirmed, error=_err(err),
        )

    r.session_id = execution_session_id
    stored = await app.store.get_job(job_id)
    r.session_confirmed = r.session_confirmed or bool(stored and stored.get("execution_session_confirmed"))
    if r.error:
        r.error = _stage_error(r.error, "execution", execution_session_id, r.session_confirmed)
    stage_status = r.status if r.completed else "INCOMPLETE"
    overall_status = r.status if r.completed else "EXECUTION_INCOMPLETE"
    await app.store.set_execution_session(
        job_id, execution_session_id, overall_status, r.summary,
        execution_status=stage_status, session_confirmed=r.session_confirmed,
        error=_err(r.error).model_dump() if r.error else None,
        result=_exec_to_result(job_id, r).model_dump(),
    )
    return ContinueTaskResult(
        job_id=job_id,
        status=r.status,
        execution_completed=r.completed,
        execution_session_id=execution_session_id,
        execution_session_confirmed=r.session_confirmed,
        summary=r.summary,
        files_changed=r.files_changed,
        validation=r.validation,
        error=_err(r.error) if r.error else None,
        usage=r.usage,
        output_truncated=r.output_truncated,
        omitted=r.omitted,
    )


async def _run_job_core(app: AppState, spec: JobSpec) -> RunJobResult:
    cfg = app.config
    # review=true + empty acceptance is an input error (spec §6.3).
    if spec.review and not spec.acceptance:
        return RunJobResult(
            job_id=spec.job_id,
            execution=ExecuteTaskResult(job_id=spec.job_id, error=_err(MCPError(
                REVIEW_REQUIRES_ACCEPTANCE,
                "review=true requires non-empty acceptance",
                details={"job_id": spec.job_id},
            ))),
            status=None,
        )

    exec_result = await _execute_task_core(
        app, spec.job_id, spec.task, spec.cwd, spec.acceptance,
        spec.model, spec.effort, spec.timeout_sec, spec.project_root,
    )
    review_result: ReviewTaskResult | None = None
    combined_status = exec_result.status if exec_result.execution_completed else "EXECUTION_INCOMPLETE"

    if spec.review and exec_result.execution_completed and exec_result.status == "COMPLETED":
        # project_root=None here: the follow-up review inherits the root that
        # execution just persisted for this job.
        review_result = await _review_task_core(
            app, spec.job_id, spec.task, spec.acceptance, spec.cwd,
            exec_result.summary, spec.model, spec.timeout_sec, None,
        )
        if review_result.status == "PASS":
            combined_status = "COMPLETED"
        elif review_result.status == "FAIL":
            combined_status = "REVIEW_FAILED"
        else:
            # Review hit a timeout/transport/provider/protocol error. The
            # execution completed; do not mask it with FAILED.
            combined_status = "REVIEW_INCOMPLETE"
    elif spec.review and not (exec_result.execution_completed and exec_result.status == "COMPLETED"):
        # Execution did not complete; skip review deterministically.
        combined_status = exec_result.status if exec_result.execution_completed else "EXECUTION_INCOMPLETE"

    return RunJobResult(
        job_id=spec.job_id,
        execution=exec_result,
        review=review_result,
        status=combined_status,
        execution_status=exec_result.status if exec_result.execution_completed else "INCOMPLETE",
        review_status=(
            review_result.status if review_result and review_result.review_completed
            else "INCOMPLETE" if review_result else None
        ),
    )


async def _run_jobs_core(app: AppState, jobs: list[JobSpec], max_concurrency: int | None) -> RunJobsResult:
    # job_id uniqueness within the batch.
    seen: set[str] = set()
    for j in jobs:
        if j.job_id in seen:
            return RunJobsResult(error=_err(MCPError(
                INVALID_ARGUMENT,
                f"duplicate job_id in batch: {j.job_id}",
                details={"job_id": j.job_id},
            )))
        seen.add(j.job_id)

    cfg = app.config
    if max_concurrency and max_concurrency < 1:
        return RunJobsResult(error=_err(MCPError(INVALID_ARGUMENT, "max_concurrency must be >= 1")))
    effective = min(max_concurrency or cfg.max_concurrency, cfg.max_concurrency)
    batch_sem = asyncio.Semaphore(effective)

    # NOTE: the batch semaphore is intentionally SEPARATE from the process-wide
    # scheduler. _run_job_core -> _execute_task_core/_review_task_core already
    # acquire the scheduler around each Claude call (and release it between
    # execute and review). Wrapping the whole job in the SAME scheduler again
    # would re-enter it and deadlock once concurrent jobs reach the limit.
    async def _run_one(spec: JobSpec) -> RunJobResult:
        async with batch_sem:
            return await _run_job_core(app, spec)

    raw = await asyncio.gather(*(_run_one(j) for j in jobs), return_exceptions=True)

    results: list[RunJobResult] = []
    for spec, item in zip(jobs, raw):
        if isinstance(item, BaseException):
            log.exception("run_jobs job failed job=%s", spec.job_id, exc_info=item)
            results.append(RunJobResult(
                job_id=spec.job_id,
                execution=ExecuteTaskResult(job_id=spec.job_id, status="FAILED",
                                            error=_err(internal_error(f"{type(item).__name__}: {item}"))),
                status="FAILED",
            ))
        else:
            results.append(item)
    return RunJobsResult(results=results)


async def _get_job_status_core(app: AppState, job_id: str, cwd: str) -> JobStatusResult:
    """Authorized persisted state lookup for post-timeout recovery."""
    try:
        job_id = _require_text(job_id, "job_id")
    except MCPError as exc:
        return JobStatusResult(job_id=job_id, error=_err(exc))
    await app.store.reconcile(app.owner.is_dead)
    job = await app.store.get_job(job_id)
    if job is None:
        return JobStatusResult(job_id=job_id, found=False)
    try:
        resolved = app.config.authorize_cwd(cwd, _stored_project_root(job)).cwd
    except MCPError as exc:
        return JobStatusResult(job_id=job_id, found=False, error=_err(exc))
    if Path(job["cwd"]).resolve() != resolved:
        return JobStatusResult(
            job_id=job_id, found=False,
            error=_err(_context_mismatch(job_id, ["cwd"])),
        )
    return JobStatusResult(
        job_id=job_id,
        found=True,
        usage=await app.store.get_usage(job_id),
        execution_result=job.get("execution_result"),
        review_result=job.get("review_result"),
        status=job.get("status"),
        execution_status=job.get("execution_status"),
        review_status=job.get("review_status"),
        execution_session_id=job.get("execution_session_id") or None,
        execution_session_confirmed=bool(job.get("execution_session_confirmed")),
        review_session_id=job.get("review_session_id") or None,
        review_session_confirmed=bool(job.get("review_session_confirmed")),
        execution_summary=job.get("execution_summary"),
        review_summary=job.get("review_summary"),
        execution_error=job.get("execution_error"),
        review_error=job.get("review_error"),
        created_at=job.get("created_at"),
        updated_at=job.get("updated_at"),
    )


def _ping_metadata() -> PingResult:
    """Runtime metadata without touching Claude or the provider."""
    cfg = get_config()
    cli_path = resolve_cli_path(cfg.cli_path)
    return PingResult(
        ok=True,
        pid=os.getpid(),
        version=__version__,
        sdk_version=sdk_version(),
        cli_path=cli_path,
        cli_version=resolve_cli_version(cli_path),
        max_buffer_size=cfg.max_buffer_size,
    )


# --- FastMCP server & tool wrappers -----------------------------------------

def build_server() -> FastMCP:
    @asynccontextmanager
    async def lifespan(server):
        app = await get_app()
        try:
            yield app
        finally:
            # Shield graceful cleanup from transport/request cancel scopes.
            import anyio
            with anyio.CancelScope(shield=True):
                await app.close()
            global _app
            if _app is app:
                _app = None

    mcp = FastMCP("codex-claude-agent-mcp", lifespan=lifespan)

    @mcp.tool()
    async def ping() -> PingResult:
        """Return pid/version, SDK/CLI metadata, buffer size; no CC call."""
        log.debug("ping_tool rid=%s", _request_id())
        return _ping_metadata()

    @mcp.tool()
    async def execute_task(
        job_id: JobId, task: TaskText, cwd: PathText,
        project_root: ProjectRoot | None = None,
        acceptance: list[Criterion] | None = None,
        model: str | None = None,
        effort: Effort | None = None,
        timeout_sec: PositiveSeconds = 3600,
        background: bool = False,
    ) -> ExecuteTaskResult:
        """Run one CC task without review; only a confirmed session is resumable."""
        app = await get_app()
        log.info("execute_tool job=%s rid=%s", job_id, _request_id())
        return await _execute_task_core(
            app, job_id, task, cwd, acceptance, model, effort, timeout_sec, project_root, background,
        )

    @mcp.tool()
    async def review_task(
        job_id: JobId, original_task: TaskText, acceptance: CriteriaRequired, cwd: PathText,
        project_root: ProjectRoot | None = None,
        execution_summary: str | None = None,
        model: str | None = None,
        timeout_sec: PositiveSeconds = 3600,
    ) -> ReviewTaskResult:
        """Fresh Read/Grep/Glob-only CC review; acceptance is required; never repairs."""
        app = await get_app()
        log.info("review_tool job=%s rid=%s", job_id, _request_id())
        return await _review_task_core(
            app, job_id, original_task, acceptance, cwd, execution_summary, model, timeout_sec, project_root,
        )

    @mcp.tool()
    async def continue_task(
        job_id: JobId, execution_session_id: JobId, feedback: FeedbackText, cwd: PathText,
        model: str | None = None,
        effort: Effort | None = None,
        timeout_sec: PositiveSeconds = 3600,
    ) -> ContinueTaskResult:
        """Resume this job's CC-1 session with feedback; inherits the job's root."""
        app = await get_app()
        log.info("continue_tool job=%s rid=%s", job_id, _request_id())
        return await _continue_task_core(app, job_id, execution_session_id, feedback, cwd, model, effort, timeout_sec)

    @mcp.tool()
    async def run_job(
        job_id: JobId, task: TaskText, cwd: PathText,
        project_root: ProjectRoot | None = None,
        acceptance: list[Criterion] | None = None,
        review: bool = False,
        model: str | None = None,
        effort: Effort | None = None,
        timeout_sec: PositiveSeconds = 3600,
    ) -> RunJobResult:
        """Execute; if review=true and acceptance is nonempty, add a fresh review."""
        app = await get_app()
        log.info("run_job_tool job=%s review=%s rid=%s", job_id, review, _request_id())
        spec = JobSpec(job_id=job_id, task=task, cwd=cwd, project_root=project_root,
                       acceptance=acceptance or [],
                       review=review, model=model, effort=effort, timeout_sec=timeout_sec)
        return await _run_job_core(app, spec)

    @mcp.tool()
    async def run_jobs(jobs: list[JobSpec], max_concurrency: int | None = None) -> RunJobsResult:
        """Run an already-independent job batch with bounded concurrency."""
        app = await get_app()
        log.info("run_jobs_tool n=%d rid=%s", len(jobs), _request_id())
        return await _run_jobs_core(app, list(jobs), max_concurrency)

    @mcp.tool()
    async def get_job_status(job_id: JobId, cwd: PathText) -> JobStatusResult:
        """Read persisted job state (sessions, stage statuses); no CC call."""
        app = await get_app()
        log.info("get_job_status_tool job=%s rid=%s", job_id, _request_id())
        return await _get_job_status_core(app, job_id, cwd)

    return mcp


# --- entrypoint -------------------------------------------------------------

def main() -> None:
    """Module entrypoint: ``python -m codex_claude_agent_mcp`` or the console script."""
    configure_logging()
    log.info("codex-claude-agent-mcp starting (pid=%s)", os.getpid())
    # FastMCP lifespan initializes and closes AppState in the serving loop.
    mcp = build_server()
    mcp.run(transport="stdio")
