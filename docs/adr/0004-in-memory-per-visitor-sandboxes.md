# 0004 · In-memory, per-visitor sandboxes

**Status:** accepted

## Context

Every visitor injects faults, places holds and approves proposals. They must not see or affect each other,
and a public endpoint must have a hard ceiling on memory. Durability matters little: a demo plant that
resets after two idle hours is fine.

## Decision

Each visitor gets a `Sandbox` (simulator plus mock business systems plus audit log) selected by a signed,
random session cookie and held in a `SessionManager` with a 2-hour idle TTL and a cap of 200 (oldest evicted).
Agent runs, approvals and UI preferences are keyed by the same session and dropped when the sandbox goes.
Everything runs in one process on one machine.

## Consequences

- Isolation is structural: there is no shared table to filter wrongly.
- Memory is bounded and predictable.
- A restart or deploy clears every sandbox; acceptable for the demo.
- Horizontal scaling needs a shared store for sessions, runs, approvals and rate limits. The interfaces
  involved are small; the HANDOVER guide lists them.
