# 0003 · Replay by default

**Status:** accepted

## Context

The demo is public. Live model calls cost money, take 10 to 20 seconds and depend on an external API.
Visitors should see the agent work every time, and nobody should be able to run up the bill.

## Decision

Replay is the default mode. A recording holds the model's side of a real live run (its notes, tool calls,
token counts and latency). Replay feeds those turns through the normal loop, so the tools run for real
against the visitor's own sandbox, through the same tiers, evidence checks and approval store. The QA hold
really lands, proposals really wait, and a stale probe really changes which recording is chosen and what the
checks conclude.

Live mode is opt-in per visitor, limited per session per hour, by concurrency and by a global daily cap with
worst-case reservations per call. Any refusal or failure falls back to replay inside the same run, with a
visible "Fallback" step.

Recordings are made by `evals.record`, which refuses to save a run that fails its scenario checks.

## Consequences

- The demo costs nothing to serve in replay and never shows a broken run because of the API.
- Recorded prose can mention times from the recording sandbox; the checks and side effects are always live.
  The README says so.
- A prompt or tool change requires re-recording (about US$0.15), which keeps recordings honest.
