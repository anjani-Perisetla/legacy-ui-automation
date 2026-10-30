# Report

## 1. Architecture

One Python process, four parts with narrow interfaces between them:

```
            discover (LLM)                       replay (no LLM)
                 |                                     |
                 v                                     v
   agent.py --records--> Capability JSON --loads--> replay.py
       \                                            /
        \--------- surface.py (perceive / act) ----/
                         |            \
                    policy.py      handoff.py (human takes the same session)
```

- **Surface** (`surface.py`, `dom.js`) is the only code that knows it is driving a
  browser. It exposes `observe`, `locate`, `click`, `fill`, `read`, `has_text`,
  `screenshot`. Callers speak in `Target` and `Read` objects, never selectors.
- **Discovery** (`agent.py`) is an observe/decide/act loop with Claude (Opus 5.5, tool
  use, one action per turn). The model gets a redacted text view of each frame and a
  numbered list of controls; it answers with `act`, `finish` or `ask_human`. Every
  action it takes is recorded as a step in operator terms.
  The model sits behind a one-method `decide(observation, last_result)` interface:
  `ClaudeDecider` calls the API, `FileDecider` exchanges turns through files. The run
  in `evidence/` used the file decider because no API key was available (details in
  `evidence/README.md`).
- **Replay** (`replay.py`) runs a capability with inputs and returns a structured result.
- **App profile** (`targets/cucore/profile.json`) holds what is true for the whole app
  rather than one task: the policy, and the screens the app can throw at you (not found,
  session expired, system notice, system error) with how to treat each.

Decisions and trade-offs:

- *Text + control list, not screenshots, as the model's input.* It is cheaper, it can be
  redacted before it leaves the process, and it is exactly what replay uses to find
  things again. The cost: anything not exposed in the DOM/accessibility layer (canvas,
  images of text) is invisible. For those surfaces a screenshot+coordinates observer
  would plug in behind the same interface (section 4).
- *Synchronous, single process.* There is no queue or service split. The seams that
  would become service boundaries (surface, capability store, handoff control) are
  modules with small interfaces, which is the part that matters now.
- *Playwright on Chromium.* Good frame support and it exposes the live browser over CDP,
  which is what the handoff uses.

## 2. Artifact schema

See `cua/artifact.py` and `evidence/member_savings_balance.json`. A capability has a
**contract** that a calling agent reads and a **body** that replay runs:

| field | purpose |
|---|---|
| `id`, `version`, `schema_version`, `status` | identity; `draft` until a person approves it |
| `app`, `app_version` | which vendor product (profile) and which version it was recorded on |
| `inputs` | typed parameters; `pattern` is validated before the UI is touched; `secret: true` values are never stored |
| `outputs` | name, type (`string`, `money`), where to read it, `pii` flag |
| `outcomes` | business outcomes this capability may return instead of outputs |
| `steps` | `goto` / `click` / `fill` / `wait_text`, each with a `Target`, a templated `value` and the model's one-line `note` |
| `checkpoint` | text that must be on screen before outputs are read |
| `recorded_from` | the discovery run id, for traceability |

Why this shape:

- **Targets describe controls the way an operator sees them**: frame, role, visible name,
  the label text next to it (`near`), and a CSS path only as a last resort. On a legacy
  screen the input next to the cell "Member Number" has no id, name or label, but
  "textbox next to *Member Number* in the *main* frame" is stable as long as the screen
  is. It also survives tenant branding and markup changes that move elements around.
- **Values are templates** (`{{member_number}}`). The model is told to type placeholders,
  and the recorder converts any literal input value it does type. That is what makes the
  recording a parameterized capability instead of a transcript.
- **Reads are semantic** (`label` or `row`+`column`), the same way a teller reads a screen:
  "the Balance column of the SHARE SAVINGS row".
- **No model transcript in the artifact.** The `note` per step is enough for a reviewer
  to see intent; the full reasoning stays in the run log.
- **App-wide conditions are not copied into each artifact.** "Session expired" is the
  same for every capability on CU*CORE, so it lives in the profile and is fixed in one
  place. The artifact only declares which business outcomes it can return.

## 3. Determinism & error handling

**Targeting.** `dom.js` builds the same control list during recording and replay. A
target is resolved by trying `role+name`, then `role+near`, then `css`. A strategy only
counts if it matches exactly one control; ambiguity falls through to the next. If the
match was not on the primary strategy, replay logs a `drift` event. Locating polls for
up to 5 s, which absorbs slow screens without fixed sleeps; after each action every frame
is waited to `load`.

**Result contract.** Replay returns one of three statuses:

- `success` with typed outputs (money normalised to `1234.56`).
- `business_outcome` with a declared outcome, e.g. `member_not_found`. This is an answer,
  not an error, and the caller is expected to branch on it.
- `failure` with `{kind, step, expected, observed, screenshot}`. `kind` is one of
  `bad_input`, `not_approved`, `approval_required`, `system_error`, `bad_signon`,
  `stuck`, `checkpoint_not_met`, `output_missing`, `output_malformed`,
  `recovery_exhausted`, `unsafe_restart`, `undeclared_outcome`, `policy_violation`,
  `handoff_timeout`.

**Recoverable conditions** never reach the caller; they appear in `recoveries`:

- an interstitial (`SYSTEM NOTICE`) is dismissed by its known button;
- session expiry restarts the flow from the entry step, but only if no risky step has
  run yet, so a restart can never double-post anything;
- a control that is not there yet gets one more retry after a pause.

Recoveries are capped at three per run. Order of checks before and after each step:
known condition on screen → recover or stop; otherwise act; if the target is missing,
check conditions again before calling it stuck. A screen that matches nothing known is
`stuck`, which either fails with a screenshot or, with `--escalate`, goes to a human.

**Drift.** Because the UIs are stable, drift is a secondary concern. It shows up as:
a fallback locator strategy matching (`drift` event), the version marker in the banner
not matching what the capability was recorded on, or a hard `stuck`. Those are the
signals to count across tenants (section 4).

Evidence in `evidence/`: a discovery run, a clean replay with other inputs, a
not-found replay, a replay that recovers from a notice and a session expiry, a hard
system error, and a handoff.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The recorded flow depends only on `Target`, `Read` and the
surface methods. Other surfaces implement the same methods:

- *Legacy web* is what is implemented: framesets, nested tables, no ids. `frame` is part
  of the target; `near` handles unlabeled inputs.
- *Desktop (Win32/WinForms/Java)*: UI Automation and Java Access Bridge expose the same
  ideas: control type (role), Name, and LabeledBy / previous sibling text (near). `frame`
  maps to the window/pane path. `read` maps to grid cells by header. Nothing in the
  artifact changes except the surface named in the profile.
- *Pixel-only (Citrix, terminal emulators, canvas)*: an OCR/vision observer produces the
  same control list (role from shape, name/near from recognised text, plus a bounding
  box). Targeting by visible text stays meaningful; coordinates would only be a cache.
  Green-screen 3270/5250 is easier still: field positions and protected labels are known.

**Multi-tenant reuse.** The unit of reuse is the vendor product, not the tenant:

```
vendor profile  (CU*CORE 3.x: policy defaults, known screens)
  └─ capability (member_savings_balance v1, recorded on base tenant)
       └─ tenant overlay  (optional, small: label renames, extra interstitial,
                           different menu text, base URL, credentials ref)
```

A tenant overlay is a JSON patch keyed by step id and target field (for example tenant B
calls the menu item "Member Lookup"). Resolution is base → vendor version → tenant, so
most tenants carry no overlay at all. Credentials and base URLs are always per tenant
and are never in the artifact.

Detecting drift per tenant: each replay records the `app_version` marker and which
locator strategy matched each step. Aggregated per (capability, tenant, vendor version),
that shows when a tenant upgrades or rebrands before it starts failing: a step that
moves from `role+name` to `role+near` is an early warning. A new vendor version gets
capabilities re-verified by replaying them against a staging tenant; failures become
either a version-specific override or a re-discovery, which goes back through review.

## 5. Escalation & handoff

**Detecting stuck.** In discovery the model calls `ask_human` (it is told to rather than
guess), or a policy block on a risky control prompts it to. In replay, stuck is any of:
a target that cannot be found after condition checks and a retry, a checkpoint that is
not reached, or a failure-class screen when `--escalate` is on.

**Routing.** `request_human` writes `intervention.json` in the run directory: run id,
capability and version (or goal), step id and the step's note, what was expected, the
redacted screen text, a screenshot and how to take over. In production this record is
what goes to an operator queue.

**Control transfer.** `control.json` names exactly one holder, `automation` or `human`.
Automation checks it before every action (`assert_automation`). On handoff it flips to
`human` and automation only polls; the browser, cookies and page stay as they are. The
operator gets the *same* session two ways: the visible window (`--headed`), or by
attaching to the browser over CDP. `cua operator click/fill` is a minimal console that
does the latter; operator commands refuse to act unless the holder is `human`.

**Capturing what the human did.** An init script in every frame reports clicks and field
changes (described in the same role/name/near terms as steps, password fields as
`[SECRET]`) through a Playwright binding. Python only keeps them while a human holds
control. They go into the log as `handoff_returned` with the operator's note, and into the
result's `handoffs`.

**Handing back.** `cua operator resume <run> --note ...` sets the holder back. Replay
then re-checks: if the checkpoint is now met (the human finished the flow) it reads the
outputs; otherwise it retries the step it was stuck on. Two handoffs per run at most,
15-minute timeout. Human actions are recorded but deliberately not merged into the
capability: if a human was needed, that is a signal for review or re-discovery, not
something to learn silently.

Mocked: the operator console. A real one would stream the CDP screencast to a web page
and take input there; the control model and capture would not change.

## 6. Safety

- **Allowlist.** Origins and blocked paths are enforced at the network layer
  (`context.route`): a request outside the allowlist is aborted whoever triggers it,
  including a link the model clicks or a human. Action types are checked before every
  step.
- **Risky actions.** Controls whose label matches the profile's risky list (`Post
  Transfer`, `Approve`, `Delete`, ...) are blocked during discovery; the model must ask a
  human. In replay, a step marked `risky` only runs if the caller passes an approval for
  that step id. Draft capabilities do not run unattended at all. Restart-based recovery
  is disabled once a risky step has run.
- **Secrets.** Secret inputs come from environment variables, are only ever shown to the
  model as placeholders, are templated into artifacts, and are replaced with `[SECRET]`
  in every log line.
- **PII.** Screen text is redacted before it goes to the model and before it is logged
  (SSN to last four, long account numbers, dates of birth). Screenshots black out table
  cells holding those patterns. Outputs flagged `pii` are returned to the caller but
  masked in logs and in the saved result.

Limits: risk classification by label is a blunt instrument; an unlisted destructive
button is treated as safe in discovery (replay is still gated by review and approval).
Regex redaction misses PII in free-form shapes (names, addresses), so names on screen do
reach the model; a production version needs field-level classification from the app
profile and a model endpoint covered by the institution's data agreement. Logs are local
files without encryption or retention controls.

## 7. Cuts

Left out on purpose:

- Screenshot/vision perception and a desktop surface (the interfaces are there).
- Tenant overlays and version-specific overrides (designed in section 4, not built).
- A real operator console; the CDP CLI stands in for it.
- LLM-assisted recovery of a single failed step during replay.
- Persistence beyond files: no capability store, no run database, no queue.
- Stability scoring across repeated replays.

Next, in order: tenant overlays plus a second "tenant" variant of the mock app to prove
reuse; per-step locator statistics to drive drift alerts and an approval score; a
bounded, policy-checked LLM fallback for one step with mandatory review of the result;
a desktop surface on UI Automation.
