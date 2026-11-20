# Architecture decision records

Short records of decisions that shape ColdLine, in the order they were made. Each says what was decided,
why, and what it costs.

| ADR | Decision |
|---|---|
| [0001](0001-guardrails-in-code-not-prompt.md) | Enforce action tiers and evidence rules in code, not in the prompt |
| [0002](0002-hand-written-tool-loop.md) | A hand-written tool-use loop instead of an agent framework |
| [0003](0003-replay-by-default.md) | Replay recorded model decisions by default; live runs are opt-in and capped |
| [0004](0004-in-memory-per-visitor-sandboxes.md) | One in-memory sandbox per visitor, in one process |
| [0005](0005-server-rendered-htmx-sse.md) | Server-rendered pages with htmx and SSE, no front-end framework |
