# 0005 · Server-rendered pages with htmx and SSE

**Status:** accepted

## Context

The UI is a dense control-room screen: an SVG plant mimic, trend charts, a streaming agent trace and a few
controls. Python is the team's core language, and the people maintaining this should not need a separate
front-end build.

## Decision

Render everything on the server with Jinja (charts are server-side SVG), use htmx for the few interactions,
and stream agent steps as HTML fragments over server-sent events. JavaScript is one small file plus vendored
htmx; no build step and no third-party scripts at runtime.

## Consequences

- One language and one deployable; templates are covered by the same tests as the routes.
- A strict Content-Security-Policy is easy: scripts come only from the app itself.
- Rich client-side interaction (drag, offline) would be harder; the product does not need it.
- SSE is one-way and reconnects natively; the stream uses step numbers as event ids so a reconnect resumes.
