# D-Bot demo frontend guide

This file is the working contract for agents editing `demo/`.

## Functional map

- `index.html`: current frontend, including CSS and JavaScript.
- `app.py`: FastAPI page/API layer and process state for offline and LIVE runs.
- `../live_scenarios/runner.py`: LIVE E2E orchestration and trace schema.
- `../live_scenarios/scenarios.py`: the five PostgreSQL workloads and cleanup logic.

## Product hierarchy

The primary journey is **LIVE diagnosis**:

1. choose one of five scenarios;
2. start one run;
3. follow `Workload → Metric → Alert → Webhook → D-Bot → Report → Cleanup`;
4. read the root cause, evidence, and final E2E status.

Offline replay and report history are secondary tabs. They must not compete with LIVE on first load and must load their data only when opened.

## Agent-readable UI contract

- Keep one `h1`, one visible primary action per state, and DOM order aligned with the workflow.
- Implement modes with `tablist`, `tab`, and `tabpanel`; inactive panels must be `hidden` and absent from the accessibility tree.
- Use native controls. Scenario selection must be a radio/single-select pattern, not clickable cards alone.
- Give actions unique accessible names containing the scenario, for example `Chạy LIVE: Ghi đồng thời`.
- Expose one status region and an ordered workflow list with `aria-current="step"`.
- Keep stable `data-testid` or `data-agent-id` values for primary controls and states. Suggested names:
  - `tab-live`, `tab-offline`, `tab-history`
  - `scenario-large-read`, `scenario-bulk-delete`, `scenario-many-indexes`, `scenario-concurrent-writes`, `scenario-slow-join`
  - `run-live`, `stop-live`, `live-status`, `live-workflow`, `live-result`, `technical-details`
  - `consultation-summary`, `consultation-consensus`, `consultation-transcript`, plus stable `data-message-id` and `data-bot-id` on transcript entries
- Do not use CSS class names, row positions, or changing text as the only automation selector.

## Load contract

- Initial load may fetch only LIVE scenario/config/state data.
- Do not fetch `/api/history` until the History tab opens.
- Do not render the offline cards until the Offline tab opens.
- Fetch history with pagination; do not mount every saved report row at once.
- Start polling only for an active run and stop it at every terminal state or when ownership changes.
- Avoid duplicate config requests during bootstrap.

## Domain invariants

- Ground-truth labels are for post-run comparison only. Never send them into the D-Bot diagnosis input.
- Consultation UI may show only messages present in `brainstorming.messages`. Do not invent bot participants, agreement counts, minority views, or hidden reasoning; use an explicit empty state when no transcript was stored.
- Present the final diagnosis and evidence before the consultation transcript. Keep the transcript read-only and bind it to the current `run_id` or saved result id.
- A LIVE run succeeds only when the trace confirms all required stages: signal observed, Prometheus alert firing, webhook received, diagnosis return code `0`, report present, and cleanup successful.
- Starting a second offline or LIVE run while one is active must remain blocked.
- Stopping a run must preserve cleanup behavior and make the final state explicit.

## Comment policy

Add comments for non-obvious contracts only: state transitions, polling lifecycle, lazy-load boundaries, ground-truth isolation, and cleanup guarantees. Do not add comments that repeat element names or straightforward assignments.

## Minimum verification after UI changes

- Desktop and 390 × 844 mobile render without horizontal overflow.
- Keyboard-only user can switch tabs, select a scenario, start/stop, and reach the result.
- Accessibility tree contains only controls from the active tab; all primary controls have unique names.
- Opening the page does not call `/api/history`; opening History calls it once and shows loading/empty/error states.
- LIVE workflow reports the active stage and distinguishes success, error, stopping, and cleanup failure.
- Browser console has no uncaught errors.
