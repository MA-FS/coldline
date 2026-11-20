# ColdLine design system

ColdLine's UI follows **ISA-101 high-performance HMI** conventions: a calm, dense, grey control-room screen
where colour is a signal, not decoration. An operator should be able to glance at the overview and see the one
thing that is wrong.

Live reference: `/styleguide` renders every token and component from synthetic fixtures. In development every
page can also be previewed under `/preview/...` (see [Previews](#previews)); production hides those routes. The visual target is
[`reference-mockup-a.png`](reference-mockup-a.png).

## Rules

1. **Colour means abnormal.** Normal process state is grey. Amber and red appear only for alarms, excursions,
   holds and things a person must act on. If everything is fine, the screen has no amber or red on it.
2. **Amber = needs attention, red = act now.** Alarm priorities map one-to-one: `AMBER` → `--amber`,
   `RED` → `--red`. `LOW` alarms are listed in the alarm table but never colour the mimic.
3. **Only the abnormal value is coloured.** In the hero scenario TT-401 is amber; TT-402, in the same alarm
   but reading normally, stays grey.
4. **Blue is for things you can click** (primary buttons, links, approval actions, APPROVE-tier badges), never
   for process state.
5. **Green is a confirmation mark only**: a done-step tick, an approved decision, a passing eval. Never a
   "running OK" fill on equipment.
6. **Stale is not a value.** A stale tag is grey italic with a hatched `STALE` marker; charts draw stale
   samples dashed. Stale data is never coloured as if it were a reading.
7. **Monospace for machine text**: tags, record ids, tool calls, `obs:` lines, timestamps, idempotency keys.
   Inter for everything a person reads as prose.
8. **Thin lines, flat surfaces.** 1px borders, no shadows except on popovers and toasts, no gradients except
   the faint wash behind guardrail steps.
9. **Motion is functional.** A spinner while the agent works, a 280 ms rise when a streamed step lands.
   Nothing blinks.
10. **Every page carries the disclaimer** (`contracts.DISCLAIMER`) in the banner above the top bar.

## Tokens (`src/coldline/web/static/css/tokens.css`)

| Group | Tokens | Use |
|---|---|---|
| Surfaces | `--bg-0` page · `--bg-1` panel · `--bg-2` raised · `--bg-3` hover/selected · `--bg-inset` code/obs · `--bg-banner` | Layering, darkest at the back |
| Lines | `--line` · `--line-soft` · `--line-strong` | 1px borders, table rules, equipment frames |
| Text | `--text-1` values · `--text-2` body · `--text-3` secondary, axes, stale values (italic), step metrics · `--text-4` decoration only | Four levels, all ≥ 4.5:1 on `--bg-1` in both themes except `--text-4`, which never carries text a person needs to read |
| Process | `--proc` · `--proc-dim` · `--proc-fill` | Mimic outlines and pipes; dim = standby |
| Abnormal | `--amber` `--amber-weak` `--amber-line` `--amber-ink` · `--red` `--red-weak` `--red-line` | Alarms, holds, refusals |
| Confirm | `--ok` `--ok-weak` | Ticks only |
| Interactive | `--accent` `--accent-strong` `--accent-weak` `--focus` | Buttons, links, focus ring |
| Stale | `--stale` `--stale-hatch` | Stale markers and dashed chart segments |
| Type | `--font-ui` (Inter) · `--font-mono` (JetBrains Mono) · `--fs-xs` 11 → `--fs-3xl` 34 | Tabular numerals everywhere numbers line up |
| Space | `--s-1` 4 → `--s-10` 40 | 4px grid |
| Shape | `--r-1` 3 · `--r-2` 5 · `--r-3` 8 · `--r-pill` | Small radii; equipment is rectangular |

Dark is the default. `[data-theme=light]` switches to a light ISA grey palette with the same semantics
(toggle in the top bar, remembered per browser; `?theme=light` forces it for a page load).

## Files

```
src/coldline/web/
  render.py          Jinja2Templates instance, filters (hhmm, usd, ms, temp, cite, markdown…), base_context()
  views.py           PlantSnapshot → PlantView (mimic state rules), chiller_detail(), citations, safe markdown
  charts.py          trend_svg() and sparkline_svg(): server-rendered SVG, no JS chart library
  fixtures.py        Synthetic hero-scenario data for previews and tests
  preview.py         /preview/* (development only) and /styleguide routes
  routes.py          Live pages, the chiller fragment and the SSE stream
  routes_actions.py  POST controls and approval decisions (CSRF header + rate limits)
  context.py         Page contexts built from the visitor's sandbox and the agent service
  runtime.py         Session sandboxes, agent service, per-visitor prefs (mode, role, focus)
  security.py        Signed session cookie, CSRF check, rate limiter, security headers
  settings.py        Web settings from the environment
  static/css/        tokens.css · base.css (shell, layout) · components.css · pages.css (page layouts, mimic)
  static/js/app.js   Theme toggle, menus, toasts, SSE scroll; ~80 lines, no framework
  static/vendor/     htmx 2.0.3 + htmx-ext-sse 2.2.2 (vendored, so the app loads no third-party scripts)
  templates/
    base.html        App shell: disclaimer, top bar, nav, clock, mode badge, toasts
    components/      Jinja macros (below)
    fragments/       Partials for HTMX swaps and SSE events
    pages/           One template per page
```

## Component catalogue

| Macro | File | Notes |
|---|---|---|
| `icon(name)` | `icons.html` | Inline stroke icons, `currentColor` |
| `ui.panel(title, id, meta, count)` | `ui.html` | `{% call %}` for the body |
| `ui.kpi(label, value, unit, note, baseline, kind, tone, spark)` | `ui.html` | `kind` = measured / estimate (dashed tag) |
| `ui.badge(text, tone)` · `ui.status_badge(status)` · `ui.mode_badge(mode)` | `ui.html` | Tones: running, attention, ok, red, amber, muted, neutral |
| `ui.prio(priority, active)` | `ui.html` | Solid annunciator chip; cleared = dashed grey |
| `ui.tier(tier)` | `ui.html` | READ grey · AUTO neutral · APPROVE blue · NEVER red with lock |
| `ui.button(label, variant, icon_name, href, size, attrs)` | `ui.html` | primary · approve · decline · outline · ghost; built-in htmx spinner |
| `ui.tagval(tag, value, tone, direction)` · `ui.kv(rows)` | `ui.html` | Tag/value pairs and label/value lists |
| `ui.ev_chip(id)` · `ui.ev_list(ids)` | `ui.html` | Evidence chips; glyph per record kind (tag, lot, WO, alarm, message, approval) |
| `ui.empty` · `ui.loading` · `ui.toast` · `ui.note` | `ui.html` | Empty, skeleton, toast, inline note |
| `ag.step_card(step, running)` | `agent.html` | Numbered; tool call in mono; `obs:` block; tier badge; tick / shield / blocked |
| `ag.conclusion(text, confidence)` | `agent.html` | unconfirmed & insufficient_data amber, refused red, confirmed neutral |
| `ag.approval_card(a, compact)` | `agent.html` | Evidence chips, payload, source-version binding, approve/decline; expired state |
| `ag.agent_panel(run, approvals)` | `agent.html` | Overview right column; SSE-ready when the run is running |
| `plant_mimic(plant_view)` | `mimic.html` | Hand-written SVG, 1060×600 viewBox |
| `pl.trend_block` · `pl.chiller_detail(d)` | `plant.html` | Current value, direction, chart |
| `pl.alarm_table(alarms)` · `pl.audit_row(e)` | `plant.html` | Alarm strip and audit log rows |

### Plant mimic states

Driven entirely by `views.plant_view(snapshot, trends)`:

| Condition | Rendering |
|---|---|
| Active AMBER/RED alarm on a chiller | 3px amber/red outline, two-line badge from the alarm description (`"Probe mismatch - …"` → `PROBE MISMATCH`), amber/red product drop line |
| Tag in that alarm and above 4.0 °C | Value amber/red with direction arrow |
| Tag quality stale/bad | Grey italic value, hatched `TT-402 STALE` badge |
| Compressor stopped with an active RED alarm on it (or `detail["state"] == "tripped"`) | Red symbol, red box, `TRIPPED` |
| Compressor not running, no alarm | Dim symbol and dashed feed, `STANDBY` |
| Lot on QA hold at the dock | Amber `QA HOLD` chip |

Chillers are focusable buttons (`hx-get /chillers/{id}` → swaps `#chiller-detail`).

### Trend charts

`charts.trend_svg(series, events=, divergence=, limit=, y_min=, y_max=)` returns inline SVG:
axes and gridlines, `ChartEvent` markers (dashed, staggered labels), amber shading where two series differ by
more than the divergence threshold (good samples only, so shading stops when a probe goes stale), dashed
limit lines, dashed grey stale segments with a `STALE` label. Series tones: `normal`, `amber`, `red`, `ghost`.

## Pages and their context

All pages extend `base.html` and need `render.base_context(active, now, mode=, pending_approvals=)`.

| Template | Extra context |
|---|---|
| `pages/overview.html` | `plant` (PlantView), `detail` (`views.chiller_detail(...)`), `run`, `approvals`, `scenarios`, `poll_url` |
| `pages/runs.html` | `runs` |
| `pages/run_detail.html` | `run`, `evidence` (id → {title, detail, version}), `evidence_chart` |
| `pages/approvals.html` | `approvals`, `all_approvals`, `status_filter` |
| `pages/audit.html` | `events` |
| `pages/evals.html` | `evals` (list of {scenario, case, checks[{name, passed}], passed, cost_usd, latency_ms, run_id, note}) |
| `pages/impact.html` | `kpis`, `assumptions` |
| `pages/handover.html` | `run`, `evidence` |
| `pages/how_it_works.html` | none |

## HTMX regions and endpoints

Stable ids for swaps: `#overview`, `#mimic`, `#chiller-detail`, `#agent-panel`, `#agent-steps`,
`#agent-conclusion`, `#agent-approvals`, `#alarms`, `#approvals`, `#approval-{id}`, `#trace-steps`,
`#audit-list`, `#clock`, `#toasts`.

| Control | Request | Target |
|---|---|---|
| Inject fault menu | `POST /session/inject` `{scenario}` | `#overview` (hx-select) |
| Make reference probe stale | `POST /session/stale` `{tag: "TT-402"}` | `#overview` |
| Replay / Live | `POST /session/mode` `{mode}` | `#overview` |
| Reset plant | `POST /session/reset` | `#overview` |
| Click a chiller | `GET /chillers/{id}` → `fragments/chiller_detail.html` | `#chiller-detail` (outerHTML) |
| Approve / Decline | `POST /approvals/{id}/approve` `{idempotency_key}` · `/decline` → `fragments/approval_card.html` | `#approval-{id}` (outerHTML) |
| Approver role | `POST /session/role` `{role}` (Shift Supervisor · QA Manager) → 204 + `HX-Refresh` | page |
| Live mimic refresh | `GET {poll_url}` every 5 s or on `plant-changed` | `#mimic` (hx-select) |
| Agent stream | SSE `GET /runs/{id}/stream` | events: `step` → `fragments/agent_step.html` appended to `#agent-steps`; `conclusion` → `fragments/conclusion.html`; `approval` → compact approval card; `done` removes the working skeleton; `trace-step` → `fragments/trace_step.html` for the run page. Event ids are step numbers (`Last-Event-ID` resumes); both connecting elements carry `sse-close="done"` and `app.js` re-renders the region when the stream closes |

Toasts: send `HX-Trigger: {"toast": {"title": "...", "text": "...", "tone": "ok|amber|red"}}`. Refused requests
(403, 404, 409, 429) carry a red toast the same way.

Every POST must carry the `X-ColdLine: 1` header (set once on `<body>` with `hx-headers`); the pages load no
inline script except the hashed pre-paint theme switch, and `htmx-config` turns off `eval`, so `hx-on` and
`hx-trigger` filter expressions are not available. Put behaviour in `app.js` instead.

## Previews

| URL | Shows |
|---|---|
| `/preview/` | Overview, hero scenario finished, two approvals pending |
| `/preview/?state=running` | Agent mid-run (SSE-connected panel) |
| `/preview/?state=stale` | Reference probe stale: comparison withdrawn, approval expired |
| `/preview/?state=trip` | C2 tripped, both probes rising |
| `/preview/?state=idle` | Normal plant, agent idle |
| `/preview/runs`, `/preview/runs/R-7842` | Run list and trace (also R-7843 stale, R-7839 poisoned email) |
| `/preview/approvals`, `/preview/audit`, `/preview/evals`, `/preview/impact`, `/preview/handover`, `/preview/how-it-works` | Remaining pages |
| `/styleguide` | Everything above, side by side |

## Accessibility

Body text contrast ≥ 7:1, secondary ≥ 4.5:1 on panels. Visible focus ring on every control; mimic units are
keyboard-focusable toggle buttons (`aria-pressed` marks the chiller shown in the detail panel) with
descriptive `aria-label`s, and the SVG carries a `<desc>` summarising active alarms.
Status is never colour alone: alarms have text priority chips, stale has a label, blocked steps have an icon and
the word "Blocked". Layout stacks to one column below 1200px; on phones the mimic scrolls horizontally inside
its panel rather than shrinking text below legibility.
