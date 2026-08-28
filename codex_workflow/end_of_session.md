# End-of-Session Handoff

Use this automatic closure once after every substantive Medium or Heavy
deployment, immediately before the main agent's final response. It also applies
when a deployment pauses or blocks. Questions and small or odd bounded tasks on
the direct fast path do not use this handoff and produce no worker statistics.

Spawn one fresh worker with:

- `agent_type="end_of_session"`
- `task_name="end_of_session_<deployment_id>"`, where the suffix is a unique,
  lowercase, underscore-safe deployment identifier
- `fork_turns="200"`

Pass only the active route, deployment ID, and closure state (`complete`,
`paused`, or `blocked`). Do not summarize the session, build a task capsule, or
maintain a usage ledger. The automatic finite fork passes recent main-agent
turns so the worker inherits the deployment context while retaining its Luna
xhigh model; its TOML contains the full procedure.

For a stateful Heavy deployment, Root must first observe
`.orchestration/state.json` with `closure_ready=true`. The worker may read the
machine state for consistency but must not edit `.orchestration/`; its existing
ownership remains project documentation and Git/session closure. It returns
`ORCHESTRATION_CLOSURE: PASS` only when those duties succeed, otherwise
`ORCHESTRATION_CLOSURE: BLOCKED` with evidence. Root alone records the final
state transition to DONE.

The worker alone reconciles the complete `agent_docs/` framework, performs
compact closing checks, handles Git staging and commit, and returns the final
handoff report and statistics table. Do not call a second documentation worker
or duplicate these steps. Wait for the worker, then relay its result. Create a
fresh uniquely named worker for every later substantive deployment in the same
session.

If the worker cannot be created or is blocked, report that limitation. Do not
silently transfer the handoff to Explorer or another role.
