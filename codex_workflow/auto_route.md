# AUTO Route

AUTO is an optional classifier, not a fourth execution system. Explicit user
requests for Light, Medium, or Heavy always win. If no route is specified, the
existing default remains Light; activate this classifier only when the user
selects AUTO.

Resolve AUTO to an existing route:

- Trivial, single-file, low-risk leaf work: Light and no workers.
- Bounded work: Light unless independent worker verification materially helps;
  in that case Heavy with one READY package, one `executor_luna`, and Tester.
- Multi-step work without useful parallel packages: Medium.
- Complex, parallel, dependency-deep, cross-module, ambiguous, high-risk, or
  verification-heavy work: Heavy with persistent orchestration.

Use `workflow.py route --assessment <json-file>` for a deterministic decision.
The assessment fields are `subtasks`, `dependency_depth`, `file_count`,
`parallelizable_tasks`, `expected_iterations`, `cross_module`, `ambiguity`,
`risk`, and `verification_need`. A caller may pass `--manual-route` to prove
that an explicit override was honored.

Do not launch Heavy for a one-line task merely because AUTO is active. AUTO
never creates a second planner: after resolving to Heavy, follow
`heavy_route.md` and materialize that Heavy Plan as the Task DAG.
