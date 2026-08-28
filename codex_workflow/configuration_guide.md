# Configure the Workflow

Run this procedure only when the user's trimmed message is exactly:

    codex_workflow --configure

The lifecycle CLI is:

    ~/.codex/codex_workflow/workflow.py

It requires Python 3.11 or newer and applies the validated configuration
directly.

## Configuration menu

Read the current values from
`~/.codex/codex_workflow/workflow_config.json`. Do not walk through every
setting sequentially. Instead, display this complete selectable menu, showing
the current value beside each setting and keeping **Exit** as the final option:

The installed file is mutable state. Package defaults and migration fallbacks
come from `~/.codex/codex_workflow/resources/workflow_config.default.json` and
must not be edited as user configuration.

1. Default executor: `executor_luna` or `executor_terra`.
2. Default-executor reasoning effort: `high`, `xhigh`, or `max`.
3. Maximum concurrent workers, from 1 through the current platform limit of 20.
4. Maximum concurrent `executor_sol` instances.
5. Maximum worker final-report size in words.
6. Exit.

The Luna-only active limit is deliberately not part of this upstream-compatible
menu. It lives in each project's extension-owned
`.orchestration/config.json` as `max_luna_executors` (default `4`). Total child
capacity remains `max_concurrent_workers`, while `executor_sol` capacity remains
the existing `max_executor_sol_instances` setting.

Ask the user to select one menu item. For a setting, ask only the follow-up
needed for a valid value, allow **Keep current**, and then return to the full
menu with refreshed current values. Continue until the user selects **Exit**.
If no setting changed, exit without running the lifecycle CLI.

The automatic session-start update check is controlled explicitly by
`codex_workflow --enable_auto_check_update` and
`codex_workflow --disable_auto_check_update`; it is not part of this menu. Do
not edit any live file directly. The former `--enable_auto_update` and
`--disable_auto_update` forms remain compatibility aliases.

## Plan and apply

After the user selects **Exit**, run
`python3 ~/.codex/codex_workflow/workflow.py configure` once with only the
changed flags:

```text
--default-executor <name>
--reasoning-effort <effort>
--max-workers <count>
--max-sol <count>
--report-size <words>
```

Run it with `--json` after collecting the requested values. The command
validates and applies the complete configuration in one operation.

The script validates the configuration, keeps `doc-writer` and
`end_of_session` enabled as required system roles, renders the Heavy snapshot,
synchronizes all distributed worker TOMLs, removes only obsolete manifest-owned
workers, and patches only workflow-owned Codex settings. The End-of-Session
handoff is integrated and automatic; it is not user-configurable. Report the
result and tell the user to restart Codex when worker definitions or platform
settings changed.
