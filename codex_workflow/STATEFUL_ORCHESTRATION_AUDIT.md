# Stateful Orchestration Audit

## Base

- Local Codex inspected: `codex-cli 0.149.1`.
- Source base: `viettran-edgeAI/codex_workflow` `1.1.3`, commit
  `e6c899ffd82d7d32aa9f93f0986a402add47c32d`.
- This thin-extension build: `1.2.2`.
- Design references inspected without importing code or runtime dependencies:
  `iannuttall/ralph` commit `5bc402540c45192bd1e9cacb84611ee2e5ba13a8`
  and `maveric/agent-framework` commit
  `abb87b0dda0fefbb86ccccdb9d19a7843fc909e3`.

## Reused

The existing Root knowledge plane, `executor_luna`, `executor_sol`, Tester,
Explorer, End-of-Session, Light/Medium/Heavy routes, bounded capsules, direct
executor↔tester repair, project documentation, context hygiene, cross-session
handoff, lifecycle migrations, update backups, and compensating atomic file
transaction remain authoritative. No second planner, verifier, repair system,
agent framework, Ralph dependency, or maveric dependency was introduced.

## Added

- Project-local `.orchestration/` machine state, separate from `agent_docs/`.
- Explicit finite phases from INIT through DONE/BLOCKED/FAILED.
- Heavy Plan import as a validated DAG with task dependencies, READY derivation,
  Acceptance Criteria, verification, required inputs, and write scopes.
- Dynamic Luna allocation capped at four, existing Sol cap enforcement, total
  platform-capacity enforcement, and an End-of-Session reservation.
- Normalized SHA-256 failure signatures, persistent failure/event/run memory,
  three-attempt Luna budget, same-signature threshold two, two Sol escalation
  budget, stagnation signals, and twelve macro iterations.
- Recovery of missing/stale running agents and objective verification reopening
  stored completion.
- Optional AUTO selection into the existing routes; manual route selection wins
  and unspecified work remains Light.
- Lifecycle CLI controls and requested/configured/catalog/runtime-separated
  model diagnostics.

## Model Routing

| Role | Requested | Package/session configuration | Runtime identity |
| --- | --- | --- | --- |
| Root | GPT-5.6 Sol, high | Main-session owned; deliberately absent from worker configuration | NOT_VERIFIED |
| `executor_luna` | GPT-5.6 Luna, max | Worker TOML and default config match | NOT_VERIFIED |
| `executor_sol` | GPT-5.6 Sol, high | Worker TOML matches; maximum active instance 1 | NOT_VERIFIED |

The Codex 0.149.1 bundled catalog accepts all requested model/effort pairs. A
functional Luna/max canary and Sol/high canary each returned their bounded
sentinel. The available agent diagnostics did not expose independent actual
child model/reasoning metadata, so a successful response is not upgraded to a
runtime-identity PASS and no silent fallback is claimed.

All shipped Luna-based support roles (`tester`, `doc-writer`, `explorer`, and
`end_of_session`) are also pinned to `max`. Every shipped worker explicitly uses
`service_tier = "default"`; Fast mode is never enabled implicitly by this
workflow.

## Verification Matrix

- A: trivial AUTO request resolves to Light with no agent.
- B: bounded, high-verification request uses one Luna, Tester, and verified
  End-of-Session closure.
- C: five READY packages allocate four Lunas; overlapping scopes serialize.
- D: first routine failure remains executor↔tester repair with no Root attention.
- E: the same normalized failure twice escalates to Sol diagnosis, then Luna
  implementation and Tester verification.
- F: restart reconciliation reopens a task whose recorded agent is absent.
- G: Luna≤4 and Sol≤1 coexist with Tester, Explorer, End-of-Session, and total
  workflow capacity 20 in the deterministic scheduler test.
- H: objective FAIL reopens a stored done task.

Additional checks cover cycle rejection, NOT_TESTED handling, macro budget,
event sequencing, corruption rejection, concurrent result transitions,
retry-memory preservation across replanning, blocker resolution, CLI lifecycle,
upstream lifecycle regression, package validation, deterministic archive
verification, and checksum generation.

## Known Boundaries

- `workflow_config.json` configures a cap of 20 child threads, while an actual
  Codex service/session may impose a lower capacity. The runtime remains the
  final authority; the Luna-only extension never raises that platform limit.
- The state layer records and validates dispatch; Codex itself creates agents.
  A spawn failure is recovered by `reconcile`, not hidden as a successful run.
- Model catalog acceptance and functional canary response are not actual model
  identity metadata. Use `--runtime-metadata` only with an independent trusted
  diagnostic artifact.
- `.orchestration/` is ignored by Git by default and preserved by removal. Its
  core files and run snapshots are included in update backups.
