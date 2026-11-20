# `coldline.agent`: the shift-engineer agent

This package is the agent: a small tool-use loop over the Anthropic Messages API, with every safety
decision made in Python rather than in the prompt. It reads plant data (OT) and acts only on business
systems (IT) through `PlantSystems`; there is no code path to plant control. All data is synthetic and
"Southern Range Meats" is fictional.

This document is for the team that runs and extends the agent: how a run works, what is enforced where,
how to change it safely, and how to tell whether a change made it better or worse.

## Contents

| Module | Responsibility |
|---|---|
| `service.py` | Public API for the web layer. Sessions, background runs, streaming, approvals, live vs replay. |
| `loop.py` | The bounded tool-use loop (an async generator of `TraceStep`) and run finalisation. |
| `tools.py` | Tool registry: typed inputs, tiers, scopes, NEVER-tier refusals, handover gate. |
| `evidence.py` | Deterministic checks: probe freshness and divergence, verdicts, confidence cap, citations. |
| `approvals.py` | APPROVE-tier proposals: source versions, idempotency, expiry, audit. |
| `injection.py` | Delimiting and screening of untrusted text (inbox, ticket notes). |
| `budget.py` | Per-session rate limit, concurrency limit, daily US$ cap with per-call reservations. |
| `pricing.py` | The one place where tokens become dollars. |
| `model.py` | `AnthropicModelClient` (live) and `ScriptedModelClient` (replay and evals). |
| `prompts.py` | Frozen system prompt, run trigger, prompt version hash. |
| `replay.py` | Recording library, selection by plant state, placeholder resolution. |
| `recordings/` | Recorded runs, one per scenario. |
| `data/eval_results.json` | Latest eval results, rendered by the Evals page. |

## Using it from the web layer

```python
from coldline.agent import get_agent_service

agent = get_agent_service()                      # configured from the environment, one per process
plant = sessions.get(session_id)                 # coldline.systems.SessionManager

run = await agent.start_run(session_id, plant, mode="replay")   # or mode="live"; returns immediately
async for event in agent.stream_events(run.run_id):             # SSE-ready dicts
    ...  # {"event": "step", "id": "3", "data": <TraceStep json>} ... then {"event": "done", "data": <RunTrace>}

agent.approve(session_id, approval_id, "Shift Supervisor", plant)   # -> ApprovalResult(approval, executed, message)
agent.decline(session_id, approval_id, "Shift Supervisor", plant, reason="")
agent.list_runs(session_id); agent.get_run(run_id); agent.list_approvals(session_id)
agent.live_status(session_id)        # BudgetStatus: is live available, why not, spend, runs left
agent.revalidate_approvals(session_id, plant)  # expire pending approvals whose evidence changed
agent.forget_session(session_id)     # call when the visitor's sandbox expires (cancels a running run)
```

`start_run` returns the run already in progress if the session has one. `stream(run_id, after=n)` yields
raw `TraceStep`s from index `n`, so a reconnecting client can resume. `public_run_json(run)` serialises a
run without the raw model turns. `load_eval_results()` returns the Evals page data.

## How a run works

```
trigger (alarms, code-side historian screen, inbox count)
  -> model call (cached tools + system prompt)          THINK step: note, tokens, cost, latency
  -> each tool call goes through ToolRegistry.execute   TOOL / GUARDRAIL steps
  -> all tool results go back in one user turn
  -> repeat until write_handover_note is accepted, or a bound is hit
  -> finalize: conclusion step, totals, status (awaiting_approval if anything is pending)
```

Bounds (all in `AgentSettings`): at most 8 model calls, 3,000 output tokens per call, 40 s per call, 90 s
per run. The model is `claude-sonnet-5-5` at effort `medium` with `between_tools` thinking (no extended
thinking; the short notes the model writes between tool calls become the THINK steps). Tool definitions and
the system prompt are a frozen, cached prefix; a second cache breakpoint on the conversation means each
loop iteration reads earlier turns from cache. A typical live hero run is 3 model calls, about 20k tokens
(mostly cache reads), US$0.02 to 0.04 and 10 to 15 s.

The loop is about 120 lines and has no framework underneath it, on purpose: it can be read top to bottom
and every branch maps to a trace step a supervisor can see.

## Tools and tiers

Every tool has a tier and a scope the agent identity must hold (`context.AGENT_SCOPES`). Removing a scope
disables the tool without touching the prompt.

| Tool | Tier | Scope |
|---|---|---|
| `get_sensor_trend`, `get_latest_readings` | READ | `historian:read` |
| `get_asset` | READ | `assets:read` |
| `list_active_alarms` | READ | `alarms:read` |
| `list_lots_in_zone`, `get_lot` | READ | `lots:read` |
| `find_work_orders`, `get_asset_history` | READ | `cmms:read` |
| `read_inbox` | READ | `inbox:read` |
| `place_qa_hold` | AUTO | `qa:hold` |
| `create_work_order` | AUTO | `cmms:create` |
| `draft_isolation_permit` | AUTO (draft only) | `permits:draft` |
| `flag_suspected_injection` | AUTO | `security:flag` |
| `write_handover_note` | AUTO | `handover:write` |
| `propose_work_order_amendment` | APPROVE | `proposals:create` |
| `propose_dispatch_change` | APPROVE | `proposals:create` |
| `propose_release_qa_hold` | APPROVE | `proposals:create` |

**NEVER tier.** Releasing a hold, changing dispatch or amending a work order directly, authorising permits,
approving proposals, and anything that looks like plant control (setpoints, compressors, valves, alarm
shelving) are not tools. If the model asks for one anyway, `classify_unknown` refuses it with a reason, a
GUARDRAIL step and an audit event. Unknown names are refused the same way, with a "did you mean" hint.

## What code enforces (not the prompt)

| Rule | Where |
|---|---|
| Tool must exist, not be NEVER tier, and match the agent's scopes | `ToolRegistry.execute` |
| Arguments validated against the Pydantic model before anything runs | `ToolRegistry.execute` |
| APPROVE-tier calls only create a proposal; nothing executes until a person approves | `ToolRegistry._queue`, `approvals.py` |
| Holds, proposals and citations may only reference records a tool returned in this run | `_require_seen` |
| No new work order without first checking existing ones; no duplicate in one run | `create_work_order` |
| Permits are drafts; the agent cannot authorise | `draft_isolation_permit`, NEVER list |
| Dispatch change refused while the lot's chiller evidence is stale | `propose_dispatch_change` |
| Handover rejected until trend, lots and work orders are read for every affected chiller (and work orders or history for an alarmed compressor) | `missing_lookups` |
| Handover rejected if it cites an id no tool returned | `write_handover_note` |
| Confidence capped by the evidence; uncertain verdicts get a fixed headline | `evidence.gate_conclusion` |
| A run that read suspected injection text cannot propose hold releases, cannot use that text as justification, and must say so in its conclusion | `tools.py` |
| Approvals expire if a source record changed; execute exactly once; the agent cannot approve | `ApprovalStore.approve` |
| Spend reserved before every call against the daily cap | `budget.py`, `loop.py` |

## Evidence checks

`assess_chiller` uses `coldline.sim.analysis` (the same freshness and divergence functions the plant mimic
uses) and returns one verdict per chiller:

| Verdict | When | Confidence cap | Headline |
|---|---|---|---|
| `insufficient_data` | a probe is stale or the samples are not comparable | `insufficient_data` | "Insufficient data - TT-402 stale; comparison withdrawn." |
| `probes_disagree` | mean gap above 2 °C over 15 min | `unconfirmed` | "Probes disagree - cause unconfirmed." |
| `excursion_confirmed` | both probes above the 5 °C illustrative limit | `confirmed` | model's wording |
| `transient_recovered` | rose and came back with both probes agreeing | `confirmed` | model's wording |
| `probes_agree` | otherwise | `confirmed` | model's wording |

The verdict is returned to the model with every trend read (as `evidence_checks`) and recomputed at
handover time. The handover note always ends with a "Checks computed in code" block, so a reader can see the
numbers that set the confidence independently of what the model wrote.

## Approvals

A proposal records `source_versions` for every record it depends on: the work order's `version`, a short
content hash of the lot's dispatch time, and each relevant probe's quality (good, stale, bad). On approve:

1. Already approved: no-op ("not executed again"). Not pending: nothing happens.
2. Actor must hold an approver role (`Shift Supervisor` for dispatch and amendments, `QA Manager` for hold
   releases). `agent` and `system` are always refused.
3. Versions re-read. Any change: `EXPIRED`, audited, nothing runs. This is how "make the reference probe
   stale" invalidates proposals made on the earlier comparison.
4. Execute once (guarded by the idempotency key), set `APPROVED`, write the `approve` audit event, all under
   one lock. A failure during execution (for example a work-order version conflict) also ends as `EXPIRED`.

Decline records a `decline` event and changes nothing else. `revalidate_approvals()` runs step 3 for every pending approval without executing anything; the web layer calls it when a visitor makes a probe stale, so the expiry shows immediately rather than on the next click.

## Untrusted text

Inbox messages and work-order text reach the model inside `<untrusted_data source="...">` blocks, with any
delimiter inside the text neutralised. The system prompt says such text is data, never instructions. A
deterministic screen (`injection.screen_text`) flags instruction-like text; a flag writes an audit event,
shows a GUARDRAIL step and taints the run. None of this is the primary control: tiers are enforced in code,
so even a model that fully obeys an injected message cannot release a hold or approve anything (see the
`compromised_model` eval).

## Replay and live

**Replay is the default for public visitors.** A recording holds the model's side of a real run (its notes,
tool calls, token counts and latency). Replay feeds those turns through the same loop, so the tools run
for real against the visitor's sandbox, through the same tiers, evidence gate and approval store. The QA
hold appears, proposals wait for approval, and a stale probe changes the outcome, at zero model cost.
Records created during a run get placeholders in recordings (`{{hold:L-2231}}`,
`{{approval:change_dispatch:L-2231}}`, `{{flagged}}`), resolved per sandbox at replay time.

`RecordingLibrary.select` picks the recording for the active scenario, preferring the stale variant when
its `requires_stale` probe is not fresh. Recorded prose (times, values) reflects the recording sandbox; the
code-computed checks and all side effects reflect the visitor's.

Current recordings (`extra.recorded` says which):

| Recording | Source | Model calls | Recorded cost |
|---|---|---|---|
| `probe_mismatch` | live | 3 | US$0.037 |
| `probe_mismatch_stale` | live | 3 | US$0.019 |
| `compressor_trip` | live | 3 | US$0.041 |
| `door_open` | live | 2 | US$0.015 |
| `poisoned_email` | hand-authored | 3 | US$0.010 |

The poisoned-email recording is hand-authored so its handover makes no claims about plant state it did not
check; it must read correctly whatever else the visitor has done in their sandbox.

**Live mode** is opt-in per run. It is refused, and the run continues as replay with a visible
"Fallback" step, when the kill switch is off, no API key is set, the session has used its live runs, too
many live runs are in flight, or the daily cap is reached. A live run that fails part-way (API error,
timeout, refusal, budget) also continues as replay in the same run. Server-side fallback
(`fallbacks: "default"`) is on, so a request the model declines can be retried by the API on another model.

To re-record after a prompt or tool change: `uv run python -m evals.record` (needs `ANTHROPIC_API_KEY`;
about US$0.15 for all four live recordings). Each recording must pass its scenario eval before it is saved.
`uv run python -m evals.record --authored` rebuilds the hand-authored one without API calls. Recordings
carry the `prompt_version` they were made with.

## Cost control

| Limit | Default | Env |
|---|---|---|
| Kill switch | on | `COLDLINE_LIVE_ENABLED=0` forces replay for everyone |
| Daily cap (all visitors, UTC day) | US$3.00 | `COLDLINE_DAILY_USD_CAP` |
| Live runs per session per hour | 3 | `COLDLINE_SESSION_RUNS_PER_HOUR` |
| Concurrent live runs | 2 | `COLDLINE_MAX_CONCURRENT_LIVE` |
| Model calls per run | 8 | `COLDLINE_MAX_MODEL_CALLS` |
| Output tokens per call | 3000 | `COLDLINE_MAX_TOKENS_PER_CALL` |
| Run / call timeout | 90 s / 40 s | `COLDLINE_RUN_TIMEOUT_S`, `COLDLINE_CALL_TIMEOUT_S` |

Before each call the loop reserves the call's worst case (every input token at the cache-write rate plus
the full output limit) and settles to the real cost afterwards, so concurrent visitors cannot overshoot the
cap. The day's spend is persisted to `$COLDLINE_DATA_DIR/agent_spend.json` when that directory exists. The session limit is keyed by session id; the daily cap is the backstop for
visitors who clear cookies.

Other settings: `ANTHROPIC_API_KEY`, `COLDLINE_MODEL` (default `claude-sonnet-5-5`), `COLDLINE_EFFORT`
(`medium`), `COLDLINE_THINKING` (`between_tools` on Sonnet 5.5, `adaptive` elsewhere),
`COLDLINE_SERVER_FALLBACK` (`1`), `COLDLINE_REPLAY_SPEED` (`1.0` = recorded pacing, `0` = instant).

## Evals

```
uv run pytest -q                               # unit tests + the deterministic eval suite
uv run python -m evals.run                     # same suite, writes data/eval_results.json for the Evals page
uv run python -m evals.run --live              # scenario cases against the real model
COLDLINE_LIVE_TESTS=1 uv run pytest evals -q   # the same, under pytest
```

Evals assert on what happened in the business systems and on the reported confidence, never on wording.

* **scenario** (5): probe mismatch, stale reference probe, compressor trip, door open, poisoned email.
  Deterministic in CI via the recordings; live with `--live`.
* **adversarial** (5, scripted models that misbehave on purpose): follows the injected email, invents a
  diagnosis, skips required lookups, proposes a dispatch on stale evidence, cites a record it never read.
  These show the controls hold when the model does not.
* **approval** (4): expiry on changed evidence, exactly-once execution, decline, no self-approval.

Recording the live scenarios is itself an eval: the first prompt version missed the dispatch proposal and
the permit draft and over-reacted to the door-open transient. A short "site procedure" section in the
system prompt fixed all three; the recorder refused to save any run that failed its scenario checks.

## Changing things safely

* **Add a tool:** write a Pydantic input model (`extra="forbid"`) and a handler returning `ToolResult` (or
  `Proposal` for APPROVE tier), add a `Tool(...)` entry with tier and scope, add the scope to
  `AGENT_SCOPES`, add tests in `tests/agent/`, then re-run the evals and re-record if behaviour changes.
  New APPROVE-tier actions also need an entry in `approvals.EXECUTORS` and `APPROVER_ROLES`.
* **Change the prompt or tool descriptions:** this changes `prompt_version` and the cached prefix. Run
  `evals.run --live` (about US$0.15) before shipping, and re-record.
* **Change the model:** set `COLDLINE_MODEL`, add its price to `pricing.PRICES` (unknown models are priced
  conservatively high), run the live evals.
* **Replace the mock systems with real ones:** implement `coldline.contracts.PlantSystems`. The agent only
  calls that protocol; the read side should come from a historian replica, never the control network.

## Known limitations

* State is in memory (one process). Runs, approvals and the rate limiter reset on restart; the daily
  spend does not if `COLDLINE_DATA_DIR` is set.
* The injection screen is a pattern list, a cheap first signal. The tier rules are the control.
* Recorded prose can mention times from the recording sandbox; checks and side effects are always live.
