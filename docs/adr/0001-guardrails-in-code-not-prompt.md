# 0001 · Guardrails in code, not in the prompt

**Status:** accepted

## Context

The agent changes business records (QA holds, dispatch times, work orders) on a food plant, and it reads
untrusted text (supplier emails, ticket notes). A prompt can ask a model to be careful; it cannot guarantee
it. Any control that depends on the model following instructions fails exactly when it matters: when the
model is wrong or has been manipulated.

## Decision

Every safety rule is enforced in Python, in the tool registry and approval store:

- Each tool has a **tier** (READ, AUTO, APPROVE, NEVER) and a **scope**. NEVER-tier actions are not tools at
  all; requests for them are refused and audited. APPROVE-tier tools only create proposals.
- The systems interface has **no write path to plant control**.
- **Evidence checks** (freshness, probe divergence) are computed in code and cap the confidence the model may
  claim. Citations must reference records a tool returned in this run. The handover's disposition (holds,
  shipping, proposals waiting, required follow-ups) is composed in code from the records; the model's prose
  is a labelled section below it that may not state those decisions, and sentences the checks contradict
  are removed.
- **Approvals** bind to the evidence they relied on (record versions, probe quality, the evidence verdict,
  the incident generation), an idempotency key and a per-session decision token, re-check the action's precondition at decision time, and
  expire when any of it changes.

The prompt still explains the rules, so the model mostly behaves well and the trace reads naturally, but
nothing depends on it.

## Consequences

- The adversarial evals can use scripted models that misbehave on purpose and still pass.
- Adding a capability means writing code and tests, not editing a prompt. That is slower, and intended.
- Some refusals are visible in traces as GUARDRAIL steps; that is a feature for supervisors and auditors.
