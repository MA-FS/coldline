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
| `approvals.py` | APPROVE-tier proposals: evidence binding, idempotency, expiry, audit. |
| `handover.py` | Handover integrity: the code-composed disposition and checks, the filtered model notes, replay check. |
| `injection.py` | Delimiting and screening of untrusted text (inbox, ticket notes). |
| `budget.py` | Per-session and per-address limits, concurrency, daily US$ cap with journalled per-call reservations. |
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
async for event in agent.stream_events(session_id, run.run_id): # SSE-ready dicts
    ...  # {"event": "step", "id": "3", "data": <TraceStep json>} ... then {"event": "done", "data": <RunTrace>}

agent.approve(session_id, approval_id, "Shift Supervisor", plant)   # -> ApprovalResult(approval, executed, message)
agent.decline(session_id, approval_id, "Shift Supervisor", plant, reason="")
agent.list_runs(session_id); agent.get_run(session_id, run_id); agent.list_approvals(session_id)
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
| A QA hold must cite a reading, alarm or asset record for the chiller the lot is in, and never untrusted text | `place_qa_hold` |
| No new work order without first checking existing ones; an open agent-raised order (or permit draft) is reused, not duplicated, even across runs and a live-to-replay fallback | `create_work_order`, `draft_isolation_permit` |
| Permits are drafts; the agent cannot authorise | `draft_isolation_permit`, NEVER list |
| Dispatch change refused while the lot's chiller evidence is stale | `propose_dispatch_change` |
| Handover rejected until trend, lots and work orders are read for every affected chiller (and work orders or history for an alarmed compressor) | `missing_lookups` |
| Handover rejected if it cites an id no tool returned | `write_handover_note` |
| Confidence capped by the evidence; uncertain verdicts get a fixed headline | `evidence.gate_conclusion` |
| The handover's disposition (holds, shipping, proposals waiting, required follow-ups) is composed in code; the model's notes may not state those decisions and lose sentences the checks contradict | `handover.py` |
| A run without an accepted handover is marked `incomplete`, never shown as a finished investigation | `loop.finalize` |
| A run that read suspected injection text cannot propose hold releases, cannot use that text as justification, and must say so in its conclusion | `tools.py` |
| Approvals expire if their evidence changed (records, probe quality, verdict, incident) or the action's precondition no longer holds; execute exactly once; approve and decline need the same role; the agent cannot decide | `ApprovalStore` |
| An upper bound on every call's cost reserved and journalled before the call; unknown outcomes keep the reservation | `budget.py`, `loop.py` |

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
handover time.

## Handover integrity

What a person reads, and copies with "Copy as text", cannot contradict what code enforces (`handover.py`).
The note has three sections, in this order:

1. **Disposition (set in code from the plant records)**: the evidence headline and its confidence
   (`evidence.code_conclusion`), the fixed rule "Product disposition: QA decision. Held lots stay held until
   QA releases them.", one line per lot the run looked at (on QA hold with the hold id and "do not ship or load it until QA releases the
   hold", or not on hold, with a warning when that lot's evidence is uncertain or shows an excursion), the
   proposals waiting with their approver, and the follow-ups the evidence requires. Every line is a
   template filled from evidence checks, lot records, holds and approvals; no model text reaches it on any
   path, including confirmed evidence and runs with no chiller evidence. The agent never declares stock fit
   for sale: release is QA's decision.
2. **Agent narrative (model-written, not authoritative)**: the model's observations. Any sentence, bold
   lead or section that states a product, hold or approval decision is moved to the trace (section 1 says
   it from the records), and sentences the checks contradict are removed: a cause stated while the verdict
   is uncertain, a claim that something was released or approved, an empty inbox that is not, no alarms
   while alarms are active, nothing awaiting approval while proposals are pending, or text imitating a
   code section. Everything left out is listed in the trace and in `run.extra["gate"]["removed"]`. This
   filter only tidies the narrative; section 1 does not depend on it.
3. **Checks computed in code (not by the model)**: active alarms, the evidence check per chiller, work
   orders and permit drafts, decided proposals, and what the run did with any untrusted input (derived from
   its recorded steps, not asserted).

Replays also check they are describing the same plant. See "Replay and live".

## Approvals

A proposal records `source_versions`, one categorical value per piece of evidence it relied on: the work
order's or lot's `version` (a lot's moves on every hold, release and dispatch change), each relevant
probe's quality (good, stale, bad), the chiller's evidence verdict, the exact QA hold a release would lift,
and the plant's incident generation (moved by every fault injection and reset). Sampling noise moves none
of these. On approve, inside one sandbox transaction:

0. A decision from the web must carry the card's decision token: an opaque value issued with that proposal
   in that session generation. Display ids restart when a visitor's sandbox expires, so a stale tab could
   otherwise name a newer AP-0001; with the wrong token the decision is refused ("session expired, reload")
   and nothing changes. A reset starts a new generation too.
1. Already approved: no-op ("not executed again"). Not pending: nothing happens.
2. Actor must hold an approver role, matched exactly (`Shift Supervisor` for dispatch and amendments,
   `QA Manager` for hold releases). `agent` and `system` are always refused. Decline needs the same role.
3. Evidence re-read and the action's precondition re-checked (a new dispatch time must still be ahead).
   Any change: `EXPIRED` with the reason in `approval.note`, audited, nothing runs. This is how "make the
   reference probe stale" or injecting a new fault retires proposals made on the earlier evidence.
4. Execute once (guarded by the idempotency key), set `APPROVED`, write the `approve` audit event, all under
   one lock. A failure during execution (for example a work-order version conflict) also ends as `EXPIRED`.

`revalidate_approvals()` runs step 3 for every pending approval without executing anything. It runs after
every fault injection, stale probe, decision and finished run, so expiry shows as soon as evidence changes.
A later run proposing the same action on the same, still-valid evidence reuses the pending approval and adds
its id to `run_ids`; one decision then settles every run that asked for it. `run_ids` is trimmed to the
runs the session still keeps (the originating run always stays) and `run_count` keeps the total, so a
proposal shared by many runs never holds references to runs that are gone.

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
`{{approval:change_dispatch:L-2231}}`, `{{flagged}}`), and so do alarms (`{{alarm:C2}}`, the alarm active
on C2 in this plant), resolved per sandbox at replay time.

`RecordingLibrary.select` picks the recording for the active scenario, preferring the stale variant when
its `requires_stale` probe is not fresh. A poisoned email injected while an earlier fault's alarm is still
active replays the investigation of that fault (those recordings read the inbox too), because a handover
that skipped the active alarm would be rejected.

Visitors chain scenarios, so a replay often meets a plant the recording never saw: a lot already on hold,
an email in the inbox, an approved dispatch change. Each recording stores `handover_facts`, an id-free
summary of what the recorded run observed and did. At handover the replay compares its own; if they differ,
the recorded wording described a different plant, so the note is composed in code instead and the trace
says why ("Replay check: plant differs from the recording"). Side effects, citations and checks always
reflect the visitor's plant. `uv run python -m evals.record --refresh-facts` recomputes the stored facts
without API calls.

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
timeout, refusal, budget) also continues as replay in the same run, keeping the run's context: what the
live part created is reused rather than repeated, and anything it flagged stays flagged. Server-side
fallback (`fallbacks: "default"`) is on, so a request the model declines can be retried by the API on
another model; reservations are priced for the most expensive model that might answer.

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
| Live runs per client address per day | 6 | `COLDLINE_ORIGIN_RUNS_PER_DAY` |
| Concurrent live runs | 2 | `COLDLINE_MAX_CONCURRENT_LIVE` |
| Model calls per run | 8 | `COLDLINE_MAX_MODEL_CALLS` |
| Output tokens per call | 3000 | `COLDLINE_MAX_TOKENS_PER_CALL` |
| Run / call timeout | 90 s / 40 s | `COLDLINE_RUN_TIMEOUT_S`, `COLDLINE_CALL_TIMEOUT_S` |

Before each call the loop reserves an upper bound: one token per input byte (a token always covers at
least one byte) at the cache-write rate of the most expensive model that may answer, plus the full output
limit. The SDK does not retry, so one reservation covers one attempt. The reservation is written to the
ledger before the call and settled afterwards: at the real cost, at zero for an API error, or kept in full
(as `unreconciled_usd`) when the outcome is unknown (timeout, lost connection, cancellation). The ledger is
`$COLDLINE_DATA_DIR/agent_spend.json`, replaced atomically on every change. A reservation left behind by a
stopped process counts as spent on restart. If the ledger cannot be read or written, or fails validation
(version and fields, a real date not in the future, finite non-negative amounts), live runs are disabled
with a visible reason; startup and replay are never affected. A file that fails validation is quarantined
(renamed to `agent_spend.json.quarantined-<UTC time>`, contents untouched), and live runs stay off across
restarts until an operator reviews it and removes the quarantined copy. With server-side fallback
(`fallbacks: "default"`) the provider picks the fallback model, so reservations are priced at the
conservative rate used for unlisted models; that rate is the supported ceiling, and a settlement above its
reservation switches live runs off. Each run records its attempted calls (`live_calls`) and any spend whose
outcome is unknown (`unreconciled_usd`); the Impact page shows the latter separately, never as a confirmed
zero. The session limit is keyed by session id, the
per-address quota stops one visitor cycling cookies, and the daily cap is the backstop.

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

Evals assert on what happened in the business systems and on the reported confidence. Where wording is the
risk (an unsupported diagnosis or release advice in the handover), they check the note as rendered and
copied, and the code-set headline.

* **scenario** (5): probe mismatch, stale reference probe, compressor trip, door open, poisoned email.
  Deterministic in CI via the recordings; live with `--live`.
* **adversarial** (7, scripted models that misbehave on purpose): follows the injected email, holds a lot on
  the injected email's say-so, invents a diagnosis and release advice, writes "can ship" over a lot it just
  held (and buries the precaution), skips required lookups, proposes a dispatch on stale evidence, cites a
  record it never read. These show the controls hold when the model
  does not.
* **approval** (5): expiry on a stale probe, expiry when a new fault supersedes the evidence, exactly-once
  execution, decline, no self-approval.

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
* The handover sentence checks are patterns too: they catch the contradictions above, not every possible
  misstatement. The code-composed block is the authoritative record.
* Recorded prose can mention clock times from the recording sandbox; checks and side effects are always
  live, and wording recorded against a different plant state is not reused.
