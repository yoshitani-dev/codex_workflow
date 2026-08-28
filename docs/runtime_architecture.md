# Lifecycle Runtime Architecture

The lifecycle runtime separates immutable release inputs, mutable installed
state, generated outputs, and project-owned content.

## Data ownership

- `codex_workflow/resources/`: immutable defaults distributed by a release.
- `~/.codex/codex_workflow/workflow_config.json`: mutable installed state.
- Heavy snapshots, the End-of-Session fork value, all worker TOMLs, and
  workflow-owned Codex settings: generated outputs; never sources of truth.
- Project personalization: structured project state materialized into its own
  marker region.
- Project-local instructions: opaque preserved content in a separate marker
  region.
- Project `.orchestration/`: extension-owned machine state derived from the
  Heavy Plan. It is separate from human-readable `agent_docs/` and preserved on
  workflow removal.

## Module boundaries

- `layout.py`: package and target path contracts.
- `config.py`: configuration schema and rendering.
- `migrations.py`: ordered persistent-resource migrations.
- `orchestration.py`: Heavy Plan DAG validation, state transitions, READY
  scheduling, bounded failure memory, escalation, and reality reconciliation.
- `model_canary.py`: requested/configured/catalog/runtime model diagnostics;
  catalog support is never reported as actual runtime identity.
- `markers.py`: strict text-region parsing and rendering.
- `project_ops.py`: project entry point, personalization, and documents.
- `runtime_ops.py`: user-level runtime and generated outputs.
- `backup.py`: persistent update backups.
- `transaction.py`: atomic file writes and compensating rollback.
- `plan.py`: validated mutation plans and compact summaries.
- `lifecycle.py`: composition only; it owns no low-level transformation.
- `release.py`: release selection, checksum, and safe extraction.
- `workflow.py`: CLI parsing, direct application, two-phase removal, and
  incoming-runtime delegation, plus the thin orchestration control surface.

The removal plan deletes the recognized project entry point and private
workflow resource, strips only the marked workflow region from the user-level
`AGENTS.md`, removes workflow-owned Codex settings and worker files, and
cleans the dedicated runtime directory. It deliberately preserves
`agent_docs/` and unrelated user-level content.

## Upgrade contract

1. The installed launcher selects and verifies the incoming release.
2. The verified incoming CLI validates and applies the update using the target
   version's runtime.
3. The mutable installed configuration is migrated into the incoming schema;
   package defaults supply only newly introduced fields. Generated worker
   surfaces are rendered from that preserved configuration.
4. Each project entry point is validated against the source backup for the
   workflow version recorded in its project state. Project-local regions,
   personalization, unrelated user files, and enabled/disabled state are
   preserved as opaque data.
5. Marker drift or ambiguous legacy content stops before live writes.
6. Every write command validates and applies one mutation plan with rollback.

Add a new migration without changing callers: register the transformation from
schema `N` to `N+1`, add fixtures for both versions, and keep the incoming
default at the new schema.
