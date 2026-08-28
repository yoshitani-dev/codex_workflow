"""Persistent Heavy-route orchestration state and deterministic scheduling.

This module is a thin control-plane extension over the prompt-driven worker
roles in ``codex_workflow``.  It does not plan work or launch agents.  The main
agent remains the planner and Codex remains the worker runtime; this module
validates and persists the Heavy Plan, derives READY work, enforces executor
capacity/write-scope constraints, and records verification and failures.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import WorkflowConfig
from .errors import ValidationError
from .transaction import Mutation
from .transaction import apply as apply_transaction

ORCHESTRATION_SCHEMA_VERSION = 1
ORCHESTRATION_DIR = ".orchestration"
PHASES = {
    "INIT",
    "DISCOVER",
    "PLAN",
    "READY",
    "DISPATCH",
    "EXECUTE",
    "VERIFY",
    "CLASSIFY",
    "REPLAN",
    "ESCALATE",
    "DONE",
    "BLOCKED",
    "FAILED",
}
TASK_STATUSES = {"planned", "ready", "running", "verifying", "done", "failed", "blocked"}
AC_STATUSES = {"PASS", "FAIL", "NOT_TESTED"}
WRITE_ROLES = {"executor_luna", "executor_terra", "executor_sol", "tester", "doc-writer"}
EXECUTOR_ROLES = {"executor_luna", "executor_terra", "executor_sol"}
HARD_FAILURE_CLASSES = {
    "architecture",
    "concurrency",
    "contract",
    "cross_module",
    "migration",
    "security",
    "unknown_root_cause",
    "conflicting_evidence",
    "scope_expansion",
}
BLOCKER_FAILURE_CLASSES = {
    "authentication",
    "blocker",
    "external",
    "missing_input",
    "permission",
    "unsafe_operation",
    "unavailable_dependency",
    "user_decision",
}

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "INIT": {"DISCOVER", "PLAN", "BLOCKED", "FAILED"},
    "DISCOVER": {"PLAN", "REPLAN", "BLOCKED", "FAILED"},
    "PLAN": {"READY", "REPLAN", "BLOCKED", "FAILED"},
    "READY": {"DISPATCH", "EXECUTE", "VERIFY", "CLASSIFY", "REPLAN", "BLOCKED", "FAILED"},
    "DISPATCH": {"READY", "EXECUTE", "REPLAN", "BLOCKED", "FAILED"},
    "EXECUTE": {"READY", "VERIFY", "CLASSIFY", "REPLAN", "BLOCKED", "FAILED"},
    "VERIFY": {"READY", "EXECUTE", "CLASSIFY", "REPLAN", "BLOCKED", "FAILED"},
    "CLASSIFY": {"READY", "REPLAN", "ESCALATE", "BLOCKED", "FAILED"},
    "REPLAN": {"PLAN", "READY", "DISPATCH", "EXECUTE", "VERIFY", "DONE", "BLOCKED", "FAILED"},
    "ESCALATE": {"DISPATCH", "EXECUTE", "READY", "REPLAN", "VERIFY", "BLOCKED", "FAILED"},
    "BLOCKED": {"DISCOVER", "PLAN", "READY", "REPLAN", "FAILED"},
    "FAILED": {"DISCOVER", "PLAN", "REPLAN"},
    # Objective evidence may invalidate a previous completion.
    "DONE": {"REPLAN"},
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _require_int(value: Any, name: str, *, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValidationError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class OrchestrationConfig:
    schema_version: int
    max_luna_executors: int
    max_luna_attempts_per_task: int
    same_failure_limit: int
    max_sol_escalations_per_task: int
    max_macro_iterations: int
    stale_running_seconds: int
    reserve_end_of_session_slot: bool

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> OrchestrationConfig:
        expected = {
            "schema_version",
            "max_luna_executors",
            "max_luna_attempts_per_task",
            "same_failure_limit",
            "max_sol_escalations_per_task",
            "max_macro_iterations",
            "stale_running_seconds",
            "reserve_end_of_session_slot",
        }
        unknown = set(raw) - expected
        missing = expected - set(raw)
        if unknown or missing:
            raise ValidationError(
                "invalid orchestration config keys; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        if raw["schema_version"] != ORCHESTRATION_SCHEMA_VERSION:
            raise ValidationError("unsupported orchestration config schema_version")
        reserve = raw["reserve_end_of_session_slot"]
        if not isinstance(reserve, bool):
            raise ValidationError("reserve_end_of_session_slot must be a boolean")
        return cls(
            ORCHESTRATION_SCHEMA_VERSION,
            _require_int(raw["max_luna_executors"], "max_luna_executors"),
            _require_int(
                raw["max_luna_attempts_per_task"],
                "max_luna_attempts_per_task",
            ),
            _require_int(raw["same_failure_limit"], "same_failure_limit"),
            _require_int(
                raw["max_sol_escalations_per_task"],
                "max_sol_escalations_per_task",
            ),
            _require_int(raw["max_macro_iterations"], "max_macro_iterations"),
            _require_int(raw["stale_running_seconds"], "stale_running_seconds"),
            reserve,
        )

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrchestrationPaths:
    project_root: Path

    @property
    def root(self) -> Path:
        return self.project_root / ORCHESTRATION_DIR

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def tasks(self) -> Path:
        return self.root / "tasks.json"

    @property
    def failures(self) -> Path:
        return self.root / "failures.json"

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def lock(self) -> Path:
        return self.root / ".lock"


def initial_orchestration_mutations(
    project_root: Path,
    *,
    workflow_version: str,
    config_text: str,
    now: str | None = None,
) -> list[Mutation]:
    """Create missing machine-state files without replacing existing state."""

    paths = OrchestrationPaths(project_root)
    if paths.root.is_symlink() or (paths.root.exists() and not paths.root.is_dir()):
        raise ValidationError(f"orchestration path is not a regular directory: {paths.root}")
    try:
        raw_config = json.loads(config_text)
    except json.JSONDecodeError as error:
        raise ValidationError(f"invalid default orchestration config: {error}") from error
    if not isinstance(raw_config, dict):
        raise ValidationError("default orchestration config root must be an object")
    config = OrchestrationConfig.from_mapping(raw_config)
    timestamp = now or utc_now()
    defaults: dict[Path, bytes] = {
        paths.config: _json_bytes(config.to_mapping()),
        paths.state: _json_bytes(
            {
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "workflow_version": workflow_version,
                "deployment_id": None,
                "route": None,
                "phase": "INIT",
                "macro_iteration": 0,
                "closure_ready": False,
                "active_agents": [],
                "blocker": None,
                "failure": None,
                "git_head": None,
                "event_sequence": 0,
                "created_at": timestamp,
                "updated_at": timestamp,
                "completed_at": None,
            }
        ),
        paths.tasks: _json_bytes(
            {
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "source": "heavy_plan",
                "plan_revision": 0,
                "plan_id": None,
                "tasks": [],
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        ),
        paths.failures: _json_bytes(
            {
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "failures": [],
                "updated_at": timestamp,
            }
        ),
        paths.events: b"",
        paths.runs / ".keep": b"",
    }
    existing = [path for path in defaults if path.exists()]
    missing = [path for path in defaults if not path.exists()]
    if existing and missing:
        raise ValidationError(
            "partial orchestration state exists; recover the missing files before installation: "
            + ", ".join(str(path) for path in missing)
        )
    return [] if existing else [Mutation(path, content) for path, content in defaults.items()]


class OrchestrationStore:
    """Atomic multi-file persistence with a cross-process advisory lock."""

    def __init__(self, project_root: Path):
        self.paths = OrchestrationPaths(project_root.resolve())

    @contextlib.contextmanager
    def locked(self, timeout_seconds: float = 10.0) -> Iterator[None]:
        if not self.paths.root.is_dir():
            raise ValidationError(
                f"orchestration state is not initialized: {self.paths.root}"
            )
        handle = self.paths.lock.open("a+b")
        if self.paths.lock.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        locked = False
        try:
            while not locked:
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl: Any = importlib.import_module("fcntl")
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise ValidationError(
                            "timed out waiting for orchestration state lock"
                        ) from None
                    time.sleep(0.05)
            yield
        finally:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl = importlib.import_module("fcntl")
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def load(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], OrchestrationConfig]:
        state = self._read_json(self.paths.state)
        tasks = self._read_json(self.paths.tasks)
        failures = self._read_json(self.paths.failures)
        config_raw = self._read_json(self.paths.config)
        self._validate_roots(state, tasks, failures)
        self._validate_event_stream(state)
        return state, tasks, failures, OrchestrationConfig.from_mapping(config_raw)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValidationError(f"cannot load orchestration file {path}: {error}") from error
        if not isinstance(value, dict):
            raise ValidationError(f"orchestration file root must be an object: {path}")
        return value

    @staticmethod
    def _validate_roots(
        state: dict[str, Any], tasks: dict[str, Any], failures: dict[str, Any]
    ) -> None:
        for name, value in (("state", state), ("tasks", tasks), ("failures", failures)):
            if value.get("schema_version") != ORCHESTRATION_SCHEMA_VERSION:
                raise ValidationError(f"unsupported {name} orchestration schema_version")
        if state.get("phase") not in PHASES:
            raise ValidationError(f"invalid orchestration phase: {state.get('phase')!r}")
        if not isinstance(state.get("active_agents"), list):
            raise ValidationError("state.active_agents must be a list")
        if not isinstance(state.get("macro_iteration"), int) or isinstance(
            state.get("macro_iteration"), bool
        ) or state["macro_iteration"] < 0:
            raise ValidationError("state.macro_iteration must be a non-negative integer")
        if not isinstance(state.get("closure_ready"), bool):
            raise ValidationError("state.closure_ready must be a boolean")
        if not isinstance(state.get("event_sequence"), int) or isinstance(
            state.get("event_sequence"), bool
        ) or state["event_sequence"] < 0:
            raise ValidationError("state.event_sequence must be a non-negative integer")
        instance_ids: set[str] = set()
        for agent in state["active_agents"]:
            if not isinstance(agent, dict) or not isinstance(agent.get("instance_id"), str):
                raise ValidationError("state.active_agents entries require instance_id")
            if agent["instance_id"] in instance_ids:
                raise ValidationError("state.active_agents contains duplicate instance IDs")
            instance_ids.add(agent["instance_id"])
        if tasks.get("source") != "heavy_plan" or not isinstance(tasks.get("tasks"), list):
            raise ValidationError("tasks.json must be a Heavy Plan representation")
        required_task_keys = {
            "id",
            "title",
            "description",
            "status",
            "dependencies",
            "acceptance_criteria",
            "verification",
            "owner",
            "write_scope",
            "attempts",
            "failure_signature",
            "result",
            "created_at",
            "updated_at",
        }
        for task in tasks["tasks"]:
            if not isinstance(task, dict) or not required_task_keys.issubset(task):
                raise ValidationError("persisted task is missing required schema fields")
            if task["status"] not in TASK_STATUSES:
                raise ValidationError(f"persisted task has invalid status: {task['status']!r}")
            if task["owner"] not in EXECUTOR_ROLES:
                raise ValidationError(f"persisted task has invalid owner: {task['owner']!r}")
            if not isinstance(task["dependencies"], list) or not all(
                isinstance(item, str) for item in task["dependencies"]
            ):
                raise ValidationError("persisted task dependencies must be a string list")
            if not isinstance(task["write_scope"], list) or not all(
                isinstance(item, str) for item in task["write_scope"]
            ):
                raise ValidationError("persisted task write_scope must be a string list")
            for scope in task["write_scope"]:
                _normalize_scope(scope)
            if not isinstance(task["attempts"], int) or isinstance(task["attempts"], bool) or task["attempts"] < 0:
                raise ValidationError("persisted task attempts must be a non-negative integer")
            criteria = task["acceptance_criteria"]
            if not isinstance(criteria, list) or not criteria:
                raise ValidationError("persisted task requires acceptance criteria")
            if any(
                not isinstance(item, dict) or item.get("status") not in AC_STATUSES
                for item in criteria
            ):
                raise ValidationError("persisted acceptance criterion has invalid status")
            verification = task["verification"]
            if not isinstance(verification, dict) or verification.get("status") not in AC_STATUSES:
                raise ValidationError("persisted task verification has invalid status")
        validate_task_dag(tasks["tasks"])
        if not isinstance(failures.get("failures"), list):
            raise ValidationError("failures.failures must be a list")
        for failure in failures["failures"]:
            if (
                not isinstance(failure, dict)
                or not re.fullmatch(r"[0-9a-f]{64}", str(failure.get("signature", "")))
                or not isinstance(failure.get("count"), int)
                or isinstance(failure.get("count"), bool)
                or failure["count"] < 1
            ):
                raise ValidationError("failure memory contains an invalid entry")

    def _validate_event_stream(self, state: dict[str, Any]) -> None:
        try:
            text = self.paths.events.read_text(encoding="utf-8")
        except OSError as error:
            raise ValidationError(f"cannot load orchestration events: {error}") from error
        if text and not text.endswith("\n"):
            raise ValidationError("events.jsonl is truncated")
        expected = 0
        for line in text.splitlines():
            expected += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValidationError(f"events.jsonl contains invalid JSON: {error}") from error
            if not isinstance(event, dict) or event.get("seq") != expected:
                raise ValidationError("events.jsonl sequence is not contiguous")
        if state.get("event_sequence") != expected:
            raise ValidationError("state/event sequence mismatch; recover orchestration state")

    def commit(
        self,
        state: dict[str, Any],
        tasks: dict[str, Any],
        failures: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        timestamp: str,
        snapshot_label: str | None = None,
    ) -> None:
        state["updated_at"] = timestamp
        tasks["updated_at"] = timestamp
        failures["updated_at"] = timestamp
        current_events = self.paths.events.read_text(encoding="utf-8")
        if current_events and not current_events.endswith("\n"):
            raise ValidationError("events.jsonl is truncated; recover it before continuing")
        rendered_events = current_events
        sequence = state.get("event_sequence", 0)
        if not isinstance(sequence, int) or sequence < 0:
            raise ValidationError("state.event_sequence must be a non-negative integer")
        for event in events:
            sequence += 1
            item = {
                "seq": sequence,
                "timestamp": timestamp,
                "deployment_id": state.get("deployment_id"),
                "macro_iteration": state.get("macro_iteration", 0),
                **event,
            }
            rendered_events += json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
        state["event_sequence"] = sequence
        mutations = [
            Mutation(self.paths.state, _json_bytes(state)),
            Mutation(self.paths.tasks, _json_bytes(tasks)),
            Mutation(self.paths.failures, _json_bytes(failures)),
            Mutation(self.paths.events, rendered_events.encode("utf-8")),
        ]
        if snapshot_label is not None:
            label = re.sub(r"[^a-z0-9_-]+", "-", snapshot_label.lower()).strip("-")
            if not label:
                raise ValidationError("snapshot label is empty after normalization")
            snapshot = {
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "captured_at": timestamp,
                "state": state,
                "tasks": tasks,
                "failure_signatures": [
                    {"signature": item.get("signature"), "count": item.get("count")}
                    for item in failures["failures"]
                ],
            }
            name = f"{state.get('macro_iteration', 0):03d}-{sequence:06d}-{label}.json"
            mutations.append(Mutation(self.paths.runs / name, _json_bytes(snapshot)))
        apply_transaction(mutations)


def _normalize_scope(scope: str) -> tuple[str, bool]:
    if not isinstance(scope, str) or not scope.strip():
        raise ValidationError("write_scope entries must be non-empty strings")
    value = scope.strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if value.startswith("/") or value == ".." or value.startswith("../"):
        raise ValidationError(f"write_scope must be project-relative: {scope!r}")
    wildcard = any(character in value for character in "*?[") or value.endswith("/")
    prefix = re.split(r"[*?[\]]", value, maxsplit=1)[0].rstrip("/")
    return prefix or ".", wildcard


def write_scopes_conflict(left: list[str], right: list[str]) -> bool:
    """Conservatively detect overlapping project-relative write surfaces."""

    for left_raw in left:
        left_prefix, left_wild = _normalize_scope(left_raw)
        for right_raw in right:
            right_prefix, right_wild = _normalize_scope(right_raw)
            if left_prefix == "." or right_prefix == ".":
                return True
            if left_prefix == right_prefix:
                return True
            if left_wild and right_prefix.startswith(left_prefix + "/"):
                return True
            if right_wild and left_prefix.startswith(right_prefix + "/"):
                return True
    return False


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b")
_ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b")
_HEX_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]+\b")
_STACK_LINE = re.compile(r"(\.[A-Za-z0-9]{1,8}):\d+(?::\d+)?")


def normalize_failure_text(value: Any, *, project_root: Path | None = None) -> str:
    text = "" if value is None else str(value)
    text = _ANSI.sub("", text).replace("\\", "/")
    if project_root is not None:
        root = str(project_root.resolve()).replace("\\", "/")
        text = text.replace(root, "<project>")
    text = _UUID.sub("<uuid>", text)
    text = _ISO_TIME.sub("<timestamp>", text)
    text = _HEX_ADDRESS.sub("<hex>", text)
    text = _STACK_LINE.sub(r"\1:<line>", text)
    return " ".join(text.split()).strip()


def failure_signature(packet: dict[str, Any], *, project_root: Path | None = None) -> tuple[str, dict[str, str]]:
    fields = {
        "failed_test": normalize_failure_text(packet.get("failed_test"), project_root=project_root),
        "exception_type": normalize_failure_text(packet.get("exception_type"), project_root=project_root),
        "message": normalize_failure_text(
            packet.get("normalized_error_message", packet.get("message")),
            project_root=project_root,
        ),
        "command": normalize_failure_text(packet.get("command"), project_root=project_root),
        "stack_location": normalize_failure_text(packet.get("stack_location"), project_root=project_root),
        "acceptance_criterion": normalize_failure_text(
            packet.get("acceptance_criterion"), project_root=project_root
        ),
    }
    if not any(fields.values()):
        raise ValidationError("failure packet has no signature-bearing evidence")
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), fields


def _normalize_acceptance(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValidationError("task acceptance_criteria must be a non-empty list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if isinstance(item, str):
            criterion = {"id": f"AC{index}", "text": item}
        elif isinstance(item, dict):
            criterion = dict(item)
        else:
            raise ValidationError("acceptance_criteria entries must be strings or objects")
        criterion_id = criterion.get("id", f"AC{index}")
        text = criterion.get("text", criterion.get("description"))
        if not isinstance(criterion_id, str) or not criterion_id or criterion_id in seen:
            raise ValidationError("acceptance criterion IDs must be unique non-empty strings")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError(f"acceptance criterion {criterion_id} has no text")
        status = str(criterion.get("status", "NOT_TESTED")).upper()
        if status not in AC_STATUSES:
            raise ValidationError(f"invalid acceptance criterion status: {status}")
        seen.add(criterion_id)
        result.append(
            {
                "id": criterion_id,
                "text": text.strip(),
                "status": status,
                "evidence": criterion.get("evidence"),
            }
        )
    return result


def _normalize_verification(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        required = [value]
        raw: dict[str, Any] = {}
    elif isinstance(value, list):
        required = value
        raw = {}
    elif isinstance(value, dict):
        raw = dict(value)
        required = raw.get("required", raw.get("commands", []))
    else:
        raise ValidationError("task verification must be a string, list, or object")
    if not isinstance(required, list) or not required or not all(
        isinstance(item, str) and item.strip() for item in required
    ):
        raise ValidationError("task verification.required must contain commands or methods")
    status = str(raw.get("status", "NOT_TESTED")).upper()
    if status not in AC_STATUSES:
        raise ValidationError(f"invalid verification status: {status}")
    return {
        "required": [item.strip() for item in required],
        "status": status,
        "evidence": raw.get("evidence", []),
        "updated_at": raw.get("updated_at"),
    }


def normalize_task(
    raw: dict[str, Any],
    *,
    now: str,
    enabled_workers: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("each Heavy Plan task must be an object")
    task_id = raw.get("id")
    title = raw.get("title")
    description = raw.get("description")
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task_id):
        raise ValidationError(f"invalid task id: {task_id!r}")
    if not isinstance(title, str) or not title.strip():
        raise ValidationError(f"task {task_id} has no title")
    if not isinstance(description, str) or not description.strip():
        raise ValidationError(f"task {task_id} has no description")
    status = raw.get("status", "planned")
    if status not in TASK_STATUSES:
        raise ValidationError(f"task {task_id} has invalid status: {status!r}")
    if status != "planned":
        raise ValidationError(
            f"Heavy Plan source task {task_id} must start planned; runtime owns task status"
        )
    dependencies = raw.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) and item for item in dependencies
    ):
        raise ValidationError(f"task {task_id} dependencies must be a string list")
    if len(dependencies) != len(set(dependencies)) or task_id in dependencies:
        raise ValidationError(f"task {task_id} has duplicate or self dependencies")
    owner = raw.get("owner", "executor_luna")
    if owner not in EXECUTOR_ROLES or owner not in enabled_workers:
        raise ValidationError(
            f"task {task_id} owner must be an enabled executor; "
            "Tester, Explorer, Root, and End-of-Session remain workflow gates outside the DAG"
        )
    scopes = raw.get("write_scope", [])
    if not isinstance(scopes, list):
        raise ValidationError(f"task {task_id} write_scope must be a list")
    normalized_scopes = [item.strip().replace("\\", "/") for item in scopes]
    for scope in normalized_scopes:
        _normalize_scope(scope)
    read_only = bool(raw.get("read_only", False))
    if owner in WRITE_ROLES and not read_only and not normalized_scopes:
        raise ValidationError(f"write-capable task {task_id} requires write_scope")
    attempts = raw.get("attempts", 0)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ValidationError(f"task {task_id} attempts must be a non-negative integer")
    if attempts != 0:
        raise ValidationError(f"Heavy Plan source task {task_id} must start with attempts=0")
    sol_escalations = raw.get("sol_escalations", 0)
    if (
        not isinstance(sol_escalations, int)
        or isinstance(sol_escalations, bool)
        or sol_escalations != 0
    ):
        raise ValidationError(
            f"Heavy Plan source task {task_id} must start with sol_escalations=0"
        )
    created_at = raw.get("created_at", now)
    updated_at = raw.get("updated_at", now)
    return {
        "id": task_id,
        "title": title.strip(),
        "description": description.strip(),
        "status": status,
        "dependencies": list(dependencies),
        "acceptance_criteria": _normalize_acceptance(raw.get("acceptance_criteria")),
        "verification": _normalize_verification(raw.get("verification")),
        "owner": owner,
        "planned_owner": owner,
        "write_scope": normalized_scopes,
        "read_only": read_only,
        "required": bool(raw.get("required", True)),
        "required_inputs_available": bool(raw.get("required_inputs_available", True)),
        "priority": _require_int(raw.get("priority", 5), f"task {task_id} priority", minimum=0),
        "attempts": attempts,
        "sol_escalations": 0,
        "failure_signature": None,
        "failure_history": [],
        "strategies": [],
        "stagnation": [],
        "blocker": None,
        "assigned_agent": None,
        "started_at": None,
        "result": None,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def validate_task_dag(tasks: list[dict[str, Any]]) -> None:
    index = {task["id"]: task for task in tasks}
    if len(index) != len(tasks):
        raise ValidationError("Heavy Plan contains duplicate task IDs")
    missing = sorted(
        {dependency for task in tasks for dependency in task["dependencies"] if dependency not in index}
    )
    if missing:
        raise ValidationError(f"Heavy Plan has missing dependencies: {missing}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise ValidationError(f"Heavy Plan dependency cycle includes {task_id}")
        visiting.add(task_id)
        for dependency in index[task_id]["dependencies"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in index:
        visit(task_id)


def _task_contract(task: dict[str, Any]) -> str:
    value = {
        "title": task["title"],
        "description": task["description"],
        "dependencies": task["dependencies"],
        "acceptance_criteria": [
            {"id": item["id"], "text": item["text"]}
            for item in task["acceptance_criteria"]
        ],
        "verification": task["verification"]["required"],
        "owner": task.get("planned_owner", task["owner"]),
        "write_scope": task["write_scope"],
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def route_request(assessment: dict[str, Any], manual_route: str | None = None) -> dict[str, Any]:
    """Resolve AUTO to an existing route without creating a fourth execution system."""

    if manual_route is not None:
        normalized = manual_route.strip().lower()
        if normalized not in {"light", "medium", "heavy"}:
            raise ValidationError("manual route must be light, medium, or heavy")
        return {
            "route": normalized,
            "manual_override": True,
            "profile": "manual",
            "reason": "explicit user route override",
        }
    metrics: dict[str, int] = {}
    for key in ("subtasks", "dependency_depth", "file_count", "parallelizable_tasks", "expected_iterations"):
        metrics[key] = _require_int(assessment.get(key, 0), key, minimum=0)
    risk = str(assessment.get("risk", "low")).lower()
    verification = str(assessment.get("verification_need", "low")).lower()
    ambiguity = str(assessment.get("ambiguity", "low")).lower()
    if risk not in {"low", "medium", "high", "critical"}:
        raise ValidationError("risk must be low, medium, high, or critical")
    if verification not in {"low", "medium", "high"}:
        raise ValidationError("verification_need must be low, medium, or high")
    if ambiguity not in {"low", "medium", "high"}:
        raise ValidationError("ambiguity must be low, medium, or high")
    cross_module_raw = assessment.get("cross_module", False)
    if not isinstance(cross_module_raw, bool):
        raise ValidationError("cross_module must be a boolean")
    cross_module = cross_module_raw
    if metrics["parallelizable_tasks"] > metrics["subtasks"]:
        raise ValidationError("parallelizable_tasks cannot exceed subtasks")
    if (
        metrics["subtasks"] <= 1
        and metrics["dependency_depth"] == 0
        and metrics["file_count"] <= 1
        and not cross_module
        and risk == "low"
        and verification == "low"
    ):
        return {"route": "light", "manual_override": False, "profile": "trivial", "reason": "single low-risk leaf task"}
    heavy = (
        metrics["parallelizable_tasks"] >= 2
        or metrics["dependency_depth"] >= 2
        or metrics["file_count"] >= 6
        or metrics["expected_iterations"] >= 3
        or cross_module
        or risk in {"high", "critical"}
        or ambiguity == "high"
        or verification == "high"
    )
    if heavy:
        profile = "bounded_verified" if metrics["subtasks"] <= 1 else "complex"
        return {"route": "heavy", "manual_override": False, "profile": profile, "reason": "worker isolation, dependencies, risk, or independent verification warrants Heavy"}
    if metrics["subtasks"] >= 2 or metrics["file_count"] >= 2 or metrics["expected_iterations"] >= 2:
        return {"route": "medium", "manual_override": False, "profile": "multi_step", "reason": "multi-step work without useful parallel execution"}
    return {"route": "light", "manual_override": False, "profile": "bounded", "reason": "bounded work is faster on the direct path"}


class OrchestrationEngine:
    """State-machine controller used by the Root agent between Codex turns."""

    def __init__(
        self,
        project_root: Path,
        workflow_config: WorkflowConfig,
        *,
        now: Callable[[], str] = utc_now,
    ):
        self.project_root = project_root.resolve()
        self.workflow_config = workflow_config
        self.store = OrchestrationStore(self.project_root)
        self._now = now

    def status(self) -> dict[str, Any]:
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            return self._summary(state, tasks, failures, config)

    def start_deployment(
        self,
        deployment_id: str,
        *,
        route: str = "heavy",
        git_head: str | None = None,
        new_deployment: bool = False,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", deployment_id):
            raise ValidationError("deployment_id must be lowercase and underscore-safe")
        if route not in {"heavy", "auto"}:
            raise ValidationError("persistent orchestration is used only by Heavy or AUTO→Heavy")
        timestamp = self._now()
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            current = state.get("deployment_id")
            if current == deployment_id:
                if state["phase"] != "INIT" and not new_deployment:
                    return self._summary(state, tasks, failures, config)
                if state["phase"] in {"DONE", "BLOCKED", "FAILED"} and new_deployment:
                    raise ValidationError("a new deployment requires a new deployment_id")
            if current and current != deployment_id:
                if not new_deployment or state["phase"] not in {"DONE", "BLOCKED", "FAILED"}:
                    raise ValidationError(
                        f"deployment {current!r} is still recorded; resume it or explicitly start a new deployment"
                    )
                if state["active_agents"]:
                    raise ValidationError(
                        "reconcile or release recorded agents before starting a new deployment"
                    )
                tasks["tasks"] = []
                tasks["plan_revision"] = 0
                tasks["plan_id"] = None
                previous_phase = state["phase"]
                state["phase"] = "INIT"
            else:
                previous_phase = None
            state.update(
                {
                    "deployment_id": deployment_id,
                    "route": route,
                    "macro_iteration": 0 if current != deployment_id else state["macro_iteration"],
                    "closure_ready": False,
                    "blocker": None,
                    "failure": None,
                    "completed_at": None,
                    "git_head": git_head,
                }
            )
            events: list[dict[str, Any]] = [
                {"event": "deployment_started", "route": route, "resumed": current == deployment_id}
            ]
            if previous_phase is not None:
                events.append(
                    {
                        "event": "deployment_state_reset",
                        "from": previous_phase,
                        "to": "INIT",
                    }
                )
            self._transition(state, "DISCOVER", events)
            self.store.commit(state, tasks, failures, events, timestamp=timestamp)
            return self._summary(state, tasks, failures, config)

    def import_heavy_plan(self, plan: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any]:
        timestamp = self._now()
        if isinstance(plan, list):
            raw_tasks = plan
            plan_id = None
            source = "heavy_plan"
        elif isinstance(plan, dict):
            candidate_tasks = plan.get("tasks")
            raw_tasks = candidate_tasks if isinstance(candidate_tasks, list) else []
            plan_id = plan.get("plan_id")
            source = plan.get("source", "heavy_plan")
        else:
            raise ValidationError("Heavy Plan payload must be an object or task list")
        if source != "heavy_plan" or not isinstance(raw_tasks, list) or not raw_tasks:
            raise ValidationError("Task DAG must be the machine-readable Heavy Plan")
        with self.store.locked():
            state, tasks_doc, failures, config = self.store.load()
            if state.get("deployment_id") is None:
                raise ValidationError("start a deployment before importing its Heavy Plan")
            if any(
                task.get("status") in {"running", "verifying"}
                for task in tasks_doc["tasks"]
            ):
                raise ValidationError(
                    "cannot replace the Heavy Plan while execution or verification is active"
                )
            events: list[dict[str, Any]] = []
            recovering_terminal = state["phase"] in {"BLOCKED", "FAILED"}
            if state["phase"] not in {"DISCOVER", "PLAN", "REPLAN", "BLOCKED", "FAILED"}:
                self._transition(state, "REPLAN", events)
            self._transition(state, "PLAN", events)
            enabled = set(self.workflow_config.enabled_workers)
            proposed = [normalize_task(item, now=timestamp, enabled_workers=enabled) for item in raw_tasks]
            validate_task_dag(proposed)
            previous = {task["id"]: task for task in tasks_doc["tasks"]}
            merged: list[dict[str, Any]] = []
            for task in proposed:
                old = previous.get(task["id"])
                if old and _task_contract(old) == _task_contract(task):
                    for key in (
                        "status",
                        "owner",
                        "acceptance_criteria",
                        "verification",
                        "attempts",
                        "sol_escalations",
                        "failure_signature",
                        "failure_history",
                        "strategies",
                        "stagnation",
                        "blocker",
                        "result",
                        "created_at",
                    ):
                        task[key] = old.get(key, task.get(key))
                merged.append(task)
            tasks_doc["tasks"] = merged
            tasks_doc["plan_revision"] = int(tasks_doc.get("plan_revision", 0)) + 1
            tasks_doc["plan_id"] = plan_id
            if recovering_terminal:
                state["blocker"] = None
                state["failure"] = None
            ready_events = self._refresh_readiness(state, tasks_doc, events)
            events.append(
                {
                    "event": "heavy_plan_imported",
                    "plan_id": plan_id,
                    "plan_revision": tasks_doc["plan_revision"],
                    "task_count": len(merged),
                    "ready_count": ready_events,
                }
            )
            self._set_phase_for_work(state, tasks_doc, events)
            self.store.commit(state, tasks_doc, failures, events, timestamp=timestamp)
            return self._summary(state, tasks_doc, failures, config)

    def begin_iteration(self) -> dict[str, Any]:
        timestamp = self._now()
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            events: list[dict[str, Any]] = []
            if not tasks["tasks"]:
                raise ValidationError("cannot iterate before the Heavy Plan is imported")
            if any(
                agent.get("role") in EXECUTOR_ROLES | {"tester"}
                for agent in state["active_agents"]
            ):
                raise ValidationError(
                    "cannot begin a new macro iteration while execution or verification is active"
                )
            state["macro_iteration"] += 1
            if state["macro_iteration"] > config.max_macro_iterations:
                state["failure"] = {
                    "type": "macro_iteration_budget_exhausted",
                    "limit": config.max_macro_iterations,
                }
                self._transition(state, "FAILED", events)
                events.append({"event": "macro_iteration_budget_exhausted"})
                self.store.commit(
                    state,
                    tasks,
                    failures,
                    events,
                    timestamp=timestamp,
                    snapshot_label="failed-budget",
                )
                return self._summary(state, tasks, failures, config)
            self._refresh_readiness(state, tasks, events)
            schedule = self._calculate_schedule(state, tasks, config)
            events.append(
                {
                    "event": "macro_iteration_started",
                    "iteration": state["macro_iteration"],
                    "dispatchable": [item["task_id"] for item in schedule["dispatchable"]],
                }
            )
            if schedule["dispatchable"]:
                self._transition(state, "DISPATCH", events)
            else:
                self._set_phase_for_work(state, tasks, events)
            self.store.commit(
                state,
                tasks,
                failures,
                events,
                timestamp=timestamp,
                snapshot_label="iteration",
            )
            result = self._summary(state, tasks, failures, config)
            result["schedule"] = schedule
            return result

    def schedule(self) -> dict[str, Any]:
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            result = self._summary(state, tasks, failures, config)
            result["schedule"] = self._calculate_schedule(state, tasks, config)
            return result

    def dispatch(self, task_id: str, *, agent_instance: str) -> dict[str, Any]:
        timestamp = self._now()
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            task = self._task(tasks, task_id)
            schedule = self._calculate_schedule(state, tasks, config)
            candidates = {item["task_id"] for item in schedule["dispatchable"]}
            if task_id not in candidates:
                raise ValidationError(
                    f"task {task_id} is not currently dispatchable; READY, capacity, or write scope failed"
                )
            if any(agent.get("instance_id") == agent_instance for agent in state["active_agents"]):
                raise ValidationError(f"agent instance already active: {agent_instance}")
            if task["owner"] == "executor_luna":
                if task["attempts"] >= config.max_luna_attempts_per_task:
                    raise ValidationError(f"task {task_id} exhausted its Luna attempt budget")
                task["attempts"] += 1
            task.update(
                {
                    "status": "running",
                    "assigned_agent": agent_instance,
                    "started_at": timestamp,
                    "updated_at": timestamp,
                    "blocker": None,
                }
            )
            state["active_agents"].append(
                {
                    "instance_id": agent_instance,
                    "role": task["owner"],
                    "task_id": task_id,
                    "write_scope": task["write_scope"],
                    "started_at": timestamp,
                }
            )
            events: list[dict[str, Any]] = []
            if state["phase"] not in {
                "DISPATCH",
                "READY",
                "EXECUTE",
                "VERIFY",
                "ESCALATE",
            }:
                raise ValidationError(f"cannot dispatch from phase {state['phase']}")
            self._transition(state, "EXECUTE", events)
            events.append(
                {
                    "event": "agent_spawned",
                    "agent": task["owner"],
                    "agent_instance": agent_instance,
                    "task": task_id,
                    "attempt": task["attempts"],
                }
            )
            self.store.commit(state, tasks, failures, events, timestamp=timestamp)
            return self._summary(state, tasks, failures, config)

    def register_auxiliary_agent(
        self,
        *,
        role: str,
        agent_instance: str,
        task_id: str | None = None,
        write_scope: list[str] | None = None,
    ) -> dict[str, Any]:
        timestamp = self._now()
        scopes = list(write_scope or [])
        for scope in scopes:
            _normalize_scope(scope)
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            if role not in self.workflow_config.enabled_workers:
                raise ValidationError(f"auxiliary agent role is not enabled: {role}")
            active = state["active_agents"]
            if any(agent.get("instance_id") == agent_instance for agent in active):
                raise ValidationError(f"agent instance already active: {agent_instance}")
            if len(active) >= self.workflow_config.max_concurrent_workers:
                raise ValidationError("platform child-worker capacity is exhausted")
            if role == "executor_luna" and sum(a.get("role") == role for a in active) >= config.max_luna_executors:
                raise ValidationError("Luna executor capacity is exhausted")
            if role == "executor_sol" and sum(a.get("role") == role for a in active) >= self.workflow_config.max_executor_sol_instances:
                raise ValidationError("Sol executor capacity is exhausted")
            for agent in active:
                if write_scopes_conflict(scopes, list(agent.get("write_scope", []))):
                    raise ValidationError(
                        f"write scope conflicts with active agent {agent.get('instance_id')}"
                    )
            active.append(
                {
                    "instance_id": agent_instance,
                    "role": role,
                    "task_id": task_id,
                    "write_scope": scopes,
                    "started_at": timestamp,
                }
            )
            events = [
                {
                    "event": "agent_registered",
                    "agent": role,
                    "agent_instance": agent_instance,
                    "task": task_id,
                }
            ]
            self.store.commit(state, tasks, failures, events, timestamp=timestamp)
            return self._summary(state, tasks, failures, config)

    def release_agent(self, agent_instance: str, *, reason: str = "completed") -> dict[str, Any]:
        timestamp = self._now()
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            removed = self._release_agents(state, instance_id=agent_instance)
            if not removed:
                raise ValidationError(f"agent instance is not active: {agent_instance}")
            events = [
                {
                    "event": "agent_released",
                    "agent_instance": agent_instance,
                    "reason": reason,
                }
            ]
            self.store.commit(state, tasks, failures, events, timestamp=timestamp)
            return self._summary(state, tasks, failures, config)

    def record_execution_result(self, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict) or not result:
            raise ValidationError("execution result must be a non-empty object")
        timestamp = self._now()
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            task = self._task(tasks, task_id)
            if task["status"] != "running" or task["owner"] not in EXECUTOR_ROLES:
                raise ValidationError(f"task {task_id} is not running under an executor")
            self._release_agents(state, task_id=task_id, roles=EXECUTOR_ROLES)
            task.update(
                {
                    "status": "verifying",
                    "assigned_agent": None,
                    "started_at": None,
                    "result": {**(task.get("result") or {}), "execution": result},
                    "updated_at": timestamp,
                }
            )
            events: list[dict[str, Any]] = []
            self._transition(state, "VERIFY", events)
            events.append({"event": "execution_completed", "task": task_id})
            self.store.commit(state, tasks, failures, events, timestamp=timestamp)
            return self._summary(state, tasks, failures, config)

    def record_verification(self, task_id: str, report: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(report, dict):
            raise ValidationError("verification report must be an object")
        timestamp = self._now()
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            task = self._task(tasks, task_id)
            if task["status"] != "verifying":
                raise ValidationError(f"task {task_id} is not awaiting verification")
            if not any(
                agent.get("role") == "tester" and agent.get("task_id") == task_id
                for agent in state["active_agents"]
            ):
                raise ValidationError(
                    f"task {task_id} requires a recorded independent tester before verification"
                )
            verdict = str(report.get("status", "NOT_TESTED")).upper()
            if verdict not in AC_STATUSES:
                raise ValidationError(f"invalid verification verdict: {verdict}")
            criteria_report = report.get("criteria", {})
            if not isinstance(criteria_report, dict):
                raise ValidationError("verification criteria must be keyed by acceptance ID")
            for criterion in task["acceptance_criteria"]:
                evidence = criteria_report.get(criterion["id"])
                if isinstance(evidence, str):
                    criterion_status = evidence.upper()
                    criterion_evidence = None
                elif isinstance(evidence, dict):
                    criterion_status = str(evidence.get("status", "NOT_TESTED")).upper()
                    criterion_evidence = evidence.get("evidence")
                else:
                    criterion_status = "NOT_TESTED"
                    criterion_evidence = None
                if criterion_status not in AC_STATUSES:
                    raise ValidationError(
                        f"invalid status for criterion {criterion['id']}: {criterion_status}"
                    )
                criterion["status"] = criterion_status
                criterion["evidence"] = criterion_evidence
            task["verification"].update(
                {
                    "status": verdict,
                    "evidence": report.get("evidence", []),
                    "updated_at": timestamp,
                }
            )
            self._release_agents(state, task_id=task_id, roles={"tester"})
            all_pass = verdict == "PASS" and all(
                criterion["status"] == "PASS" for criterion in task["acceptance_criteria"]
            )
            events: list[dict[str, Any]] = []
            if all_pass:
                task["status"] = "done"
                task["updated_at"] = timestamp
                task["result"] = {
                    **(task.get("result") or {}),
                    "verification": report,
                }
                events.append({"event": "task_done", "task": task_id})
                self._transition(state, "REPLAN", events)
                self._refresh_readiness(state, tasks, events)
                self._set_phase_for_work(state, tasks, events)
                action = {"action": "done", "root_attention": False}
            else:
                packet = report.get("failure")
                if not isinstance(packet, dict):
                    packet = {
                        "failed_test": report.get("failed_test", "verification"),
                        "exception_type": "VerificationFailure" if verdict == "FAIL" else "VerificationIncomplete",
                        "message": report.get("message", f"verification verdict {verdict}"),
                        "command": report.get("command", "verification gate"),
                        "acceptance_criterion": next(
                            (
                                criterion["id"]
                                for criterion in task["acceptance_criteria"]
                                if criterion["status"] != "PASS"
                            ),
                            "verification",
                        ),
                        "classification": report.get("classification", "routine"),
                        "acceptance_progress": report.get("acceptance_progress", False),
                    }
                action = self._record_failure_docs(
                    state,
                    tasks,
                    failures,
                    task,
                    packet,
                    config,
                    events,
                    timestamp,
                )
            self.store.commit(state, tasks, failures, events, timestamp=timestamp)
            result = self._summary(state, tasks, failures, config)
            result["decision"] = action
            return result

    def record_failure(self, task_id: str, packet: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(packet, dict):
            raise ValidationError("failure packet must be an object")
        timestamp = self._now()
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            task = self._task(tasks, task_id)
            if task["status"] not in {"running", "verifying"}:
                raise ValidationError(f"task {task_id} is not executing or verifying")
            events: list[dict[str, Any]] = []
            action = self._record_failure_docs(
                state,
                tasks,
                failures,
                task,
                packet,
                config,
                events,
                timestamp,
            )
            self.store.commit(state, tasks, failures, events, timestamp=timestamp)
            result = self._summary(state, tasks, failures, config)
            result["decision"] = action
            return result

    def record_specialist_result(self, task_id: str, report: dict[str, Any]) -> dict[str, Any]:
        required = {
            "ROOT_CAUSE",
            "EVIDENCE",
            "RECOMMENDED_FIX",
            "ALTERNATIVES",
            "RISKS",
            "VERIFICATION_PLAN",
            "CONFIDENCE",
        }
        missing = sorted(required - set(report)) if isinstance(report, dict) else sorted(required)
        if missing:
            raise ValidationError(f"specialist report is missing: {missing}")
        timestamp = self._now()
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            task = self._task(tasks, task_id)
            if task["owner"] != "executor_sol" or task["status"] != "running":
                raise ValidationError(f"task {task_id} is not running under executor_sol")
            self._release_agents(state, task_id=task_id, roles={"executor_sol"})
            requested_owner = report.get("IMPLEMENTATION_OWNER", "executor_luna")
            if requested_owner not in {"executor_luna", "executor_sol"}:
                raise ValidationError("IMPLEMENTATION_OWNER must be executor_luna or executor_sol")
            if (
                requested_owner == "executor_luna"
                and task["attempts"] >= config.max_luna_attempts_per_task
            ):
                requested_owner = "executor_sol"
            task.update(
                {
                    "owner": requested_owner,
                    "status": "ready",
                    "assigned_agent": None,
                    "started_at": None,
                    "blocker": None,
                    "result": {
                        **(task.get("result") or {}),
                        "specialist": report,
                    },
                    "updated_at": timestamp,
                }
            )
            events: list[dict[str, Any]] = [
                {
                    "event": "specialist_result_recorded",
                    "task": task_id,
                    "implementation_owner": requested_owner,
                }
            ]
            self._transition(state, "REPLAN", events)
            self._transition(state, "READY", events)
            self.store.commit(state, tasks, failures, events, timestamp=timestamp)
            return self._summary(state, tasks, failures, config)

    def reconcile(self, reality: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(reality, dict):
            raise ValidationError("reality payload must be an object")
        timestamp = self._now()
        now_value = _parse_timestamp(timestamp) or datetime.now(UTC)
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            events: list[dict[str, Any]] = []
            active_ids_raw = reality.get("active_agent_ids")
            if active_ids_raw is not None and (
                not isinstance(active_ids_raw, list)
                or not all(isinstance(item, str) for item in active_ids_raw)
            ):
                raise ValidationError("active_agent_ids must be a string list")
            active_ids = set(active_ids_raw) if active_ids_raw is not None else None
            retained_agents: list[dict[str, Any]] = []
            for agent in state["active_agents"]:
                started = _parse_timestamp(agent.get("started_at"))
                stale = (
                    started is None
                    or (now_value - started).total_seconds() >= config.stale_running_seconds
                )
                missing = active_ids is not None and agent.get("instance_id") not in active_ids
                if missing or (active_ids is None and stale):
                    task_id = agent.get("task_id")
                    task = next((item for item in tasks["tasks"] if item["id"] == task_id), None)
                    if task is not None and task["status"] == "running":
                        task.update(
                            {
                                "status": "planned",
                                "assigned_agent": None,
                                "started_at": None,
                                "updated_at": timestamp,
                            }
                        )
                        events.append(
                            {
                                "event": "stale_task_reopened",
                                "task": task_id,
                                "agent_instance": agent.get("instance_id"),
                            }
                        )
                else:
                    retained_agents.append(agent)
            state["active_agents"] = retained_agents
            verification = reality.get("verification", {})
            if not isinstance(verification, dict):
                raise ValidationError("reality.verification must be an object")
            required_inputs = reality.get("required_inputs", {})
            if not isinstance(required_inputs, dict):
                raise ValidationError("reality.required_inputs must be an object")
            resolved_blockers = reality.get("resolved_blockers", [])
            if not isinstance(resolved_blockers, list) or not all(
                isinstance(item, str) for item in resolved_blockers
            ):
                raise ValidationError("reality.resolved_blockers must be a string list")
            resolved_blocker_ids = set(resolved_blockers)
            if reality.get("deployment_blocker_resolved") is True:
                state["blocker"] = None
                events.append({"event": "deployment_blocker_resolved"})
            for task in tasks["tasks"]:
                if task["id"] in resolved_blocker_ids and task["status"] == "blocked":
                    task["status"] = "planned"
                    task["blocker"] = None
                    task["updated_at"] = timestamp
                    events.append({"event": "task_blocker_resolved", "task": task["id"]})
                if task["id"] in required_inputs:
                    task["required_inputs_available"] = bool(required_inputs[task["id"]])
                objective = verification.get(task["id"])
                internally_valid = self._task_objectively_done(task)
                if task["status"] == "done" and (
                    (objective is not None and str(objective).upper() != "PASS")
                    or not internally_valid
                ):
                    verdict = str(objective or "NOT_TESTED").upper()
                    if verdict not in AC_STATUSES:
                        verdict = "FAIL"
                    task["status"] = "planned"
                    task["verification"]["status"] = verdict
                    task["updated_at"] = timestamp
                    events.append(
                        {
                            "event": "reality_reopened_task",
                            "task": task["id"],
                            "objective_verdict": verdict,
                        }
                    )
            if "git_head" in reality and reality["git_head"] != state.get("git_head"):
                events.append(
                    {
                        "event": "git_reconciled",
                        "from": state.get("git_head"),
                        "to": reality["git_head"],
                    }
                )
                state["git_head"] = reality["git_head"]
            self._transition(state, "REPLAN", events)
            self._refresh_readiness(state, tasks, events)
            self._set_phase_for_work(state, tasks, events)
            events.append({"event": "reality_reconciled"})
            self.store.commit(
                state,
                tasks,
                failures,
                events,
                timestamp=timestamp,
                snapshot_label="reconcile",
            )
            return self._summary(state, tasks, failures, config)

    def close(
        self,
        *,
        closure_state: str,
        end_of_session_status: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        timestamp = self._now()
        with self.store.locked():
            state, tasks, failures, config = self.store.load()
            events: list[dict[str, Any]] = []
            normalized = closure_state.lower()
            eos_active = any(
                agent.get("role") == "end_of_session"
                for agent in state["active_agents"]
            )
            if not eos_active:
                raise ValidationError(
                    "record the End-of-Session worker before deployment closure"
                )
            eos_verdict = str(end_of_session_status).upper()
            if normalized == "complete":
                if not state.get("closure_ready"):
                    raise ValidationError("DONE conditions are not satisfied")
                if state.get("blocker") is not None or state.get("failure") is not None:
                    raise ValidationError("unresolved deployment blocker or failure prevents DONE")
                if eos_verdict != "PASS":
                    raise ValidationError("End-of-Session closure must PASS before DONE")
                self._release_agents(state, roles={"end_of_session"})
                if state["active_agents"]:
                    raise ValidationError("cannot close while child agents remain active")
                self._transition(state, "DONE", events)
                state["completed_at"] = timestamp
                events.append({"event": "deployment_done"})
            elif normalized == "blocked":
                if eos_verdict not in {"PASS", "BLOCKED"}:
                    raise ValidationError(
                        "End-of-Session must report PASS or BLOCKED before blocked closure"
                    )
                self._release_agents(state, roles={"end_of_session"})
                state["blocker"] = {"reason": reason or "unspecified blocker"}
                self._transition(state, "BLOCKED", events)
                events.append({"event": "deployment_blocked", "reason": reason})
            elif normalized == "failed":
                if eos_verdict not in {"PASS", "BLOCKED"}:
                    raise ValidationError(
                        "End-of-Session must report PASS or BLOCKED before failed closure"
                    )
                self._release_agents(state, roles={"end_of_session"})
                state["failure"] = {"reason": reason or "bounded execution failed"}
                self._transition(state, "FAILED", events)
                events.append({"event": "deployment_failed", "reason": reason})
            else:
                raise ValidationError("closure_state must be complete, blocked, or failed")
            self.store.commit(
                state,
                tasks,
                failures,
                events,
                timestamp=timestamp,
                snapshot_label=normalized,
            )
            return self._summary(state, tasks, failures, config)

    def _record_failure_docs(
        self,
        state: dict[str, Any],
        tasks: dict[str, Any],
        failures: dict[str, Any],
        task: dict[str, Any],
        packet: dict[str, Any],
        config: OrchestrationConfig,
        events: list[dict[str, Any]],
        timestamp: str,
    ) -> dict[str, Any]:
        signature, normalized = failure_signature(packet, project_root=self.project_root)
        self._release_agents(state, task_id=task["id"])
        memory = next(
            (item for item in failures["failures"] if item.get("signature") == signature),
            None,
        )
        if memory is None:
            memory = {
                "signature": signature,
                "normalized": normalized,
                "count": 0,
                "task_ids": [],
                "first_seen": timestamp,
                "last_seen": timestamp,
                "classifications": [],
            }
            failures["failures"].append(memory)
        memory["count"] += 1
        memory["last_seen"] = timestamp
        if task["id"] not in memory["task_ids"]:
            memory["task_ids"].append(task["id"])
        classification = str(packet.get("classification", "routine")).lower()
        if classification not in memory["classifications"]:
            memory["classifications"].append(classification)
        task["failure_signature"] = signature
        task["failure_history"].append(signature)
        strategy = normalize_failure_text(packet.get("strategy"), project_root=self.project_root)
        if strategy:
            task["strategies"].append(strategy)
        stagnation = self._stagnation_reasons(task, packet, config)
        task["stagnation"] = stagnation
        task["result"] = {
            **(task.get("result") or {}),
            "last_failure": {
                "signature": signature,
                "classification": classification,
                "normalized": normalized,
                "timestamp": timestamp,
            },
        }
        task.update(
            {
                "assigned_agent": None,
                "started_at": None,
                "updated_at": timestamp,
            }
        )
        consecutive = 0
        for previous in reversed(task["failure_history"]):
            if previous != signature:
                break
            consecutive += 1
        events.append(
            {
                "event": "verification_failed" if task["status"] == "verifying" else "execution_failed",
                "task": task["id"],
                "signature": signature,
                "classification": classification,
                "consecutive": consecutive,
            }
        )
        self._transition(state, "CLASSIFY", events)
        if classification in BLOCKER_FAILURE_CLASSES:
            task["status"] = "blocked"
            task["blocker"] = {
                "classification": classification,
                "signature": signature,
                "reason": packet.get("message"),
            }
            self._set_phase_for_work(state, tasks, events)
            events.append({"event": "task_blocked", "task": task["id"]})
            return {"action": "blocked", "root_attention": True, "signature": signature}
        hard = (
            classification in HARD_FAILURE_CLASSES
            or consecutive >= config.same_failure_limit
            or task["attempts"] >= config.max_luna_attempts_per_task
            or bool(stagnation)
        )
        if hard and task["sol_escalations"] < config.max_sol_escalations_per_task:
            if self.workflow_config.max_executor_sol_instances <= 0:
                task["status"] = "blocked"
                task["blocker"] = {"reason": "executor_sol is disabled", "signature": signature}
                self._transition(state, "BLOCKED", events)
                return {"action": "blocked", "root_attention": True, "signature": signature}
            task["sol_escalations"] += 1
            task["owner"] = "executor_sol"
            task["status"] = "ready"
            task["blocker"] = None
            self._transition(state, "ESCALATE", events)
            events.append(
                {
                    "event": "escalated",
                    "agent": "executor_sol",
                    "task": task["id"],
                    "signature": signature,
                    "sol_escalation": task["sol_escalations"],
                    "stagnation": stagnation,
                }
            )
            return {"action": "escalate_sol", "root_attention": True, "signature": signature}
        if not hard and task["attempts"] < config.max_luna_attempts_per_task:
            task["owner"] = "executor_luna"
            task["status"] = "ready"
            task["blocker"] = None
            self._transition(state, "READY", events)
            events.append(
                {
                    "event": "task_retry",
                    "task": task["id"],
                    "next_attempt": task["attempts"] + 1,
                    "signature": signature,
                    "routine_repair": True,
                }
            )
            return {"action": "routine_repair", "root_attention": False, "signature": signature}
        task["status"] = "failed"
        task["blocker"] = None
        self._transition(state, "FAILED", events)
        events.append({"event": "task_failed", "task": task["id"], "signature": signature})
        return {"action": "failed", "root_attention": True, "signature": signature}

    @staticmethod
    def _stagnation_reasons(
        task: dict[str, Any], packet: dict[str, Any], config: OrchestrationConfig
    ) -> list[str]:
        reasons: list[str] = []
        history = task["failure_history"]
        if len(history) >= config.same_failure_limit and len(set(history[-config.same_failure_limit :])) == 1:
            reasons.append("same_failure_repeated")
        changed = set(packet.get("changed_files", []) or [])
        reverted = set(packet.get("reverted_files", []) or [])
        if changed and changed == reverted:
            reasons.append("same_files_changed_and_reverted")
        if packet.get("acceptance_progress") is False and task["attempts"] >= 2:
            reasons.append("no_acceptance_criteria_progress")
        strategies = task["strategies"]
        if len(strategies) >= 2 and strategies[-1] == strategies[-2]:
            reasons.append("same_strategy_repeated")
        if (
            packet.get("worker_status") == "BLOCKED"
            and not packet.get("new_evidence", False)
            and len(task["failure_history"]) >= 2
        ):
            reasons.append("blocked_without_new_evidence")
        return reasons

    def _calculate_schedule(
        self,
        state: dict[str, Any],
        tasks: dict[str, Any],
        config: OrchestrationConfig,
    ) -> dict[str, Any]:
        active = list(state["active_agents"])
        reserve = (
            1
            if config.reserve_end_of_session_slot
            and not state.get("closure_ready")
            and not any(agent.get("role") == "end_of_session" for agent in active)
            else 0
        )
        platform_available = max(
            0,
            self.workflow_config.max_concurrent_workers - len(active) - reserve,
        )
        luna_active = sum(agent.get("role") == "executor_luna" for agent in active)
        sol_active = sum(agent.get("role") == "executor_sol" for agent in active)
        luna_available = max(0, config.max_luna_executors - luna_active)
        sol_available = max(
            0,
            self.workflow_config.max_executor_sol_instances - sol_active,
        )
        active_scopes = [list(agent.get("write_scope", [])) for agent in active]
        selected_scopes: list[list[str]] = []
        dispatchable: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        ready = sorted(
            (task for task in tasks["tasks"] if task["status"] == "ready"),
            key=lambda task: (-task["priority"], task["id"]),
        )
        for task in ready:
            scopes = task["write_scope"]
            conflict = any(write_scopes_conflict(scopes, other) for other in active_scopes + selected_scopes)
            if conflict:
                conflicts.append({"task_id": task["id"], "reason": "write_scope_conflict"})
                continue
            role = task["owner"]
            allowed = platform_available > 0
            if role == "executor_luna":
                allowed = allowed and luna_available > 0
            elif role == "executor_sol":
                allowed = allowed and sol_available > 0
            if not allowed:
                continue
            dispatchable.append(
                {
                    "task_id": task["id"],
                    "owner": role,
                    "write_scope": scopes,
                    "attempts": task["attempts"],
                }
            )
            selected_scopes.append(scopes)
            platform_available -= 1
            if role == "executor_luna":
                luna_available -= 1
            elif role == "executor_sol":
                sol_available -= 1
        ready_luna = [task for task in ready if task["owner"] == "executor_luna"]
        selected_luna = [task for task in dispatchable if task["owner"] == "executor_luna"]
        return {
            "dispatchable": dispatchable,
            "write_scope_conflicts": conflicts,
            "independent_ready_luna_tasks": len(ready_luna) - sum(
                conflict["task_id"] in {task["id"] for task in ready_luna}
                for conflict in conflicts
            ),
            "active_luna": luna_active,
            "allocated_luna": len(selected_luna),
            "max_luna_executors": config.max_luna_executors,
            "active_sol": sol_active,
            "max_sol_executors": self.workflow_config.max_executor_sol_instances,
            "platform_child_capacity": self.workflow_config.max_concurrent_workers,
            "reserved_end_of_session_slots": reserve,
        }

    def _refresh_readiness(
        self,
        state: dict[str, Any],
        tasks: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> int:
        index = {task["id"]: task for task in tasks["tasks"]}
        ready_count = 0
        for task in tasks["tasks"]:
            if task["status"] not in {"planned", "ready", "blocked"}:
                continue
            dependencies = [index[item] for item in task["dependencies"]]
            failed_dependencies = [
                item["id"] for item in dependencies if item["status"] in {"failed", "blocked"}
            ]
            dependency_blocker = isinstance(task.get("blocker"), dict) and task["blocker"].get("type") == "dependency"
            if failed_dependencies:
                if task["status"] != "blocked" or not dependency_blocker:
                    task["status"] = "blocked"
                    task["blocker"] = {"type": "dependency", "tasks": failed_dependencies}
                    events.append(
                        {"event": "task_blocked", "task": task["id"], "dependencies": failed_dependencies}
                    )
                continue
            if task["status"] == "blocked" and not dependency_blocker:
                continue
            dependencies_done = all(item["status"] == "done" for item in dependencies)
            if dependencies_done and task["required_inputs_available"]:
                if task["status"] != "ready":
                    task["status"] = "ready"
                    task["blocker"] = None
                    events.append({"event": "task_ready", "task": task["id"]})
                ready_count += 1
            else:
                task["status"] = "planned"
                if dependency_blocker:
                    task["blocker"] = None
        state["closure_ready"] = (
            bool(tasks["tasks"])
            and state.get("blocker") is None
            and state.get("failure") is None
            and all(
                (not task["required"]) or self._task_objectively_done(task)
                for task in tasks["tasks"]
            )
        )
        return ready_count

    def _set_phase_for_work(
        self,
        state: dict[str, Any],
        tasks: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> None:
        statuses = {task["status"] for task in tasks["tasks"]}
        if state.get("closure_ready"):
            target = "REPLAN"
        elif "running" in statuses:
            target = "EXECUTE"
        elif "verifying" in statuses:
            target = "VERIFY"
        elif "ready" in statuses:
            target = "READY"
        elif "planned" in statuses:
            target = "PLAN"
        elif "blocked" in statuses:
            target = "BLOCKED"
        elif "failed" in statuses:
            target = "FAILED"
        else:
            target = "PLAN"
        if state["phase"] != target:
            if target not in _ALLOWED_TRANSITIONS.get(state["phase"], set()):  # noqa: SIM102
                # Reconciliation is the explicit bridge when derived reality
                # cannot be reached directly from a stale stored phase.
                if state["phase"] != "REPLAN":
                    self._transition(state, "REPLAN", events)
            if state["phase"] != target:
                self._transition(state, target, events)

    @staticmethod
    def _task_objectively_done(task: dict[str, Any]) -> bool:
        return (
            task["status"] == "done"
            and task["verification"]["status"] == "PASS"
            and all(item["status"] == "PASS" for item in task["acceptance_criteria"])
            and task.get("blocker") is None
        )

    @staticmethod
    def _task(tasks: dict[str, Any], task_id: str) -> dict[str, Any]:
        task = next((item for item in tasks["tasks"] if item["id"] == task_id), None)
        if task is None:
            raise ValidationError(f"unknown task: {task_id}")
        return task

    @staticmethod
    def _release_agents(
        state: dict[str, Any],
        *,
        instance_id: str | None = None,
        task_id: str | None = None,
        roles: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        removed: list[dict[str, Any]] = []
        retained: list[dict[str, Any]] = []
        for agent in state["active_agents"]:
            matches = True
            if instance_id is not None:
                matches = matches and agent.get("instance_id") == instance_id
            if task_id is not None:
                matches = matches and agent.get("task_id") == task_id
            if roles is not None:
                matches = matches and agent.get("role") in roles
            if matches:
                removed.append(agent)
            else:
                retained.append(agent)
        state["active_agents"] = retained
        return removed

    @staticmethod
    def _transition(
        state: dict[str, Any], target: str, events: list[dict[str, Any]]
    ) -> None:
        current = state["phase"]
        if target == current:
            return
        if target not in PHASES or target not in _ALLOWED_TRANSITIONS.get(current, set()):
            raise ValidationError(f"invalid state transition: {current} -> {target}")
        state["phase"] = target
        events.append({"event": "state_transition", "from": current, "to": target})

    def _summary(
        self,
        state: dict[str, Any],
        tasks: dict[str, Any],
        failures: dict[str, Any],
        config: OrchestrationConfig,
    ) -> dict[str, Any]:
        counts = {status: 0 for status in sorted(TASK_STATUSES)}
        for task in tasks["tasks"]:
            counts[task["status"]] += 1
        return {
            "deployment_id": state.get("deployment_id"),
            "route": state.get("route"),
            "phase": state["phase"],
            "macro_iteration": state["macro_iteration"],
            "closure_ready": state["closure_ready"],
            "task_counts": counts,
            "active_agents": state["active_agents"],
            "failure_signatures": len(failures["failures"]),
            "limits": {
                **config.to_mapping(),
                "max_sol_executors": self.workflow_config.max_executor_sol_instances,
                "platform_child_capacity": self.workflow_config.max_concurrent_workers,
            },
        }
