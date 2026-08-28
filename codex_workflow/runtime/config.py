"""Workflow configuration validation and materialization."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ._toml import tomllib
from .errors import ValidationError
from .markers import EFFECTIVE_CONFIG, replace
from .migrations import migrate_config_resource


DEFAULT_EXECUTORS = {"executor_luna", "executor_terra"}
REASONING_EFFORTS = {"high", "xhigh", "max"}
CONFIG_SCHEMA_VERSION = 4
REQUIRED_WORKERS = {"doc-writer", "end_of_session"}
PLATFORM_MAX_WORKERS = 20


@dataclass(frozen=True)
class WorkflowConfig:
    schema_version: int
    default_executor: str
    default_executor_reasoning_effort: str
    auto_check_update: bool
    max_concurrent_workers: int
    max_executor_sol_instances: int
    report_package_size: int
    enabled_workers: tuple[str, ...]

    @classmethod
    def from_mapping(
        cls, raw: dict[str, Any], *, available_workers: set[str] | None = None
    ) -> "WorkflowConfig":
        expected = {
            "schema_version",
            "default_executor",
            "default_executor_reasoning_effort",
            "auto_check_update",
            "max_concurrent_workers",
            "max_executor_sol_instances",
            "report_package_size",
            "enabled_workers",
        }
        unknown = set(raw) - expected
        missing = expected - set(raw)
        if unknown or missing:
            raise ValidationError(
                f"invalid workflow config keys; missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        if raw["schema_version"] != CONFIG_SCHEMA_VERSION:
            raise ValidationError("unsupported workflow_config schema_version")
        workers_raw = raw["enabled_workers"]
        if not isinstance(workers_raw, list) or not all(
            isinstance(item, str) and item for item in workers_raw
        ):
            raise ValidationError("enabled_workers must be a list of names")
        workers = tuple(workers_raw)
        if len(workers) != len(set(workers)):
            raise ValidationError("enabled_workers contains duplicates")
        default = raw["default_executor"]
        if default not in DEFAULT_EXECUTORS or default not in workers:
            raise ValidationError("default_executor must be enabled luna or terra")
        enabled_defaults = DEFAULT_EXECUTORS.intersection(workers)
        if enabled_defaults != {default}:
            raise ValidationError("exactly the selected default executor must be enabled")
        missing_required = REQUIRED_WORKERS - set(workers)
        if missing_required:
            raise ValidationError(
                f"required workers must remain enabled: {sorted(missing_required)}"
            )
        effort = raw["default_executor_reasoning_effort"]
        if effort not in REASONING_EFFORTS:
            raise ValidationError("invalid default_executor_reasoning_effort")
        auto_check = raw["auto_check_update"]
        if not isinstance(auto_check, bool):
            raise ValidationError("auto_check_update must be a boolean")
        maximum = _positive_int(raw["max_concurrent_workers"], "max_concurrent_workers")
        if maximum > PLATFORM_MAX_WORKERS:
            raise ValidationError(
                f"max_concurrent_workers exceeds platform limit {PLATFORM_MAX_WORKERS}"
            )
        sol_maximum = raw["max_executor_sol_instances"]
        if not isinstance(sol_maximum, int) or isinstance(sol_maximum, bool):
            raise ValidationError("max_executor_sol_instances must be an integer")
        if sol_maximum < 0 or sol_maximum > maximum:
            raise ValidationError("max_executor_sol_instances is outside worker limit")
        if "executor_sol" not in workers and sol_maximum != 0:
            raise ValidationError(
                "max_executor_sol_instances must be zero when executor_sol is disabled"
            )
        report_size = _positive_int(raw["report_package_size"], "report_package_size")
        if available_workers is not None:
            unavailable = set(workers) - available_workers
            if unavailable:
                raise ValidationError(f"missing worker templates: {sorted(unavailable)}")
        return cls(
            CONFIG_SCHEMA_VERSION,
            default,
            effort,
            auto_check,
            maximum,
            sol_maximum,
            report_size,
            workers,
        )

    def to_mapping(self) -> dict[str, Any]:
        value = asdict(self)
        value["enabled_workers"] = list(self.enabled_workers)
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_mapping(), indent=2) + "\n"


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def load_config(path: Path, *, templates: Path | None = None) -> WorkflowConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read workflow config {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ValidationError("workflow config root must be an object")
    available = None
    if templates is not None:
        available = {path.stem for path in templates.glob("*.toml") if path.is_file()}
    return WorkflowConfig.from_mapping(raw, available_workers=available)


def load_migrated_config(
    path: Path, *, defaults: Path, templates: Path
) -> WorkflowConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        default_raw = json.loads(defaults.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot load configuration migration inputs: {error}") from error
    if not isinstance(raw, dict) or not isinstance(default_raw, dict):
        raise ValidationError("configuration migration inputs must be JSON objects")
    migrated = migrate_config_resource(raw, default_raw)
    available = {item.stem for item in templates.glob("*.toml") if item.is_file()}
    return WorkflowConfig.from_mapping(migrated, available_workers=available)


def effective_config_body(config: WorkflowConfig) -> str:
    workers = ", ".join(f"`{name}`" for name in config.enabled_workers)
    return "\n".join(
        [
            "## Effective Workflow Configuration",
            "",
            f"- Default executor: `{config.default_executor}` "
            f"(`{config.default_executor_reasoning_effort}` reasoning effort).",
            f"- Enabled workers: {workers}.",
            f"- Maximum concurrent child workers: `{config.max_concurrent_workers}`.",
            f"- Maximum `executor_sol` workers: `{config.max_executor_sol_instances}`.",
            f"- Maximum worker final-report package: `{config.report_package_size}` words.",
            "",
            "Create only enabled workers and obey these limits.",
        ]
    )


def render_heavy_route(text: str, config: WorkflowConfig) -> str:
    return replace(text, EFFECTIVE_CONFIG, effective_config_body(config))


_EFFORT_LINE = re.compile(
    r'(?m)^model_reasoning_effort\s*=\s*"[^"]+"\s*$'
)


def render_worker_template(text: str, *, worker: str, config: WorkflowConfig) -> str:
    marker = f"# codex-workflow-worker: {worker}"
    if not text.startswith(marker + "\n"):
        raise ValidationError(f"worker template ownership marker mismatch: {worker}")
    if worker == config.default_executor:
        matches = list(_EFFORT_LINE.finditer(text))
        if len(matches) != 1:
            raise ValidationError(f"expected one reasoning effort field in {worker}")
        text = _EFFORT_LINE.sub(
            f'model_reasoning_effort = "{config.default_executor_reasoning_effort}"',
            text,
        )
    tomllib.loads(text)
    return text


def patch_codex_config(text: str, config: WorkflowConfig) -> str:
    if text.strip():
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise ValidationError(f"existing Codex config is invalid TOML: {error}") from error
    # Codex 0.149 exposes child-thread capacity under [agents].  Earlier
    # codex_workflow releases wrote experimental multi_agent_v2 keys.  Remove
    # only those workflow-owned legacy keys before materializing the supported
    # surface; unrelated user settings in either section are retained.
    lines = _remove_owned_keys(text.splitlines(), {
        "features.multi_agent_v2": _WORKFLOW_OWNED_KEYS["features.multi_agent_v2"]
    })
    sections: dict[str, dict[str, str]] = {
        "agents": {
            "enabled": "true",
            "max_concurrent_threads_per_session": str(config.max_concurrent_workers),
        },
    }
    for section, values in sections.items():
        lines = _patch_section(lines, section, values)
    rendered = "\n".join(lines).rstrip() + "\n"
    try:
        tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as error:
        raise ValidationError(f"generated Codex config is invalid TOML: {error}") from error
    return rendered


_SECTION = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
_KEY = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def _patch_section(lines: list[str], section: str, values: dict[str, str]) -> list[str]:
    headers = [index for index, line in enumerate(lines) if (_SECTION.match(line) and _SECTION.match(line).group(1) == section)]
    if len(headers) > 1:
        raise ValidationError(f"duplicate TOML section [{section}]")
    if not headers:
        result = list(lines)
        if result and result[-1].strip():
            result.append("")
        result.append(f"[{section}]")
        result.extend(f"{key} = {value}" for key, value in values.items())
        return result
    start = headers[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if _SECTION.match(lines[index])),
        len(lines),
    )
    found: dict[str, int] = {}
    result = list(lines)
    for index in range(start + 1, end):
        match = _KEY.match(result[index])
        if match and match.group(1) in values:
            key = match.group(1)
            if key in found:
                raise ValidationError(f"duplicate workflow-owned TOML key [{section}].{key}")
            found[key] = index
            result[index] = f"{key} = {values[key]}"
    missing = [key for key in values if key not in found]
    result[end:end] = [f"{key} = {values[key]}" for key in missing]
    return result


_WORKFLOW_OWNED_KEYS: dict[str, set[str]] = {
    "agents": {"enabled", "max_concurrent_threads_per_session"},
    "features.multi_agent_v2": {
        "enabled",
        "max_concurrent_threads_per_session",
        "hide_spawn_agent_metadata",
        "tool_namespace",
        "min_wait_timeout_ms",
        "default_wait_timeout_ms",
        "max_wait_timeout_ms",
    },
}


def _remove_owned_keys(
    lines: list[str], owned_by_section: dict[str, set[str]]
) -> list[str]:
    """Drop selected owned keys while retaining unrelated section content."""

    result: list[str] = []
    index = 0
    while index < len(lines):
        header = _SECTION.match(lines[index])
        if header is None or header.group(1) not in owned_by_section:
            result.append(lines[index])
            index += 1
            continue
        section = header.group(1)
        end = next(
            (
                position
                for position in range(index + 1, len(lines))
                if _SECTION.match(lines[position])
            ),
            len(lines),
        )
        retained = [
            line
            for line in lines[index + 1 : end]
            if not (
                (match := _KEY.match(line))
                and match.group(1) in owned_by_section[section]
            )
        ]
        if any(line.strip() for line in retained):
            result.append(lines[index])
            result.extend(retained)
        while result and not result[-1].strip() and (
            index == 0 or not any(line.strip() for line in retained)
        ):
            result.pop()
        index = end
    return result


def remove_workflow_owned_config(text: str) -> str:
    """Remove only the Codex settings owned by this workflow."""
    if not text.strip():
        return ""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ValidationError(f"existing Codex config is invalid TOML: {error}") from error

    lines = text.splitlines()
    result: list[str] = []
    index = 0
    while index < len(lines):
        header = _SECTION.match(lines[index])
        if header is None or header.group(1) not in _WORKFLOW_OWNED_KEYS:
            result.append(lines[index])
            index += 1
            continue

        section = header.group(1)
        end = next(
            (
                position
                for position in range(index + 1, len(lines))
                if _SECTION.match(lines[position])
            ),
            len(lines),
        )
        owned = _WORKFLOW_OWNED_KEYS[section]
        retained: list[str] = []
        for line in lines[index + 1 : end]:
            match = _KEY.match(line)
            if match is None or match.group(1) not in owned:
                retained.append(line)
        if any(line.strip() for line in retained):
            result.append(lines[index])
            result.extend(retained)
        index = end

    rendered = "\n".join(result).rstrip()
    if rendered:
        rendered += "\n"
    try:
        tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as error:
        raise ValidationError(f"generated Codex config is invalid TOML: {error}") from error
    return rendered
