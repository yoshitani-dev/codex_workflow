# Upstream relationship

This fork's stateful orchestration implementation was developed from
`viettran-edgeAI/codex_workflow` version 1.1.3 and retains its internal workflow
identifier so existing installations can update in place.

Upstream version 1.1.4 replaces the worker topology, role names, configuration
surface, and lifecycle behavior. It is therefore not a safe fast-forward or
drop-in merge for this fork's 1.2.1 architecture. Treating it as an outstanding
patch would risk undoing the bounded Luna `max` executor contract and the
stateful orchestration behavior added here.

The release-workflow safety fix from upstream 1.1.4 is applied independently:
manual release runs check out the requested tag and publish assets against the
actual checked-out commit. Future upstream changes should be reviewed and
ported by behavior, with the full test, lint, type-check, and deterministic
package suite run before acceptance.
