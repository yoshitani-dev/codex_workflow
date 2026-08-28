#!/usr/bin/env python3
"""Deterministic codex_workflow lifecycle CLI.

Lifecycle commands validate and apply their mutations directly. The destructive
``remove`` command is the exception: it plans first and applies only with its
hidden confirmation flag. The hidden ``--apply`` option remains accepted for
compatibility with older launchers.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    raise SystemExit("codex_workflow requires Python 3.11 or newer")

from runtime.config import load_config
from runtime.errors import WorkflowError
from runtime.layout import PROJECT_ID
from runtime.model_canary import inspect_model_routing
from runtime.orchestration import (
    OrchestrationEngine,
    initial_orchestration_mutations,
    route_request,
)
from runtime.transaction import apply as apply_transaction
from runtime.lifecycle import (
    OperationPlan,
    PackageLayout,
    ProjectPaths,
    RuntimePaths,
    plan_bootstrap,
    plan_auto_check_update_setting,
    plan_configure,
    plan_enable,
    plan_personalize,
    plan_project_install,
    plan_remove,
    plan_update,
)
from runtime.release import (
    acquire,
    parse_semver,
    select_latest,
    select_releases,
    summarize_release_notes,
)


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _add_common(parser: argparse.ArgumentParser, *, project: bool = True) -> None:
    parser.add_argument("--codex-home", type=Path, default=_default_codex_home())
    if project:
        parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true", default=True, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="emit compact JSON")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    install = commands.add_parser("install")
    _add_common(install)
    # Retained for callers that have an extracted package available. This is
    # a read-only project-install source; install never bootstraps user files.
    install.add_argument("--package-root", type=Path, help=argparse.SUPPRESS)

    bootstrap = commands.add_parser("bootstrap", help=argparse.SUPPRESS)
    _add_common(bootstrap)
    bootstrap.add_argument(
        "--package-root", type=Path, default=Path(__file__).resolve().parent
    )

    update = commands.add_parser("update")
    _add_common(update)
    # Internal hand-off from an installed launcher; not a public prompt form.
    update.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    update.add_argument("--allow-downgrade", action="store_true")
    update.add_argument(
        "--legacy-local-instructions",
        type=Path,
        help="reviewed local instructions extracted from a legacy merged entry point",
    )

    remove = commands.add_parser("remove")
    _add_common(remove)
    remove.add_argument("--confirm", action="store_true", help=argparse.SUPPRESS)

    auto_check = commands.add_parser("auto-check-update")
    _add_common(auto_check, project=False)

    check_update = commands.add_parser("check-update")
    _add_common(check_update, project=False)

    for name in (
        "enable-auto-check-update",
        "disable-auto-check-update",
        # Compatibility aliases retained from releases that called a
        # notification-only check an automatic update.
        "enable-auto-update",
        "disable-auto-update",
    ):
        command = commands.add_parser(name)
        _add_common(command, project=False)

    configure = commands.add_parser("configure")
    _add_common(configure, project=False)
    configure.add_argument("--default-executor", choices=["executor_luna", "executor_terra"])
    configure.add_argument("--reasoning-effort", choices=["high", "xhigh", "max"])
    configure.add_argument("--max-workers", type=int)
    configure.add_argument("--max-sol", type=int)
    configure.add_argument("--report-size", type=int)
    configure.add_argument(
        "--auto-check-update",
        choices=["enabled", "disabled"],
        help=argparse.SUPPRESS,
    )

    personalize = commands.add_parser("personalize")
    _add_common(personalize)
    personalize.add_argument("--resource", type=Path, required=True)

    for name in ("enable", "disable"):
        command = commands.add_parser(name)
        _add_common(command)

    validate = commands.add_parser("validate")
    _add_common(validate, project=False)
    validate.add_argument("--package-root", type=Path, default=Path(__file__).resolve().parent)

    route = commands.add_parser("route", help="resolve an AUTO assessment to Light, Medium, or Heavy")
    _add_common(route, project=False)
    route.add_argument("--assessment", type=Path, required=True)
    route.add_argument("--manual-route", choices=["light", "medium", "heavy"])

    orchestrate = commands.add_parser("orchestrate", help="operate the persistent Heavy state machine")
    _add_common(orchestrate)
    orchestrate.add_argument(
        "action",
        choices=[
            "init",
            "status",
            "start",
            "import-plan",
            "begin-iteration",
            "schedule",
            "dispatch",
            "register-agent",
            "release-agent",
            "execution-result",
            "verify",
            "fail",
            "specialist-result",
            "reconcile",
            "close",
        ],
    )
    orchestrate.add_argument("--deployment-id")
    orchestrate.add_argument("--route", choices=["heavy", "auto"], default="heavy")
    orchestrate.add_argument("--git-head")
    orchestrate.add_argument("--new-deployment", action="store_true")
    orchestrate.add_argument("--payload", type=Path)
    orchestrate.add_argument("--task-id")
    orchestrate.add_argument("--agent-instance")
    orchestrate.add_argument("--role")
    orchestrate.add_argument("--write-scope", action="append", default=[])
    orchestrate.add_argument("--reason")
    orchestrate.add_argument("--closure-state", choices=["complete", "blocked", "failed"])
    orchestrate.add_argument("--end-of-session-status")

    canary = commands.add_parser("model-canary", help="inspect requested/configured/runtime model identity")
    _add_common(canary, project=False)
    canary.add_argument("--codex-bin", default="codex")
    canary.add_argument("--runtime-metadata", type=Path)
    return parser.parse_args()


def _paths(args: argparse.Namespace) -> tuple[RuntimePaths, ProjectPaths | None]:
    runtime = RuntimePaths(args.codex_home.expanduser().resolve())
    project = ProjectPaths(args.project.resolve()) if hasattr(args, "project") else None
    return runtime, project


def _emit(value: dict[str, object], *, compact: bool) -> None:
    if compact:
        print(json.dumps(value, separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


def _finish(plan: OperationPlan, args: argparse.Namespace) -> int:
    summary = plan.summary()
    summary["applied"] = True
    plan.apply()
    _emit(summary, compact=args.json)
    return 0


def _json_object(path: Path | None, description: str) -> dict[str, object]:
    if path is None:
        raise WorkflowError(f"{description} requires --payload")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"cannot read {description} payload {path}: {error}") from error
    if not isinstance(value, dict):
        raise WorkflowError(f"{description} payload must be a JSON object")
    return value


def _required(value: str | None, option: str) -> str:
    if not value:
        raise WorkflowError(f"{option} is required for this action")
    return value


def _orchestration_engine(
    runtime: RuntimePaths, project: ProjectPaths
) -> OrchestrationEngine:
    package_root = Path(__file__).resolve().parent
    installed = runtime.runtime
    source = installed if (installed / "VERSION").is_file() else package_root
    package = PackageLayout.resolve(source)
    mutations = initial_orchestration_mutations(
        project.root,
        workflow_version=package.version,
        config_text=(package.root / "resources" / "orchestration_config.default.json").read_text(
            encoding="utf-8"
        ),
    )
    if mutations:
        apply_transaction(mutations)
    workflow_config_path = installed / "workflow_config.json"
    if workflow_config_path.is_file():
        workflow_config = load_config(
            workflow_config_path, templates=installed / "templates" / "agents"
        )
    else:
        workflow_config = load_config(
            package.default_config, templates=package.agent_templates
        )
    return OrchestrationEngine(project.root, workflow_config)


def _run_orchestration(
    args: argparse.Namespace, runtime: RuntimePaths, project: ProjectPaths
) -> dict[str, object]:
    engine = _orchestration_engine(runtime, project)
    action = args.action
    if action in {"init", "status"}:
        return engine.status()
    if action == "start":
        return engine.start_deployment(
            _required(args.deployment_id, "--deployment-id"),
            route=args.route,
            git_head=args.git_head,
            new_deployment=args.new_deployment,
        )
    if action == "import-plan":
        return engine.import_heavy_plan(_json_object(args.payload, "Heavy Plan"))
    if action == "begin-iteration":
        return engine.begin_iteration()
    if action == "schedule":
        return engine.schedule()
    if action == "dispatch":
        return engine.dispatch(
            _required(args.task_id, "--task-id"),
            agent_instance=_required(args.agent_instance, "--agent-instance"),
        )
    if action == "register-agent":
        return engine.register_auxiliary_agent(
            role=_required(args.role, "--role"),
            agent_instance=_required(args.agent_instance, "--agent-instance"),
            task_id=args.task_id,
            write_scope=args.write_scope,
        )
    if action == "release-agent":
        return engine.release_agent(
            _required(args.agent_instance, "--agent-instance"), reason=args.reason or "completed"
        )
    if action == "execution-result":
        return engine.record_execution_result(
            _required(args.task_id, "--task-id"), _json_object(args.payload, "execution result")
        )
    if action == "verify":
        return engine.record_verification(
            _required(args.task_id, "--task-id"), _json_object(args.payload, "verification")
        )
    if action == "fail":
        return engine.record_failure(
            _required(args.task_id, "--task-id"), _json_object(args.payload, "failure")
        )
    if action == "specialist-result":
        return engine.record_specialist_result(
            _required(args.task_id, "--task-id"), _json_object(args.payload, "specialist result")
        )
    if action == "reconcile":
        return engine.reconcile(_json_object(args.payload, "reality"))
    if action == "close":
        return engine.close(
            closure_state=_required(args.closure_state, "--closure-state"),
            end_of_session_status=args.end_of_session_status,
            reason=args.reason,
        )
    raise WorkflowError(f"unsupported orchestration action: {action}")


def _project_workflow_entry(project: ProjectPaths) -> Path | None:
    """Return an existing recognized active or disabled project entry point."""

    for path in (project.active, project.disabled):
        if path.is_file() and PROJECT_ID in path.read_text(encoding="utf-8"):
            return path
    return None


def _delegate_update(incoming: PackageLayout, args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        "-B",
        str(incoming.root / "workflow.py"),
        "update",
        "--source",
        str(incoming.root),
        "--codex-home",
        str(args.codex_home),
        "--project",
        str(args.project),
    ]
    if args.allow_downgrade:
        command.append("--allow-downgrade")
    if args.legacy_local_instructions:
        command.extend(
            ["--legacy-local-instructions", str(args.legacy_local_instructions)]
        )
    if args.apply:
        command.append("--apply")
    if args.json:
        command.append("--json")
    completed = subprocess.run(command, check=False)
    return completed.returncode


def main() -> int:
    args = parse_args()
    temporary = None
    try:
        runtime, project = _paths(args)
        if args.command == "route":
            _emit(
                route_request(
                    _json_object(args.assessment, "route assessment"), args.manual_route
                ),
                compact=args.json,
            )
            return 0
        if args.command == "model-canary":
            installed_templates = runtime.runtime / "templates" / "agents"
            templates = (
                installed_templates
                if installed_templates.is_dir()
                else Path(__file__).resolve().parent / "agents"
            )
            _emit(
                inspect_model_routing(
                    agent_templates=templates,
                    codex_bin=args.codex_bin,
                    runtime_metadata=args.runtime_metadata,
                ),
                compact=args.json,
            )
            return 0
        if args.command == "orchestrate":
            assert project is not None
            _emit(_run_orchestration(args, runtime, project), compact=args.json)
            return 0
        if args.command == "validate":
            package = PackageLayout.resolve(args.package_root)
            _emit(
                {
                    "valid": True,
                    "version": package.version,
                    "workers": sorted(package.worker_names),
                },
                compact=args.json,
            )
            return 0
        if args.command == "auto-check-update":
            config = load_config(
                runtime.runtime / "workflow_config.json",
                templates=runtime.runtime / "templates" / "agents",
            )
            if not config.auto_check_update:
                _emit(
                    {"status": "disabled", "installed": None, "available": None},
                    compact=args.json,
                )
                return 0
            installed_text = (runtime.runtime / "VERSION").read_text(encoding="utf-8").strip()
            installed = parse_semver(installed_text)
            selected = select_latest()
            status = "current" if selected.version == installed else (
                "update available" if selected.version > installed else "installed newer"
            )
            _emit(
                {
                    "status": status,
                    "installed": installed_text,
                    "available": selected.version_text,
                    "asset": selected.zip_name,
                },
                compact=args.json,
            )
            return 0
        if args.command == "check-update":
            installed_text = (runtime.runtime / "VERSION").read_text(encoding="utf-8").strip()
            installed = parse_semver(installed_text)
            releases = select_releases()
            newer = [release for release in releases if release.version > installed]
            latest = releases[0]
            updates = [
                {
                    "version": release.version_text,
                    "asset": release.zip_name,
                    "release_url": release.release_url,
                    "release_notes": release.release_notes,
                    "summary": summarize_release_notes(release.release_notes),
                }
                for release in newer
            ]
            if newer:
                status = "update available"
                summary = "\n".join(
                    f"{item['version']}: {item['summary']}" for item in updates
                )
            elif latest.version == installed:
                status = "current"
                summary = "The installed workflow is current."
            else:
                status = "installed newer"
                summary = "The installed workflow is newer than the latest release."
            _emit(
                {
                    "status": status,
                    "installed": installed_text,
                    "available": latest.version_text,
                    "asset": latest.zip_name,
                    "summary": summary,
                    "updates": updates,
                },
                compact=args.json,
            )
            return 0
        if args.command == "remove":
            assert project is not None
            plan = plan_remove(runtime, project)
            if not args.confirm:
                summary = plan.summary()
                summary["applied"] = False
                summary["confirmation_required"] = True
                _emit(summary, compact=args.json)
                return 0
            return _finish(plan, args)
        if args.command in {
            "enable-auto-check-update",
            "disable-auto-check-update",
            "enable-auto-update",
            "disable-auto-update",
        }:
            return _finish(
                plan_auto_check_update_setting(
                    runtime,
                    enabled=args.command in {
                        "enable-auto-check-update",
                        "enable-auto-update",
                    },
                ),
                args,
            )
        if args.command == "bootstrap":
            assert project is not None
            package = PackageLayout.resolve(args.package_root)
            return _finish(plan_bootstrap(package, runtime, project), args)
        if args.command == "install":
            assert project is not None
            if project.active.exists() and project.disabled.exists():
                raise WorkflowError("both active and disabled project entry points exist")
            if (runtime.runtime / "VERSION").is_file():
                package = PackageLayout.resolve(runtime.runtime)
            elif args.package_root is not None:
                package = PackageLayout.resolve(args.package_root)
            else:
                raise WorkflowError(
                    "the user-level workflow bootstrap is not installed; "
                    "complete the initial bootstrap before installing a project"
                )
            existing = _project_workflow_entry(project)
            if existing is not None:
                # Validate the recognized entry before reporting a no-op. This
                # turns stale, malformed, or personalization-drifted installs
                # into actionable errors instead of misreporting them as merely
                # disabled.
                existing_plan = plan_project_install(package, project)
                if existing_plan.agent_actions[0]["files"]:
                    return _finish(existing_plan, args)
                enabled = existing == project.active
                _emit(
                    {
                        "applied": False,
                        "status": "already enabled" if enabled else "already disabled",
                        "instruction": (
                            "No action is required."
                            if enabled
                            else "Run `codex_workflow --enable` to reactivate it."
                        ),
                    },
                    compact=args.json,
                )
                return 0
            return _finish(plan_project_install(package, project), args)
        if args.command == "update":
            assert project is not None
            if args.source:
                incoming = PackageLayout.resolve(args.source)
            else:
                selected = select_latest()
                temporary, package_path = acquire(selected)
                incoming = PackageLayout.resolve(package_path)
            if incoming.root != Path(__file__).resolve().parent:
                return _delegate_update(incoming, args)
            installed_text = (runtime.runtime / "VERSION").read_text(encoding="utf-8").strip()
            if parse_semver(incoming.version) < parse_semver(installed_text) and not args.allow_downgrade:
                raise WorkflowError("incoming version is older; pass --allow-downgrade after approval")
            legacy_local = (
                args.legacy_local_instructions.read_text(encoding="utf-8")
                if args.legacy_local_instructions
                else None
            )
            return _finish(
                plan_update(
                    incoming,
                    runtime,
                    project,
                    legacy_local_instructions=legacy_local,
                ),
                args,
            )
        if args.command == "configure":
            changes = {
                "default_executor": args.default_executor,
                "default_executor_reasoning_effort": args.reasoning_effort,
                "max_concurrent_workers": args.max_workers,
                "max_executor_sol_instances": args.max_sol,
                "report_package_size": args.report_size,
                "auto_check_update": (
                    args.auto_check_update == "enabled"
                    if args.auto_check_update is not None
                    else None
                ),
            }
            return _finish(plan_configure(runtime, changes), args)
        if args.command == "personalize":
            assert project is not None
            resource = args.resource.read_text(encoding="utf-8")
            return _finish(plan_personalize(project, resource), args)
        if args.command in {"enable", "disable"}:
            assert project is not None
            return _finish(plan_enable(project, enable=args.command == "enable"), args)
        raise WorkflowError(f"unsupported command: {args.command}")
    except (OSError, WorkflowError) as error:
        _emit({"error": str(error), "applied": False}, compact=getattr(args, "json", False))
        return 1
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
