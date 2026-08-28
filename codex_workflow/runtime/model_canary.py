"""Model-routing diagnostics without claiming configuration is runtime identity."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ._toml import tomllib
from .errors import ValidationError


REQUESTED = {
    "root": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
    "executor_luna": {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
    "executor_sol": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
}


def _worker_config(path: Path) -> dict[str, str]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValidationError(f"cannot read model canary worker config {path}: {error}") from error
    return {
        "model": str(value.get("model", "")),
        "reasoning_effort": str(value.get("model_reasoning_effort", "")),
    }


def _catalog(codex_bin: str) -> tuple[str | None, dict[str, set[str]], str | None]:
    version: str | None = None
    models: dict[str, set[str]] = {}
    error: str | None = None
    try:
        version_run = subprocess.run(
            [codex_bin, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
        if version_run.returncode == 0:
            version = version_run.stdout.strip()
        model_run = subprocess.run(
            [codex_bin, "debug", "models", "--bundled"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if model_run.returncode != 0:
            error = model_run.stderr.strip() or f"codex exited {model_run.returncode}"
        else:
            payload = json.loads(model_run.stdout)
            for item in payload.get("models", []):
                if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
                    continue
                efforts = {
                    str(level.get("effort"))
                    for level in item.get("supported_reasoning_levels", [])
                    if isinstance(level, dict) and level.get("effort")
                }
                models[item["slug"]] = efforts
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as caught:
        error = str(caught)
    return version, models, error


def _runtime_metadata(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read runtime model metadata {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValidationError("runtime model metadata root must be an object")
    return value


def inspect_model_routing(
    *,
    agent_templates: Path,
    codex_bin: str = "codex",
    runtime_metadata: Path | None = None,
) -> dict[str, Any]:
    """Compare requested, configured, catalog-supported, and observed identity.

    Bundled model catalog support proves only that the local Codex build accepts
    a model/effort pair. Actual child identity is marked verified solely when an
    independent runtime metadata artifact is supplied.
    """

    configured = {
        "root": {
            "model": "session-owned",
            "reasoning_effort": "session-owned",
            "note": "select the main-session model in Codex; workflow_config does not own it",
        },
        "executor_luna": _worker_config(agent_templates / "executor_luna.toml"),
        "executor_sol": _worker_config(agent_templates / "executor_sol.toml"),
    }
    version, catalog, catalog_error = _catalog(codex_bin)
    observed = _runtime_metadata(runtime_metadata)
    components: dict[str, Any] = {}
    for name, request in REQUESTED.items():
        observation = observed.get(name)
        exact_observation = isinstance(observation, dict) and all(
            observation.get(key) == expected for key, expected in request.items()
        )
        mismatch = isinstance(observation, dict) and not exact_observation
        model_efforts = catalog.get(request["model"])
        catalog_supported = (
            request["reasoning_effort"] in model_efforts if model_efforts is not None else None
        )
        components[name] = {
            "requested": request,
            "configured": configured[name],
            "catalog_supported": catalog_supported,
            "runtime_verified": "PASS" if exact_observation else "FAIL" if mismatch else "NOT_VERIFIED",
            "runtime_observed": observation if isinstance(observation, dict) else None,
            "fallback": observation if mismatch else None,
            "reason": (
                "independent runtime metadata matched"
                if exact_observation
                else "runtime metadata reported a different model or effort"
                if mismatch
                else "Codex did not expose independent runtime identity metadata to this diagnostic"
            ),
        }
    return {
        "codex_version": version,
        "catalog_error": catalog_error,
        "model_identity": (
            "VERIFIED"
            if all(item["runtime_verified"] == "PASS" for item in components.values())
            else "NOT_VERIFIED"
        ),
        "components": components,
    }
