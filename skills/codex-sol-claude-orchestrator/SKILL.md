---
name: codex-sol-claude-orchestrator
description: Route non-trivial repository features, debugging, refactors, tests, algorithms, migrations, and data/model work between Codex and Claude Code (CC). Trigger before broad exploration; delegate concrete verifiable work while Codex keeps material decisions, critical reasoning bottlenecks, failed-repair takeover, and final acceptance. Not for discussion-only requests or tiny mechanical edits.
---

# Codex / CC orchestration

CC means the Claude Code harness. Optimize useful offload and Codex context, not call count.

## Route once, early

- Keep only discussion/planning, tiny understood mechanical edits, and work CC cannot access local.
- Otherwise delegate one coherent package when scope, material constraints, and acceptance are clear enough for independent investigation. Unknown files, root cause, or difficulty do not disqualify it; “Codex can do it” is not a reason to keep it.
- Codex resolves user intent and material product, public-contract, architecture, permission/safety, consistency, compatibility, irreversible-migration, and modeling-assumption decisions. CC makes reversible local choices and handles concrete investigation/implementation.
- Do not explore the repository or solve the task merely to prepare delegation. If a decisive bottleneck clearly exceeds CC, Codex solves only that part and delegates the stable remainder; otherwise give CC a bounded reproduction, hypothesis test, counterexample, baseline, prototype, or measurement.
- Re-route only for a real blocker, scope change, or new evidence. For difficult algorithms/modeling, visuals, critical bottlenecks, or ineffective repairs, read [capability boundaries](references/capability-boundaries.md).

## Dispatch

Send only `job_id`, `cwd`, goal, allowed scope, material constraints, inaccessible facts/artifacts, and checkable acceptance; CC locates files. Request summary, changed/evidence paths, checks, and blockers—not code dumps, full logs, or hidden reasoning.

Always pass `project_root` as the trusted active Codex workspace root or the explicitly selected project root. `cwd` must equal it or be inside it. Never infer it from the MCP process, guess it, or pass a drive root. It temporarily authorizes only that job even outside `ALLOWED_PROJECT_ROOTS`; review inherits it and continue cannot widen it.

Tell the executor: **Run necessary validation proportional to scope/risk. For a Bug with unclear trigger or cause, reproduce before editing and rerun the same condition; an existing reliable failing test or clear error evidence already counts. Report exact checks and unverified outcomes; never claim an unrun check.**

## Two assurance layers

1. **Fresh CC-2 review:** default for substantive behavior or logic changes, whoever implemented them. Skip read-only work and truly mechanical behavior-preserving edits unless requested. Give the original task, acceptance, and scoped baseline; CC-2 uses Read/Grep/Glob only, cites actionable evidence, and never repairs.
2. **Codex final acceptance:** always judge the user goal, constraints, evidence, findings, and deliverable state. Do not repeat routine tests/review; inspect deeper or run a key check only when evidence is missing or contradictory, review FAIL remains unresolved, risk is high, repair repeatedly failed, or the goal may be misunderstood.

Executor validation is implementation, not a third review layer. Resume CC-1 for fixes or targeted runtime evidence, then use a fresh CC-2 for substantive changes.

## Run and recover

- Use `run_job(review=true)` for a short, ready substantive package. For unknown-scope or long implementation/test work, use `execute_task`, inspect its persisted outcome, then `review_task`; `execute_task` never reviews. Use `review=false` only intentionally.
- On timeout or lost response, call `get_job_status(job_id, cwd)` and inspect the workspace. Continue only when `execution_session_confirmed=true`; an allocated but unconfirmed session is not resumable. Retry a transient unconfirmed execution with a new job ID. Recover `REVIEW_INCOMPLETE` with a fresh `review_task` on the same job.
- Never retry billing 402, authentication, invalid request, or protocol/invalid-output failures unchanged. Retry transient network, timeout, rate-limit, and provider 408/5xx/529 failures only a bounded number of times.
- Use `continue_task` while evidence and causal progress advance. If the same failure persists, changes mask symptoms/create regressions, evidence stalls, or a material reasoning issue appears, Codex takes the minimum decisive part. Transport/tool failure is not model incapability.
- A `job_id` stays bound to its exact task, acceptance, resolved `cwd`, and root; changed scope/root requires a new ID. Review is fresh; continue resumes only that job's confirmed CC-1 session. Use `run_jobs` only for independent packages with stable interfaces and disjoint writes.
- Use `ping` only to diagnose MCP/SDK/CLI/buffer versions; it makes no provider call. Discover tools once. Preserve user model, permission, and safety restrictions; prompt scope is not an OS sandbox.
