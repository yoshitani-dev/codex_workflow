# Persistent Heavy Orchestration

This is the machine-state extension for the existing Heavy route. It does not
replace Heavy planning, Codex agent lifecycle, the executor↔tester repair loop,
Explorer, or End-of-Session.

## Ownership

- Root owns transitions, the Heavy Plan, DAG updates, routing, classification,
  integration decisions, and final completion decisions.
- `executor_luna` executes bounded packages. `executor_sol` is the specialist
  for hard diagnosis and exceptional implementation. Tester independently
  verifies and keeps routine repair directly paired with the executor.
- End-of-Session keeps its existing documentation and Git closure ownership.
- Human-readable `agent_docs/` and machine-readable `.orchestration/` remain
  separate.

## Files

```text
.orchestration/
├── config.json
├── state.json
├── tasks.json
├── failures.json
├── events.jsonl
└── runs/
```

The extension config owns only Luna capacity and loop/retry budgets. The
existing `workflow_config.json` remains the source for platform child capacity
and `executor_sol` capacity. Do not add Luna-only fields to the upstream
workflow config.

## Root Loop

For a substantive Heavy deployment:

1. Start or resume the deployment and reconcile state with Git, active agent
   threads, required inputs, and fresh objective verification.
2. Build the normal Heavy Plan. Persist that exact plan as `source=heavy_plan`;
   the CLI validates task schema, dependencies, and cycles but does not plan.
3. Begin one macro iteration. Dispatch only task IDs returned under
   `schedule.dispatchable`; `dispatch` rechecks READY, Luna≤4, Sol≤the existing
   workflow limit, total platform capacity, the End-of-Session reservation,
   and write-scope conflicts.
4. Record executor completion, then use the existing Tester. A task becomes
   `done` only when the verification gate and every Acceptance Criterion are
   `PASS`. `NOT_TESTED` is never success.
5. Record routine failures without returning raw logs to Root. The direct
   executor↔tester repair contract remains authoritative. A repeated signature,
   exhausted budget, stagnation signal, hard class, or scope expansion routes
   to Root and then `executor_sol` when budget remains.
6. After the specialist returns the required diagnosis contract, Root chooses
   the implementation owner. Prefer Luna when its attempt budget remains;
   allow Sol implementation for the documented exception.
7. Reconcile again, persist, and start the next bounded iteration. Stop at 12
   iterations or an explicit DONE, BLOCKED, or FAILED condition.
8. When `closure_ready=true`, run the existing End-of-Session handoff. Record
   DONE only after that handoff passes.

Use `python ~/.codex/codex_workflow/workflow.py orchestrate --help` for commands.
Write JSON payloads to a temporary project-local file, call the CLI, then remove
the temporary payload after the operation succeeds. Keep raw logs out of the
state files and reference durable artifacts instead.

The normal control sequence is:

```text
workflow.py orchestrate init --project <project> --json
workflow.py orchestrate start --project <project> --deployment-id <id> --json
workflow.py orchestrate import-plan --project <project> --payload <heavy-plan.json> --json
workflow.py orchestrate reconcile --project <project> --payload <reality.json> --json
workflow.py orchestrate begin-iteration --project <project> --json
workflow.py orchestrate dispatch --project <project> --task-id <id> --agent-instance <id> --json
workflow.py orchestrate execution-result --project <project> --task-id <id> --payload <result.json> --json
workflow.py orchestrate register-agent --project <project> --role tester --agent-instance <id> --task-id <id> --json
workflow.py orchestrate verify --project <project> --task-id <id> --payload <verification.json> --json
workflow.py orchestrate register-agent --project <project> --role end_of_session --agent-instance <id> --json
workflow.py orchestrate close --project <project> --closure-state complete --end-of-session-status PASS --json
```

Use `fail` for an executor/tester failure packet and `specialist-result` for the
required `executor_sol` diagnosis contract. `schedule`, `status`,
`register-agent`, and `release-agent` expose the remaining lifecycle controls.
The package includes `resources/heavy_plan.example.json`; it shows the minimum
plan-to-DAG representation. Tester, Explorer, Root, and End-of-Session are
workflow gates and cannot be substituted as DAG executor owners.

For AUTO, create an assessment JSON and call `workflow.py route --assessment
<file>`. For model diagnostics, call `workflow.py model-canary --codex-bin
<codex-executable> --json`. A catalog-supported pair is reported separately
from actual runtime identity; absent independent runtime metadata, the result is
`MODEL_IDENTITY=NOT_VERIFIED`.

## Recovery and Reality

On a new session or after compaction, load `.orchestration/`, `agent_docs/`, and
Git state. Call `reconcile` with currently active agent instance IDs and fresh
verification results. Missing/stale running agents reopen their tasks. A stored
`done` task reopens when objective verification is `FAIL` or `NOT_TESTED`.
Objective evidence always overrides stored assumptions.

All core JSON files and `events.jsonl` are updated together through the
existing compensating transaction and atomic per-file replacement. The event
stream is append-only, and each macro iteration/reconciliation/closure writes a
run snapshot.
