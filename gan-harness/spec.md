# MVD Vertical Slice Specification

Source of truth: `plans/Multi-Agent软件开发智能助手_MVP设计蓝图.md` v0.2.

## Locked scope

- Local, single-user FastAPI service bound to `127.0.0.1`.
- One persistent SQLite run store and one in-process background worker.
- Four logical roles: Manager, Developer, Tester, Reviewer.
- The initial delivery uses a deterministic mock LLM provider so tests do not require network credentials.
- Target project access is constrained by a frozen project profile and copied workspace.
- The original `student-management` directory must never be modified.
- No Git initialization, commit, push, QQBot, RAG, Redis, multi-tenant auth, or custom frontend in the MVD.

## Thin vertical slices

### Slice 1 - Persistent mock run

Create a run through FastAPI, persist it, let the background worker claim it with a fencing token, produce a structured Manager plan, and pause at `waiting_approval`. A restart must preserve the run and plan.

### Slice 2 - Approval and protected workspace

Approve the exact `plan_hash + project_profile_hash`, create an allowlist-only workspace copy, reject stale approvals and any Manager attempt to expand file permissions.

### Slice 3 - Change, test, repair

Apply a structured ChangeSet in a generation-isolated copy, run immutable baseline and acceptance tests through a runner abstraction, feed a failed result back to Developer at most twice, and preserve the original project hash.

Production execution is Docker-only and must never silently fall back to host execution.

### Slice 4 - Review and report

Run a deterministic policy gate, obtain a structured Reviewer decision, terminate with `completed` or `needs_human`, and expose diff, events, metrics, and artifact downloads by artifact ID.

## Required contracts

- `TaskPlan`
- `ChangeSet` / `FileChange`
- `TestResult`
- `ReviewDecision`
- `RunSummary`
- Explicit run/status transition model

## Required security invariants

- Frozen `project_profile_hash` and effective glob intersection.
- Lease fencing generation checked before every published side effect.
- Generation-specific workspaces and checkpoints prevent stale-worker interference.
- Canonical path checks and no symlink/reparse-point traversal.
- No arbitrary shell strings from a model.
- Artifact IDs never accept raw paths; artifact content is hash-verified when read.
- Secrets and `.env*` are never copied.
- Existing baseline tests and external acceptance tests are immutable.
- Control API requires loopback access, a trusted Host header, and a Bearer token.

## Golden behavior

The target task is a health endpoint for the student-management backend:

- DB healthy: `GET /api/v1/health` returns HTTP 200 and `{"status":"ok","database":"ok"}`.
- DB failure: HTTP 503 and `{"status":"degraded","database":"error"}` without leaking internals.
- Existing login, student-list, and class-list behavior remains green.

## Exit criteria

- Unit and integration tests pass.
- Coverage for the assistant package is at least 80%.
- OpenAPI can create, inspect, approve, cancel, and retrieve the output of a run.
- A real API-to-Docker run reaches `completed` through immutable tests and review.
- The original target directory is unchanged by an end-to-end run.
- No unresolved Critical or High findings remain.
