"""Versioned migrations for persistent workflow resources."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .errors import ValidationError

ConfigMigration = Callable[[dict[str, Any]], dict[str, Any]]


def _migrate_v2_to_v3(raw: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(raw)
    workers = list(migrated.get("enabled_workers", []))
    if "end_of_session" not in workers:
        workers.append("end_of_session")
    migrated["enabled_workers"] = workers
    migrated["end_of_session_context_turns"] = 200
    migrated["schema_version"] = 3
    return migrated


def _migrate_v3_to_v4(raw: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(raw)
    migrated.pop("end_of_session_context_turns", None)
    migrated["schema_version"] = 4
    return migrated


CONFIG_MIGRATIONS: dict[int, ConfigMigration] = {
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
}


def migrate_config_resource(
    raw: dict[str, Any], defaults: dict[str, Any]
) -> dict[str, Any]:
    source_version = raw.get("schema_version")
    target_version = defaults.get("schema_version")
    if not isinstance(source_version, int) or not isinstance(target_version, int):
        raise ValidationError("configuration schema_version must be an integer")
    if source_version > target_version:
        raise ValidationError("installed configuration schema is newer than this release")
    migrated = dict(raw)
    while source_version < target_version:
        migration = CONFIG_MIGRATIONS.get(source_version)
        if migration is None:
            raise ValidationError(
                f"no configuration migration from schema {source_version}"
            )
        migrated = migration(migrated)
        next_version = migrated.get("schema_version")
        if not isinstance(next_version, int) or next_version <= source_version:
            raise ValidationError("configuration migration did not advance schema_version")
        source_version = next_version
    for key, value in defaults.items():
        migrated.setdefault(key, value)
    return migrated
