"""Composition layer for user-level and project lifecycle plans."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import RUNTIME_SCHEMA_VERSION
from .backup import append_backup_mutations
from .config import WorkflowConfig, load_config, load_migrated_config
from .errors import ValidationError
from .layout import USER_STATE, PackageLayout, ProjectPaths, RuntimePaths
from .personalization import materialize_personalization as materialize_personalization
from .plan import (
    OperationPlan,
    deduplicate,
    json_mutation,
    read_json,
    read_string_list,
    resolve_owned_runtime_path,
)
from .project_ops import (
    plan_enable as plan_enable,
)
from .project_ops import (
    plan_personalize as plan_personalize,
)
from .project_ops import (
    plan_project_install,
    plan_project_remove,
    plan_project_update,
)
from .release import parse_semver
from .runtime_ops import (
    plan_installed_user_agents,
    plan_materialized_config,
    plan_runtime_files,
    plan_runtime_remove,
)
from .transaction import Mutation


def plan_bootstrap(
    package: PackageLayout, runtime: RuntimePaths, project: ProjectPaths
) -> OperationPlan:
    config = load_config(package.default_config, templates=package.agent_templates)
    mutations, owned_runtime = plan_runtime_files(
        package, runtime, config, config.to_json().encode()
    )
    project_plan = plan_project_install(package, project)
    mutations.extend(project_plan.mutations)
    state = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "version": package.version,
        "owned_runtime_files": sorted(owned_runtime),
        "owned_workers": sorted(package.worker_names),
    }
    mutations.append(json_mutation(runtime.runtime / USER_STATE, state))
    return OperationPlan(
        "bootstrap",
        deduplicate(mutations),
        project_plan.warnings,
        project_plan.agent_actions,
        {"version": package.version},
        cleanup_dirs=project_plan.cleanup_dirs,
    )


def plan_configure(
    runtime: RuntimePaths,
    changes: dict[str, Any],
) -> OperationPlan:
    templates = runtime.runtime / "templates" / "agents"
    current = load_config(runtime.runtime / "workflow_config.json", templates=templates)
    raw = current.to_mapping()
    raw.update({key: value for key, value in changes.items() if value is not None})
    if "enabled_workers" not in changes and changes.get("default_executor"):
        other = ({"executor_luna", "executor_terra"} - {changes["default_executor"]}).pop()
        raw["enabled_workers"] = [
            worker for worker in raw["enabled_workers"] if worker != other
        ]
        if changes["default_executor"] not in raw["enabled_workers"]:
            raw["enabled_workers"].insert(0, changes["default_executor"])
    available = {path.stem for path in templates.glob("*.toml")}
    proposed = WorkflowConfig.from_mapping(raw, available_workers=available)
    mutations = plan_materialized_config(runtime, proposed)
    if proposed.auto_check_update != current.auto_check_update:
        mutations.extend(plan_installed_user_agents(runtime, proposed))
    mutations.append(
        Mutation(runtime.runtime / "workflow_config.json", proposed.to_json().encode())
    )
    state = read_json(runtime.runtime / USER_STATE, default={})
    state.update(
        {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "version": (runtime.runtime / "VERSION").read_text(encoding="utf-8").strip(),
            "owned_workers": sorted(available),
        }
    )
    mutations.append(json_mutation(runtime.runtime / USER_STATE, state))
    return OperationPlan(
        "configure",
        deduplicate(mutations),
        [],
        [],
        {"configuration": proposed.to_mapping()},
    )


def plan_auto_check_update_setting(
    runtime: RuntimePaths, *, enabled: bool
) -> OperationPlan:
    templates = runtime.runtime / "templates" / "agents"
    current = load_config(runtime.runtime / "workflow_config.json", templates=templates)
    raw = current.to_mapping()
    raw["auto_check_update"] = enabled
    proposed = WorkflowConfig.from_mapping(
        raw,
        available_workers={path.stem for path in templates.glob("*.toml")},
    )
    mutations = [
        Mutation(
            runtime.runtime / "workflow_config.json",
            proposed.to_json().encode(),
        )
    ]
    mutations.extend(plan_installed_user_agents(runtime, proposed))
    return OperationPlan(
        "set-auto-check-update",
        mutations,
        [],
        [],
        {"auto_check_update": enabled},
    )


def plan_remove(
    runtime: RuntimePaths,
    project: ProjectPaths,
) -> OperationPlan:
    runtime_mutations, runtime_dirs, runtime_warnings = plan_runtime_remove(runtime)
    project_mutations, project_dirs, project_warnings = plan_project_remove(project)
    return OperationPlan(
        "remove",
        deduplicate(runtime_mutations + project_mutations),
        runtime_warnings + project_warnings,
        [],
        {
            "confirmation_required": True,
            "preserves": [
                "project agent_docs/ files",
                "unrelated user AGENTS.md content",
                "unrelated Codex config.toml keys",
                "unrelated worker TOMLs",
            ],
        },
        cleanup_dirs=runtime_dirs + project_dirs,
    )


def plan_update(
    incoming: PackageLayout,
    runtime: RuntimePaths,
    project: ProjectPaths,
    *,
    legacy_local_instructions: str | None = None,
) -> OperationPlan:
    installed = PackageLayout.resolve(runtime.runtime, allow_legacy=True)
    project_installed = _project_installed_package(installed, runtime, project)
    config = load_migrated_config(
        runtime.runtime / "workflow_config.json",
        defaults=incoming.default_config,
        templates=incoming.agent_templates,
    )
    backup_root = (
        runtime.runtime
        / ".backups"
        / f"{installed.version}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
    )
    mutations: list[Mutation] = []
    append_backup_mutations(mutations, backup_root, runtime, project)
    runtime_mutations, owned_runtime = plan_runtime_files(
        incoming, runtime, config, config.to_json().encode()
    )
    mutations.extend(runtime_mutations)
    project_mutations, warnings = plan_project_update(
        project_installed,
        incoming,
        project,
        legacy_local_instructions=legacy_local_instructions,
    )
    mutations.extend(project_mutations)
    previous_state = read_json(runtime.runtime / USER_STATE, default={})
    incoming_targets = {
        mutation.path.resolve(strict=False) for mutation in runtime_mutations
    }
    for relative in read_string_list(previous_state, "owned_runtime_files"):
        obsolete = resolve_owned_runtime_path(runtime.runtime, relative)
        if obsolete not in incoming_targets and obsolete.exists():
            mutations.append(Mutation(obsolete, None))
    state = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "version": incoming.version,
        "owned_runtime_files": sorted(owned_runtime),
        "owned_workers": sorted(incoming.worker_names),
    }
    mutations.append(json_mutation(runtime.runtime / USER_STATE, state))
    return OperationPlan(
        "update",
        deduplicate(mutations),
        warnings,
        [],
        {
            "from_version": installed.version,
            "to_version": incoming.version,
            "project_from_version": project_installed.version,
            "backup": str(backup_root),
        },
    )


def _project_installed_package(
    installed: PackageLayout,
    runtime: RuntimePaths,
    project: ProjectPaths,
) -> PackageLayout:
    """Resolve the package version that produced this project's entry point."""

    if not project.active.exists() and not project.disabled.exists():
        return installed
    state = read_json(project.state, default={})
    version = state.get("workflow_version")
    if version is None:
        # Pre-state installations can only be compared with the currently
        # installed source, retaining the legacy migration behavior.
        return installed
    if not isinstance(version, str) or not version:
        raise ValidationError("project workflow_version state must be a non-empty string")
    parse_semver(version)
    if version == installed.version:
        return installed
    source_backups = (runtime.runtime / ".source_backup").resolve()
    historical_root = (source_backups / version).resolve()
    try:
        historical_root.relative_to(source_backups)
    except ValueError as error:
        raise ValidationError("project workflow_version resolves outside source backups") from error
    if not historical_root.is_dir():
        raise ValidationError(
            "the historical workflow source for this project is missing: "
            f"{historical_root}; restore it from backup before updating the project"
        )
    historical = PackageLayout.resolve(historical_root, allow_legacy=True)
    if historical.version != version:
        raise ValidationError(
            "project workflow state and historical source backup versions disagree"
        )
    return historical
