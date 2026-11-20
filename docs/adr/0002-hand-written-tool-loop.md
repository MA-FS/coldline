# 0002 · A hand-written tool-use loop

**Status:** accepted

## Context

Agent frameworks provide loops, memory and tool plumbing. They also add layers between a decision and the
code that enforces it, and their behaviour changes between versions. For an agent whose every step must be
explainable to a shift supervisor and an auditor, the loop is the part that most needs to be obvious.

## Decision

Use the Anthropic Messages API directly with a loop of about 120 lines (`agent/loop.py`): call the model,
pass each tool call through the registry, return the results in one turn, repeat until the handover is
accepted or a bound is hit. Bounds (model calls, tokens, wall time, budget) are checked in the loop. Tool
definitions and the system prompt are a frozen, cached prefix.

## Consequences

- Each branch of the loop maps to a trace step a person can see.
- Replay is simple: a scripted client returns recorded turns through the same loop.
- Features a framework would give for free (parallel agents, long-term memory) are absent. The use case
  does not need them; adding them would be a deliberate decision with its own ADR.
