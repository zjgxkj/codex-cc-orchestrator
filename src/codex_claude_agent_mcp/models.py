"""Pydantic models for MCP tool inputs and structured results.

Inputs are expressed as Pydantic models so FastMCP can generate a precise
input JSON schema for Codex. Results are Pydantic models returned by the tools;
FastMCP delivers them as ``structuredContent`` plus a JSON text block, which
Codex can parse directly (spec §22: do not return full transcripts).

Every result carries an optional ``error`` field. On success it is ``null``; on
a transport/semantic failure it carries the structured error object (spec §18).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints


JobId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
TaskText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200_000)]
Criterion = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
PathText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32_000)]
FeedbackText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200_000)]
CriteriaRequired = Annotated[list[Criterion], Field(min_length=1)]
PositiveSeconds = Annotated[int, Field(ge=1)]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


# --- Error ------------------------------------------------------------------

class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


# --- Inputs -----------------------------------------------------------------

_PROJECT_ROOT_DESC = (
    "Optional job-only trusted absolute root supplied by Codex; cwd must be inside it. "
    "Works outside ALLOWED_PROJECT_ROOTS and is never inferred from the MCP process cwd."
)

# Shared annotation so the tool input schema carries the description.
ProjectRoot = Annotated[PathText, Field(description=_PROJECT_ROOT_DESC)]


class ExecuteTaskInput(BaseModel):
    job_id: JobId = Field(..., description="Stable identifier for this job within the MCP instance.")
    task: TaskText = Field(..., description="The already-scoped coding task to execute.")
    cwd: PathText = Field(..., description="Absolute working directory; must be under project_root when supplied, otherwise under ALLOWED_PROJECT_ROOTS.")
    project_root: ProjectRoot | None = None
    acceptance: list[Criterion] | None = Field(None, description="Optional acceptance criteria (not enforced here).")
    model: str | None = Field(None, description="Claude model override.")
    effort: Effort | None = Field(None, description="Reasoning effort: low|medium|high|xhigh|max.")
    timeout_sec: PositiveSeconds = Field(3600, description="Per-task timeout in seconds.")


class ReviewTaskInput(BaseModel):
    job_id: JobId
    original_task: TaskText
    acceptance: CriteriaRequired = Field(..., description="At least one criterion is required.")
    cwd: PathText
    project_root: ProjectRoot | None = None
    execution_summary: str | None = None
    model: str | None = None
    timeout_sec: PositiveSeconds = 3600


class ContinueTaskInput(BaseModel):
    """Resume request. Deliberately has no project_root: a resume inherits the
    stored root and can never enlarge the job's authorization."""
    job_id: JobId
    execution_session_id: JobId = Field(..., description="Session id returned by execute_task to resume.")
    feedback: FeedbackText
    cwd: PathText
    model: str | None = None
    effort: Effort | None = None
    timeout_sec: PositiveSeconds = 3600


class RunJobInput(BaseModel):
    job_id: JobId
    task: TaskText
    cwd: PathText
    project_root: ProjectRoot | None = None
    acceptance: list[Criterion] = Field(default_factory=list)
    review: bool = False
    model: str | None = None
    effort: Effort | None = None
    timeout_sec: PositiveSeconds = 3600


class JobSpec(BaseModel):
    """A single job inside a run_jobs batch."""
    job_id: JobId
    task: TaskText
    cwd: PathText
    project_root: ProjectRoot | None = None
    acceptance: list[Criterion] = Field(default_factory=list)
    review: bool = False
    model: str | None = None
    effort: Effort | None = None
    timeout_sec: PositiveSeconds = 3600


class RunJobsInput(BaseModel):
    jobs: list[JobSpec]
    max_concurrency: Annotated[int, Field(ge=1)] | None = Field(
        None, description="Override per-process concurrency for this batch."
    )


# --- Results ----------------------------------------------------------------

class ExecuteTaskResult(BaseModel):
    job_id: str
    status: str | None = None  # COMPLETED | BLOCKED | FAILED | None (input error)
    execution_completed: bool = False
    execution_session_id: str | None = None
    execution_session_confirmed: bool = False
    summary: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None


class ReviewTaskResult(BaseModel):
    job_id: str
    status: str | None = None  # PASS | FAIL | FAILED | None (input error)
    review_session_id: str | None = None
    review_session_confirmed: bool = False
    summary: str | None = None
    # False when the review did not complete (timeout/transport/provider/
    # protocol error or input error); then unmet_criteria/evidence are null.
    # A completed PASS/FAIL keeps both arrays as before.
    review_completed: bool = False
    unmet_criteria: list[str] | None = None
    evidence: list[str] | None = None
    error: ErrorInfo | None = None


class ContinueTaskResult(BaseModel):
    job_id: str
    status: str | None = None  # COMPLETED | BLOCKED | FAILED
    execution_completed: bool = False
    execution_session_id: str | None = None
    execution_session_confirmed: bool = False
    summary: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None


class RunJobResult(BaseModel):
    """Combined execute (+optional review) result for run_job."""
    job_id: str
    execution: ExecuteTaskResult
    review: ReviewTaskResult | None = None
    status: str | None = None  # COMPLETED | EXECUTION_INCOMPLETE | REVIEW_FAILED | REVIEW_INCOMPLETE | BLOCKED | FAILED
    # Additive per-stage mirrors of the nested result statuses.
    execution_status: str | None = None
    review_status: str | None = None
    error: ErrorInfo | None = None


class JobStatusResult(BaseModel):
    """Persisted job state for post-timeout recovery (no Claude call)."""
    job_id: str
    found: bool = False
    status: str | None = None
    execution_status: str | None = None
    review_status: str | None = None
    execution_session_id: str | None = None
    execution_session_confirmed: bool = False
    review_session_id: str | None = None
    review_session_confirmed: bool = False
    execution_summary: str | None = None
    review_summary: str | None = None
    execution_error: ErrorInfo | None = None
    review_error: ErrorInfo | None = None
    created_at: float | None = None
    updated_at: float | None = None
    error: ErrorInfo | None = None


class RunJobsResult(BaseModel):
    results: list[RunJobResult] = Field(default_factory=list)
    error: ErrorInfo | None = None


class PingResult(BaseModel):
    ok: bool = True
    pid: int = 0
    version: str = ""
    sdk_version: str = ""
    cli_path: str | None = None
    cli_version: str | None = None
    max_buffer_size: int = 0
