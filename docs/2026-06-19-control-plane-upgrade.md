# 2026-06-19 — Control-plane upgrade

Goal: add a lightweight control plane (budgets, audit, routines, governance,
isolated self-modification) to AI_Agents *with our own design* — staying
Telegram-native and self-contained.
Each feature reuses existing primitives where possible (we already have
`AuditLog`, `activity_feed`, a `QuotaCounter` callback, tasks/timeline, a
contextvar approval bridge, and a lifespan for background services).

Everything new is **OFF by default** behind a settings flag and degrades safely.

## Feature 0 — Make `run_all.py` robust (fix the suite hang)
`_get_team_app()` keeps a process-alive resource, so a test that builds the team
app prints `OK` but never exits, hanging the whole suite. Fix the *runner*: per-test
timeout; a test that printed its success marker before the timeout counts as pass.

## Feature 1 — Cost & budget tracking with hard-stop  (`src/budget.py`)
- Tables: `CostEvent` (agent, provider, model, in/out tokens, cost_usd, task_id, ts),
  `BudgetPolicy` (scope=global|<agent>, limit_usd, window=day|month|lifetime,
  warn_percent, hard_stop, enabled).
- A price table per model ($/1M in/out); `:free` and unknown → 0.
- `CostCallback(BaseCallbackHandler)` attached to **every** model in `_make_model`;
  attributes spend to a `budget` contextvar set by the orchestrator (ceo/specialist).
- `gate(agent)` → ok|warn|blocked; enforced in `_ceo_node` / `_specialist_node`
  when `enable_budget` is on (recording always happens — it's cheap and useful).
- API: `GET /api/costs`, `GET/POST /api/budgets`. UI: a Costs card.

## Feature 2 — Unified activity / audit log  (`src/activity.py`)
- Thin `log(actor, action, target, **details)` wrapping the existing `AuditLog`
  write + live hub publish (generalizes `tools._audit`).
- Route the important new events through it: budget warn/block, approvals decided,
  routine fired, self-modify run. `activity_feed` + `/api/activity` already exist;
  add a `costs` slice.

## Feature 3 — Routines / heartbeats  (`src/routines.py`)
- Table `Routine` (name, schedule_kind=interval|daily|weekly, schedule_value,
  prompt, target=team|<agent>, enabled, last_run_at, next_run_at, catch_up).
- `RoutineScheduler` async loop (mirrors `ProactiveService`): every tick, run due
  routines → create a collab task → run the team/agent → post result to team chat.
  Coalescing (overdue + no catch_up → run once, jump to next future slot).
- No new dependency: interval/daily/weekly computed by hand (cron-expr is a later add).
- Started in the app lifespan; CRUD + `GET/POST/DELETE /api/routines` + Run-now; UI card.

## Feature 4 — Git-worktree isolation for self-modify  (`src/selfmod.py`)
- Upgrade the `maintainer` flow: the orchestrator creates a `git worktree` on a
  fresh branch in a temp dir, points the self-edit tools at THAT path, the
  maintainer edits + runs tests there, and we append the branch + `git diff --stat`
  to its report. The live bot's working tree is never touched. Falls back to the
  current in-place branch flow if git/worktree is unavailable.
- `set_self_edit(on, root=...)` gains an optional explicit root.

## Feature 5 — Typed approvals  (`src/approvals.py`)
- Table `Approval` (kind=shell|self_modify|budget_override|risky_delete, summary,
  status, requested_by, decided_by, reason, ts).
- `request_approval(kind, summary, agent=…)` records pending → asks via the existing
  Telegram asker → records decision → returns bool. `request_command_approval`
  becomes `request_approval("shell", …)` (backward compatible).
- API: `GET /api/approvals`. Each decision lands in the activity log.

## Cross-cutting
- New settings (all default-off): `enable_budget`, `enable_routines`,
  `self_worktree`, plus tuning knobs.
- `registry.setup()` already creates new tables via `SQLModel.metadata.create_all`
  (import the new models so they register).
- Tests: one focused `tests/test_*.py` per feature, plain-assert style, no network.
- UI: compact API-backed cards in `index.html` (Costs / Routines / Approvals).

## Order of work
0 → 1 → 2 → 3 → 5 → 4, then UI, then full suite green.
