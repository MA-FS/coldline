# Handover guide

For the team that runs, changes or extends ColdLine. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first
for the shape of the system and [docs/SECURITY.md](docs/SECURITY.md) for what must not be weakened.

## Operating it

| Task | How |
|---|---|
| Run locally | `uv sync && uv run uvicorn coldline.main:app --reload --port 8000` |
| Test | `uv run pytest -q && uv run ruff check .` (CI runs the same) |
| Deploy | `fly deploy` from this directory. One machine and one worker (the spend ledger has one writer); state is in memory, so a deploy clears visitor sandboxes. The image installs only `uv.lock` and runs the app from source. |
| Health | `GET /healthz` returns `{"status": "ok"}`; Fly checks it every 30 s. |
| Logs | `fly logs`. Uvicorn access logs; the agent records failures on the run itself (`run.extra["error"]`) and as an ERROR step. |
| Turn live runs off now | `fly secrets set COLDLINE_LIVE_ENABLED=0` (or unset `ANTHROPIC_API_KEY`). Everyone gets replay, with the reason shown on the overview. |
| Check today's spend | `$COLDLINE_DATA_DIR/agent_spend.json` (`spent_usd`, `unreconciled_usd`, `outstanding` reservations with the day each was made), and the live-status line on the overview. A call that fell back to a second model shows "2 billed attempts" on its THINK step and is settled for both. |
| Rotate the API key | `fly secrets set ANTHROPIC_API_KEY=...`; the machine restarts with the new value. |

### Runbook

| Symptom | Likely cause | Action |
|---|---|---|
| "Live runs unavailable" for everyone | Daily cap reached, kill switch on, or no key | Expected behaviour; check the reason text. Spend counts every call's reservation until it settles, and timed-out calls count in full (`unreconciled_usd`), so the cap can be reached before the provider's dashboard shows the same total. Reconcile against the provider's usage report; raise `COLDLINE_DAILY_USD_CAP` only deliberately. |
| "the spend ledger is unavailable" | `agent_spend.json` unreadable (for example truncated), invalid (valid JSON with the wrong version or fields, a malformed or future `day`, a negative or non-finite amount) or the data volume not writable | The app still starts and replays; live runs stay off (fail closed) with the reason shown on the overview. An invalid file is renamed, contents untouched, to `agent_spend.json.quarantined-<UTC time>`, and live runs stay off across restarts while that file exists. Check `fly logs` for the reason, review the quarantined file, write a valid ledger with today's figures (`{"version": 3, "day": "YYYY-MM-DD", "spent_usd": n, "unreconciled_usd": n, "outstanding": {}}`), remove the quarantined copy, then restart. An unwritable volume leaves the file in place. The older formats (`{"day", "spent_usd"}`, and version 2 with undated reservations) are migrated automatically. |
| "another process is already using the spend ledger" | A second worker or process started on the same data directory | Run one worker per data directory (the image does). The first process keeps writing; the second stays replay-only. |
| "a paid-mode setting is invalid" | A `COLDLINE_*` cost or timeout variable is not a finite number in range (for example `nan`) | `fly logs` names the variable. Fix it and restart; until then everyone gets replay. |
| "a call cost more than its reserved bound" | A reply was billed above the reservation: a model missing from `PRICES` in `agent/pricing.py`, or more attempts than the named fallbacks allow | Live runs stay off until a restart. Add the model's price to `PRICES` (or correct `COLDLINE_FALLBACK_MODELS`), then redeploy. |
| Toast "Session expired: reload the page" on Approve or Decline | The card came from a page rendered before the visitor's sandbox expired or was reset | Expected: the decision token on the card belongs to the earlier session, so nothing was decided. Reloading shows the current proposals. |
| A live run shows a "Fallback" step | Cap, timeout or API error during the run | The run continued as replay; nothing to do unless it is frequent (then check `fly logs` and API status). |
| Visitors see 429s | Per-IP limits (shared NAT, or abuse) | Tune `COLDLINE_POSTS_PER_MINUTE`, `COLDLINE_RUNS_PER_MINUTE`, `COLDLINE_NEW_SESSIONS_PER_10_MIN`, `COLDLINE_ORIGIN_RUNS_PER_DAY`. |
| Visitors see 409 "still investigating" | A fault or stale probe requested while their own run is going | Expected: the plant is not changed under a running investigation. The controls are disabled in the UI while a run streams. |
| Visitors see 503 "at capacity" | Every sandbox has an investigation running or was used in the last `COLDLINE_SESSION_PROTECT_S` (10 min), so none can be cleared for a newcomer | Raise `COLDLINE_MAX_SESSIONS` if it persists, or lower the protection window (trading newcomers' access against active visitors' sandboxes). |
| A visitor reports a fresh plant with a note that their sandbox was cleared | Their idle sandbox expired, was cleared for capacity, or was replaced by a newer one from the same address | Expected and per visitor: nobody else's sandbox is affected. |
| An old handover shows "Superseded" | The plant changed after the note (a new hold, a decided proposal, a reset) or a later investigation started | Expected: notes are snapshots. The banner lists the changes and the current holds; act on those. |
| Memory climbing | Many concurrent sandboxes | Lower `COLDLINE_MAX_SESSIONS` or `COLDLINE_SESSION_TTL_S`. Each sandbox is a few hundred KB. |
| Replay shows "No recorded run for this plant state" | A scenario without a recording | Record one (below). |
| Eval page out of date | Results file not regenerated after a change | `uv run python -m evals.run` and commit `agent/data/eval_results.json`. |

## Configuration

Everything is an environment variable with a safe default.

| Variable | Default | Effect |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables live runs. The only secret. |
| `COLDLINE_ENV` | unset | `production`: hides `/preview/*`, `Secure` cookies, HSTS. |
| `COLDLINE_SECRET_KEY` | random per process | Signs session cookies. A stable key keeps a visitor's cookie valid across a restart, but sandboxes live in memory, so their state does not survive a restart or carry between processes. |
| `COLDLINE_PREVIEWS` | on unless production | Fixture-backed page previews under `/preview/`. |
| `COLDLINE_CLIENT_IP_HEADER` | unset | Header holding the client IP from a trusted proxy (`Fly-Client-IP` on Fly). Unset = socket peer. |
| `COLDLINE_SESSION_TTL_S` / `COLDLINE_MAX_SESSIONS` / `COLDLINE_MAX_SESSIONS_PER_ORIGIN` | 7200 / 200 / 20 | Sandbox idle lifetime, capacity (least recently used evicted) and per-IP allowance. |
| `COLDLINE_SESSION_PROTECT_S` | 600 | At capacity, a sandbox used this recently is kept and the newcomer refused. |
| `COLDLINE_POSTS_PER_MINUTE` / `COLDLINE_RUNS_PER_MINUTE` / `COLDLINE_NEW_SESSIONS_PER_10_MIN` | 60 / 8 / 30 | Per-IP limits. |
| `COLDLINE_LIVE_ENABLED` | 1 | Kill switch for live runs. |
| `COLDLINE_DAILY_USD_CAP` | 3.00 | Global live spend per UTC day. |
| `COLDLINE_SESSION_RUNS_PER_HOUR` / `COLDLINE_ORIGIN_RUNS_PER_DAY` / `COLDLINE_MAX_CONCURRENT_LIVE` | 3 / 6 / 2 | Live runs per visitor per hour; per client address per day; in flight at once. |
| `COLDLINE_ORIGIN_CONCURRENT_LIVE` / `COLDLINE_ORIGIN_DAILY_USD_CAP` | 1 / 1.00 | Live runs in flight per client address; one address's share of the daily cap. |
| `COLDLINE_SERVER_FALLBACK` / `COLDLINE_FALLBACK_MODELS` | 1 / `claude-sonnet-5` for Sonnet 5.5 | Server-side fallback on a refusal, to the named models only (at most three). Every attempt is reserved and settled. |
| `COLDLINE_MAX_MODEL_CALLS` / `COLDLINE_MAX_TOKENS_PER_CALL` | 8 / 3000 | Bounds per run and per call. |
| `COLDLINE_RUN_TIMEOUT_S` / `COLDLINE_CALL_TIMEOUT_S` | 90 / 40 | Wall-clock bounds. |
| `COLDLINE_MODEL` / `COLDLINE_EFFORT` / `COLDLINE_THINKING` | `claude-sonnet-5-5` / `medium` / per model | Model settings. A cost or timeout value that is not a finite number in range switches live runs off. |
| `COLDLINE_REPLAY_SPEED` | 1.0 | Replay pacing; 0 is instant. |
| `COLDLINE_DATA_DIR` | `/data` in the image | Where the daily spend ledger is kept (journalled, replaced atomically). |

## Connecting a real system

The agent talks only to `coldline.contracts.PlantSystems`. To connect real systems, write an adapter class
that implements the protocol and return it from `SessionManager` (or a replacement keyed by site and user
instead of browser session).

1. **Reads first.** Implement `get_trend`, `latest`, `list_alarms`, `get_asset` against a historian replica or
   read-only API, never the control network. Return `Quality.STALE`/`BAD` honestly: the evidence checks depend
   on it.
2. **Then one write at a time,** each behind its existing tier: `place_qa_hold` (QA system),
   `create_work_order` / `amend_work_order` (CMMS; keep `expected_version` optimistic locking),
   `change_dispatch` (logistics), `draft_permit` (permit system, drafts only).
3. **Audit** to a durable, append-only store. The agent and the approval store write `AuditEvent`s through
   `audit()`.
4. **Shadow mode before action.** Remove the AUTO write scopes (`qa:hold`, `cmms:create`, `permits:draft`)
   from `agent/context.AGENT_SCOPES` and keep `proposals:create`, `handover:write` and `security:flag`, so the
   agent investigates, writes handovers and proposes, but changes no record by itself; leave proposals
   undecided (or connect adapters whose writes go nowhere) and compare its handovers with what supervisors
   did. Restore scopes one at a time.
5. Add the adapter to the contract tests in `tests/systems/` so it is held to the same behaviour as the mock.

Moving beyond one process: replace the in-memory run/approval stores and rate limiters with a database and
Redis (the interfaces are small: `AgentService` sessions, `ApprovalStore`, `BudgetGuard`, `RateLimiter`),
and take user identity and roles from the site's identity provider instead of the role switch.

## Adding a tool

1. In `agent/tools.py`, write a Pydantic input model (`extra="forbid"`) and a handler that returns a
   `ToolResult` (or a `Proposal` for APPROVE tier).
2. Register a `Tool(...)` with its tier and scope; add the scope to `AGENT_SCOPES`.
3. APPROVE tier also needs an executor in `approvals.EXECUTORS` and roles in `APPROVER_ROLES`, and its
   `Source`s so the approval binds to the right record versions.
4. Add unit tests in `tests/agent/`, then run `uv run python -m evals.run --live`. Tool descriptions are
   part of the cached prompt, so a change alters `prompt_version`: re-record (below).

## Adding a scenario

1. Add the value to `contracts.Scenario` and its physics in `sim/plant.py` (`Simulator._sample` and
   `inject`), with a label in `web/views.SCENARIO_LABELS`.
2. Add an eval case with its checks and a `RecordingSpec` in `evals/cases.py`. Checks assert on business
   systems and confidence, never wording.
3. Record it: `uv run python -m evals.record <id>` (needs `ANTHROPIC_API_KEY`; a few cents). The recorder
   refuses to save a run that fails its checks.
4. `uv run python -m evals.run` to refresh the Evals page, then `uv run pytest -q`.

## Changing the UI

The design rules (ISA-101: colour means abnormal, stale is not a value) and every component are in
[docs/DESIGN.md](docs/DESIGN.md), with a live catalogue at `/styleguide`. Templates never compute state;
view logic lives in `web/views.py` and `web/context.py` where it is unit-tested. Pages use no inline
scripts (the CSP forbids them); put behaviour in `static/js/app.js`.
