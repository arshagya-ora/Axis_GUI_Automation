# Local workflow evaluations

From `axis-agent`, with the existing development dependencies installed:

```powershell
..\.venv\Scripts\python.exe -m evals.run --preflight
..\.venv\Scripts\python.exe -m evals.run
..\.venv\Scripts\python.exe -m evals.run --case grounded_final_answer --case multi_tab_memory --repeat 3
..\.venv\Scripts\python.exe -m evals.run --config axis.yaml --output evals/results/comparison --judge
```

The default runs all 12 cases three times, sequentially. A connected Chrome
extension and bridge plus the usual AXIS provider configuration are required.
`--preflight` checks bridge readiness, bridge authentication, and local provider
setup without sending a model request or creating tabs. Provider availability
is ultimately tested by the workflow request itself.

Each repetition starts the existing fixture server on a fresh loopback origin.
Only evaluation-created tabs are exposed to AXIS, navigation is restricted to
that origin, and those tabs and the server are closed afterward. Download
fixtures create a small CSV through the browser's configured download behavior.
Existing user tabs are never closed. The canvas case uses automatic visual
recovery; a configuration with `run.visual_mode: off` can measure the DOM-only
baseline.

The original eight quality cases have executable pages, with additional cases
for memory across tabs, asynchronous SPA changes, completed downloads, and a
canvas control. Outcomes use independent fixture state, fresh browser reads,
and actual interaction counts. Repeated form/button actions fail the duplicate
action check even if the final state is correct. An agent's `completed` status
with failing fixture checks is a false completion.

Reports are `results.json` and `results.md` under the ignored
`evals/results/<timestamp>` directory unless `--output` is supplied. They include
task success, false completions, duplicate actions, latency, model requests,
browser actions, and input/output tokens. Unavailable prerequisites are shown
separately and excluded from performance averages. Exit codes are `0` for all
tasks passing, `1` for task failures, and `2` for unavailable prerequisites.

The optional `--judge` constructs an LLM judge using the configured AXIS model
and writes its Pydantic Evals report to `pydantic-evals.json`. The judge does not
replace deterministic checks. Screenshots and image bytes are removed from all
persisted reports. No model judge is constructed during an ordinary run.

Reports also include planner calls, time to first browser action, p50/p95
latency, repeated unchanged observations/actions/assertions, and calls during
local recovery. Repetition counts identify candidates for review; a repeated
observation during a legitimate asynchronous wait can still be necessary.
First-attempt target accuracy is reported only for fixtures with a known target
ID and an attributable first action; unavailable target evidence is excluded.
Verification accuracy is assessed with independent fixture state and false
completion counts.

For a matched Codex comparison, run the same fixture cases with identical user
prompts, initial pages, permissions, action budgets, and independent outcome
checks. Reset each case between agents, alternate run order, and keep model
requests, first-action time, success rate and p50/p95 latency separate. Codex's
internal planner request count may be unavailable; report that as unavailable,
rather than zero. Use at least three repetitions per case for a smoke comparison
and more repetitions before drawing conclusions about p95 latency.

The one-response read and two-response stable form regressions use scripted
FunctionModel responses. They prove orchestration request counts, not provider
latency or model judgment. Live comparisons require the configured AXIS provider
and a Codex browser session; do not claim parity from scripted runs.

The `autonomous_enterprise` fixture exercises ordered error inspection, title changes,
a delayed scrollable table with repeated Edit labels and row-header cells, selecting
the 17th row, and conditional Save. Its server records action order and saved values
independently of the agent report. Run it with `python -m evals.run --case autonomous_enterprise --repeat 3`
(using the existing bridge and provider configuration).
