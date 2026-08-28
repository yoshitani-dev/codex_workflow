"""Persistent update-backup planning."""

from __future__ import annotations

from pathlib import Path

from .errors import ValidationError
from .layout import ProjectPaths, RuntimePaths
from .transaction import Mutation


def append_backup_mutations(
    mutations: list[Mutation],
    backup_root: Path,
    runtime: RuntimePaths,
    project: ProjectPaths,
) -> None:
    targets = [
        path
        for path in (runtime.user_agents, runtime.config_toml)
        if path.is_file()
    ]
    if runtime.runtime.is_dir():
        targets.extend(
            path
            for path in runtime.runtime.rglob("*")
            if path.is_file()
            and ".backups" not in path.parts
            and ".source_backup" not in path.parts
        )
    if runtime.agents.is_dir():
        targets.extend(path for path in runtime.agents.glob("*.toml") if path.is_file())
    targets.extend(
        path
        for path in (
            project.active,
            project.disabled,
            project.personalization,
            project.state,
        )
        if path.is_file()
    )
    targets.extend(
        path
        for path in (
            project.orchestration / "config.json",
            project.orchestration / "state.json",
            project.orchestration / "tasks.json",
            project.orchestration / "failures.json",
            project.orchestration / "events.jsonl",
        )
        if path.is_file()
    )
    runs = project.orchestration / "runs"
    if runs.is_dir():
        targets.extend(path for path in runs.rglob("*") if path.is_file())
    seen: set[Path] = set()
    for source in targets:
        if source.is_symlink():
            raise ValidationError(f"refusing to back up symlinked workflow state: {source}")
        resolved = source.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if is_relative_to(source, runtime.codex_home):
            relative = Path("user") / source.relative_to(runtime.codex_home)
        else:
            relative = Path("project") / source.relative_to(project.root)
        mutations.append(Mutation(backup_root / relative, source.read_bytes()))


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
