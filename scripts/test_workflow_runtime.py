"""Regression tests for the lifecycle runtime."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "codex_workflow"
PACKAGE_VERSION = (PACKAGE / "VERSION").read_text(encoding="utf-8").strip()

import sys

sys.path.insert(0, str(PACKAGE))

import workflow as workflow_cli
from runtime.backup import append_backup_mutations
from runtime.config import (
    WorkflowConfig,
    patch_codex_config,
    remove_workflow_owned_config,
    render_heavy_route,
)
from runtime.errors import TransactionError, ValidationError
from runtime.lifecycle import (
    PackageLayout,
    ProjectPaths,
    RuntimePaths,
    materialize_personalization,
    plan_auto_check_update_setting,
    plan_bootstrap,
    plan_configure,
    plan_enable,
    plan_personalize,
    plan_project_install,
    plan_update,
)
from runtime.markers import (
    AUTO_CHECK_UPDATE_PLACEHOLDER,
    PROJECT_LOCAL,
    PROJECT_PERSONALIZATION,
    USER_MANAGED,
    extract,
    render_project_entry,
)
from runtime.migrations import migrate_config_resource
from runtime.plan import OperationPlan, read_string_list, resolve_owned_runtime_path
from runtime.release import (
    ReleaseSelection,
    parse_semver,
    select_releases,
    summarize_release_notes,
)
from runtime.transaction import Mutation, apply


class MarkerTests(unittest.TestCase):
    def test_user_command_contract_keeps_automatic_check_optional(self) -> None:
        instructions = (PACKAGE / "user_AGENTS.md").read_text(encoding="utf-8")
        auto_check = (PACKAGE / "resources" / "auto_check_update.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("auto-check-update --json", instructions)
        self.assertEqual(instructions.count(AUTO_CHECK_UPDATE_PLACEHOLDER), 1)
        self.assertIn("auto-check-update --json", auto_check)
        self.assertIn("codex_workflow --check-update", instructions)
        self.assertIn("codex_workflow --enable_auto_check_update", instructions)
        self.assertIn("codex_workflow --disable_auto_check_update", instructions)
        self.assertIn("codex_workflow --enable_auto_update", instructions)
        self.assertIn("codex_workflow --disable_auto_update", instructions)
        self.assertIn("codex_workflow --remove", instructions)

        personalization = (PACKAGE / "personalization_guide.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("resources/personalization.md", personalization)
        self.assertIn("missing or invalid", personalization)
        self.assertIn("copy that section's complete", personalization)

    def test_template_renders_independent_project_regions(self) -> None:
        template = (PACKAGE / "AGENTS.md").read_text(encoding="utf-8")
        rendered = render_project_entry(
            template,
            personalization="Personal rule.",
            local_instructions="# Existing\nKeep this.",
        )
        self.assertEqual(extract(rendered, PROJECT_PERSONALIZATION), "Personal rule.")
        self.assertEqual(extract(rendered, PROJECT_LOCAL), "# Existing\nKeep this.")

    def test_operational_policies_are_compact_and_knowledge_aware(self) -> None:
        names = (
            "AGENTS.md",
            "medium_route.md",
            "heavy_route.md",
            "explorer_companion.md",
        )
        policies = {
            name: (PACKAGE / name).read_text(encoding="utf-8") for name in names
        }
        for name, text in policies.items():
            limit = 225 if name == "heavy_route.md" else 200
            self.assertLess(len(text.splitlines()), limit, name)

        heavy = policies["heavy_route.md"]
        self.assertIn("recommended approach", heavy.lower())
        self.assertIn("canonical task names", heavy)
        self.assertIn("Decision required: none", heavy)
        self.assertIn("knowledge-delta brief", heavy)
        self.assertIn("Only when the assigned worker is `executor_luna`", heavy)
        self.assertIn("ordered implementation sequence", heavy)
        self.assertIn("Do not add this Execution Guide requirement", heavy)
        self.assertIn("not spawn, message, or otherwise call subagents", heavy)
        self.assertIn("skips End-of-Session and worker statistics", heavy)
        self.assertIn("before the final response", heavy)
        self.assertIn("automatic handoff context fork", heavy)
        self.assertNotIn("compact ledger", heavy)

        medium = policies["medium_route.md"]
        self.assertIn("direct main-agent fast path", medium)
        self.assertIn("do not call `end_of_session`", medium)
        self.assertIn("Before the final response", medium)
        self.assertIn("complete documentation framework", medium)
        self.assertNotIn("usage ledger", medium)

        agents_policy = policies["AGENTS.md"]
        self.assertIn("handoff is not a user command", agents_policy)

        explorer = policies["explorer_companion.md"]
        self.assertIn("planning brief", explorer)
        self.assertIn("knowledge-delta brief", explorer)
        self.assertIn("distinct task names", explorer)

        handoff_contract = (PACKAGE / "end_of_session.md").read_text(
            encoding="utf-8"
        )
        handoff_worker = (PACKAGE / "agents" / "end_of_session.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('task_name="end_of_session_<deployment_id>"', handoff_contract)
        self.assertIn("after every substantive Medium or Heavy", handoff_contract)
        self.assertIn("inherits the deployment context", handoff_contract)
        self.assertIn("complete `agent_docs/` framework", handoff_contract)
        self.assertIn("Do not call a second documentation worker", handoff_contract)
        self.assertNotIn('fork_turns="none"', handoff_contract)
        self.assertNotIn("compact usage ledger", handoff_contract)
        self.assertIn(
            "| Worker name | Quantity | Number of calls |", handoff_worker
        )
        self.assertIn(
            "`Quantity` is the number of distinct task names", handoff_worker
        )
        self.assertIn("turn-starting initial assignments", handoff_worker)
        self.assertIn("read the complete existing", handoff_worker)
        self.assertIn("Do not delegate or create another worker", handoff_worker)
        self.assertIn("the parent does not supply or maintain a ledger", handoff_worker)
        for framework_file in (
            "project_overview.md",
            "project_core_tech.md",
            "project_structure.md",
            "project_progress.md",
            "project_diary.md",
            "latest_session_work.md",
        ):
            self.assertIn(framework_file, handoff_worker)
        for policy in (
            heavy,
            medium,
            agents_policy,
            handoff_contract,
            handoff_worker,
        ):
            self.assertNotIn("end this session", policy.lower())

        tester = (PACKAGE / "agents" / "tester.toml").read_text(encoding="utf-8")
        executor = (PACKAGE / "agents" / "executor_luna.toml").read_text(
            encoding="utf-8"
        )
        terra = (PACKAGE / "agents" / "executor_terra.toml").read_text(
            encoding="utf-8"
        )
        doc_writer = (PACKAGE / "agents" / "doc-writer.toml").read_text(
            encoding="utf-8"
        )
        bootstrap = (PACKAGE / "bootstrap.md").read_text(encoding="utf-8")
        install = (PACKAGE / "install.md").read_text(encoding="utf-8")
        self.assertIn("contact the named executor directly", tester)
        self.assertIn("Do not involve the parent for a routine defect", executor)
        self.assertIn("Execution Guide as the primary work sequence", executor)
        self.assertIn("Track the completion checklist internally", executor)
        self.assertNotIn("Execution Guide as the primary work sequence", terra)
        self.assertIn("always contains one required", bootstrap)
        self.assertIn("explicitly labeled bootstrap/project-install action", doc_writer)
        for luna_worker in (
            "executor_luna",
            "tester",
            "doc-writer",
            "explorer",
            "end_of_session",
        ):
            worker = (PACKAGE / "agents" / f"{luna_worker}.toml").read_text(
                encoding="utf-8"
            )
            self.assertIn('model = "gpt-5.6-luna"', worker)
            self.assertIn('model_reasoning_effort = "max"', worker)
            self.assertIn('service_tier = "default"', worker)
            self.assertNotIn('service_tier = "fast"', worker)
        for standard_tier_worker in ("executor_sol", "executor_terra"):
            worker = (PACKAGE / "agents" / f"{standard_tier_worker}.toml").read_text(
                encoding="utf-8"
            )
            self.assertIn('service_tier = "default"', worker)
        for required_context in (
            "project_structure.md",
            "project_overview.md",
            "project_core_tech.md",
        ):
            self.assertIn(required_context, bootstrap)
            self.assertIn(required_context, install)

    def test_reserved_marker_collision_is_rejected_during_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / "AGENTS.md").write_text(PROJECT_LOCAL.start, encoding="utf-8")
            with self.assertRaises(ValidationError):
                plan_bootstrap(
                    PackageLayout.resolve(PACKAGE),
                    RuntimePaths(root / "home"),
                    ProjectPaths(project),
                )

    def test_package_requires_exact_user_managed_region(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex_workflow"
            shutil.copytree(
                PACKAGE,
                root,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            path = root / "user_AGENTS.md"
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace(USER_MANAGED.start, "", 1), encoding="utf-8"
            )
            with self.assertRaises(ValidationError):
                PackageLayout.resolve(root)

    def test_package_requires_auto_check_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex_workflow"
            shutil.copytree(
                PACKAGE,
                root,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            path = root / "user_AGENTS.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    AUTO_CHECK_UPDATE_PLACEHOLDER, "", 1
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError):
                PackageLayout.resolve(root)

    def test_package_requires_auto_check_instruction_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex_workflow"
            shutil.copytree(
                PACKAGE,
                root,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            (root / "resources" / "auto_check_update.md").write_text(
                "Missing command.\n", encoding="utf-8"
            )
            with self.assertRaises(ValidationError):
                PackageLayout.resolve(root)

    def test_update_help_does_not_publish_local_source_option(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-B", str(PACKAGE / "workflow.py"), "update", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("--source", completed.stdout)
        self.assertNotIn("--apply", completed.stdout)

    def test_explicit_auto_check_commands_and_legacy_aliases_are_available(self) -> None:
        for command in (
            "enable-auto-check-update",
            "disable-auto-check-update",
            "enable-auto-update",
            "disable-auto-update",
            "check-update",
        ):
            completed = subprocess.run(
                [sys.executable, "-B", str(PACKAGE / "workflow.py"), command, "--help"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_configure_help_omits_handoff_context_option(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(PACKAGE / "workflow.py"),
                "configure",
                "--help",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("--handoff-context-turns", completed.stdout)

    def test_remove_help_hides_internal_confirmation_flag(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-B", str(PACKAGE / "workflow.py"), "remove", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("--confirm", completed.stdout)


class SafetyTests(unittest.TestCase):
    def test_owned_runtime_manifest_is_confined_and_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime_root = Path(temporary) / "runtime"
            with self.assertRaises(ValidationError):
                resolve_owned_runtime_path(runtime_root, "../../outside.txt")
            with self.assertRaises(ValidationError):
                resolve_owned_runtime_path(runtime_root, "/tmp/outside.txt")
        with self.assertRaises(ValidationError):
            read_string_list({"owned_runtime_files": None}, "owned_runtime_files")

    def test_backup_skips_missing_optional_user_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            mutations: list[Mutation] = []
            append_backup_mutations(
                mutations,
                root / "backup",
                RuntimePaths(root / "home"),
                ProjectPaths(project),
            )
            self.assertEqual(mutations, [])


class ConfigTests(unittest.TestCase):
    def test_package_default_disables_automatic_update_checks(self) -> None:
        raw = json.loads(
            (PACKAGE / "resources" / "workflow_config.default.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(raw["auto_check_update"])
        self.assertFalse(WorkflowConfig.from_mapping(raw).auto_check_update)

    def test_newer_persistent_schema_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            migrate_config_resource(
                {"schema_version": 5},
                {"schema_version": 4},
            )

    def test_v2_config_migration_enables_handoff_worker(self) -> None:
        migrated = migrate_config_resource(
            {
                "schema_version": 2,
                "enabled_workers": ["executor_luna", "doc-writer", "explorer"],
            },
            {"schema_version": 4},
        )
        self.assertEqual(migrated["schema_version"], 4)
        self.assertIn("end_of_session", migrated["enabled_workers"])
        self.assertNotIn("end_of_session_context_turns", migrated)

    def test_v3_config_migration_removes_handoff_context_setting(self) -> None:
        migrated = migrate_config_resource(
            {
                "schema_version": 3,
                "end_of_session_context_turns": 150,
            },
            {"schema_version": 4},
        )
        self.assertEqual(migrated["schema_version"], 4)
        self.assertNotIn("end_of_session_context_turns", migrated)

    def test_worker_limit_above_platform_limit_is_rejected(self) -> None:
        raw = json.loads(
            (PACKAGE / "resources" / "workflow_config.default.json").read_text(
                encoding="utf-8"
            )
        )
        raw["max_concurrent_workers"] = 21
        with self.assertRaises(ValidationError):
            WorkflowConfig.from_mapping(raw)

    def test_toml_patch_preserves_unrelated_content(self) -> None:
        config = WorkflowConfig.from_mapping(
            json.loads(
                (PACKAGE / "resources" / "workflow_config.default.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        original = 'model = "custom"\n\n[agents]\nenabled = false\nother = 7\n'
        rendered = patch_codex_config(original, config)
        self.assertIn('model = "custom"', rendered)
        self.assertIn("other = 7", rendered)
        self.assertIn("max_concurrent_threads_per_session = 20", rendered)

    def test_toml_patch_migrates_legacy_owned_keys_only(self) -> None:
        config = WorkflowConfig.from_mapping(
            json.loads(
                (PACKAGE / "resources" / "workflow_config.default.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        original = (
            '[features.multi_agent_v2]\n'
            'enabled = true\n'
            'max_concurrent_threads_per_session = 7\n'
            'keep_feature = "keep"\n\n'
            '[agents]\n'
            'enabled = false\n'
            'keep_agent = true\n'
        )
        rendered = patch_codex_config(original, config)
        self.assertIn('keep_feature = "keep"', rendered)
        self.assertIn("keep_agent = true", rendered)
        self.assertNotIn("[features.multi_agent_v2]\nenabled", rendered)
        self.assertEqual(rendered.count("max_concurrent_threads_per_session"), 1)
        self.assertIn("[agents]", rendered)

    def test_toml_remove_preserves_unrelated_content(self) -> None:
        original = (
            'model = "custom"\n\n'
            "[agents]\n"
            "enabled = true\n"
            "keep_agent = true\n\n"
            "[features.multi_agent_v2]\n"
            "enabled = true\n"
            "max_concurrent_threads_per_session = 20\n"
            'keep_feature = "keep"\n'
        )
        rendered = remove_workflow_owned_config(original)
        self.assertIn('model = "custom"', rendered)
        self.assertIn("keep_agent = true", rendered)
        self.assertIn('keep_feature = "keep"', rendered)
        self.assertNotIn("max_concurrent_threads_per_session", rendered)
        self.assertIn("[agents]", rendered)
        self.assertNotIn("enabled = true", rendered)

    def test_heavy_snapshot_is_rendered_from_config(self) -> None:
        config = WorkflowConfig.from_mapping(
            json.loads(
                (PACKAGE / "resources" / "workflow_config.default.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        rendered = render_heavy_route(
            (PACKAGE / "heavy_route.md").read_text(encoding="utf-8"), config
        )
        self.assertIn("Maximum concurrent child workers: `20`", rendered)
        self.assertIn("Default executor: `executor_luna` (`max`", rendered)
        self.assertNotIn("End-of-Session context fork", rendered)
        self.assertIn(
            'fork_turns="200"',
            (PACKAGE / "end_of_session.md").read_text(encoding="utf-8"),
        )


class ReleaseTests(unittest.TestCase):
    def test_check_update_reports_new_release_notes_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "codex-home"
            runtime = home / "codex_workflow"
            runtime.mkdir(parents=True)
            (runtime / "VERSION").write_text("1.1.1\n", encoding="utf-8")
            release = ReleaseSelection(
                "1.2.0",
                parse_semver("1.2.0"),
                "codex_workflow-1.2.0.zip",
                "https://example/1.2.0.zip",
                "https://example/SHA256SUMS",
                "## Changes\n- Add release-note summaries.",
                "https://example/releases/1.2.0",
            )
            output = io.StringIO()
            argv = ["workflow.py", "check-update", "--codex-home", str(home), "--json"]
            with (
                mock.patch.object(workflow_cli, "select_releases", return_value=[release]),
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(workflow_cli.main(), 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["status"], "update available")
            self.assertEqual(summary["updates"][0]["version"], "1.2.0")
            self.assertIn("release-note summaries", summary["summary"])
            self.assertEqual((runtime / "VERSION").read_text(), "1.1.1\n")

    def test_select_releases_keeps_installable_versions_and_notes(self) -> None:
        records = [
            {
                "tag_name": "v1.3.0",
                "draft": False,
                "body": "## Changes\n- Add explicit update summaries.",
                "html_url": "https://github.com/example/releases/1.3.0",
                "assets": [
                    {
                        "name": "codex_workflow-1.3.0.zip",
                        "browser_download_url": "https://example/1.3.0.zip",
                    },
                    {"name": "SHA256SUMS", "browser_download_url": "https://example/sums"},
                ],
            },
            {
                "tag_name": "v1.2.0",
                "draft": False,
                "body": "- Older change",
                "assets": [
                    {
                        "name": "codex_workflow-1.2.0.zip",
                        "browser_download_url": "https://example/1.2.0.zip",
                    },
                    {"name": "SHA256SUMS", "browser_download_url": "https://example/sums"},
                ],
            },
            {"tag_name": "v1.4.0", "draft": True, "assets": []},
        ]
        with mock.patch("runtime.release._read_json_url", return_value=records):
            releases = select_releases()
        self.assertEqual([release.version_text for release in releases], ["1.3.0", "1.2.0"])
        self.assertIn("explicit update summaries", releases[0].release_notes)

    def test_release_note_summary_strips_markdown_and_limits_length(self) -> None:
        summary = summarize_release_notes(
            "## Changes\n- `check-update` now reports [notes](https://example)."
        )
        self.assertEqual(summary, "Changes check-update now reports notes.")
        self.assertEqual(
            summarize_release_notes("", max_length=10),
            "No release notes were provided.",
        )


class TransactionTests(unittest.TestCase):
    def test_failed_transaction_restores_all_targets(self) -> None:
        from runtime import transaction

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.write_bytes(b"old")
            original_write = transaction._atomic_write
            calls = 0

            def fail_once(path: Path, content: bytes, mode: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected failure")
                original_write(path, content, mode)

            with (
                mock.patch("runtime.transaction._atomic_write", side_effect=fail_once),
                self.assertRaises(TransactionError),
            ):
                apply([Mutation(first, b"new"), Mutation(second, b"created")])
            self.assertEqual(first.read_bytes(), b"old")
            self.assertFalse(second.exists())


class LifecycleIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / "codex-home"
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.runtime = RuntimePaths(self.codex_home)
        self.project = ProjectPaths(self.project_root)
        self.package = PackageLayout.resolve(PACKAGE)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def bootstrap(self, *, existing_agents: str | None = None) -> OperationPlan:
        if existing_agents is not None:
            self.project.active.write_text(existing_agents, encoding="utf-8")
        plan = plan_bootstrap(self.package, self.runtime, self.project)
        self.assertFalse(self.codex_home.exists())
        plan.apply()
        return plan

    def incoming_package(self, directory: str, version: str | None = None) -> PackageLayout:
        version = version or PACKAGE_VERSION
        incoming_root = self.root / directory / "codex_workflow"
        shutil.copytree(
            PACKAGE,
            incoming_root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (incoming_root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        user_agents = (incoming_root / "user_AGENTS.md").read_text(encoding="utf-8")
        (incoming_root / "user_AGENTS.md").write_text(
            user_agents.replace(
                f"codex-workflow-version: {PACKAGE_VERSION}",
                f"codex-workflow-version: {version}",
            ),
            encoding="utf-8",
        )
        return PackageLayout.resolve(incoming_root)

    def test_bootstrap_imports_existing_agents_and_materializes_runtime(self) -> None:
        plan = self.bootstrap(
            existing_agents="# Existing instructions\nKeep local policy.\n"
        )
        entry = self.project.active.read_text(encoding="utf-8")
        self.assertEqual(
            extract(entry, PROJECT_LOCAL),
            "# Existing instructions\nKeep local policy.",
        )
        self.assertTrue((self.runtime.runtime / "workflow.py").is_file())
        self.assertTrue((self.runtime.runtime / "templates" / "AGENTS.md").is_file())
        self.assertTrue((self.runtime.agents / "executor_luna.toml").is_file())
        self.assertTrue((self.runtime.agents / "executor_terra.toml").is_file())
        self.assertTrue((self.runtime.agents / "end_of_session.toml").is_file())
        self.assertIn(
            "max_concurrent_threads_per_session = 20",
            self.runtime.config_toml.read_text(encoding="utf-8"),
        )
        installed_user_agents = self.runtime.user_agents.read_text(encoding="utf-8")
        self.assertNotIn("auto-check-update --json", installed_user_agents)
        self.assertNotIn(AUTO_CHECK_UPDATE_PLACEHOLDER, installed_user_agents)
        self.assertEqual(len(plan.agent_actions), 1)
        action = plan.agent_actions[0]
        self.assertEqual(action["role"], "doc-writer")
        self.assertTrue(action["required"])
        self.assertEqual(set(action["files"]), set(action["framework"]))
        self.assertEqual(
            action["required_context_files"],
            [
                "project_structure.md",
                "project_overview.md",
                "project_core_tech.md",
            ],
        )

        repeated = plan_project_install(self.package, self.project)
        self.assertEqual(len(repeated.agent_actions), 1)
        self.assertTrue(repeated.agent_actions[0]["required"])
        self.assertEqual(
            set(repeated.agent_actions[0]["files"]),
            set(repeated.agent_actions[0]["framework"]),
        )
        self.assertEqual(repeated.agent_actions[0]["created_files"], [])
        self.assertEqual(
            set(repeated.agent_actions[0]["recovery_files"]),
            set(repeated.agent_actions[0]["framework"]),
        )

    def test_bootstrap_cleans_project_staging_and_updates_gitignore(self) -> None:
        staging = self.project_root / "Codex_Workflow"
        (staging / "nested").mkdir(parents=True)
        (staging / "nested" / "package.txt").write_text("staged", encoding="utf-8")
        (self.project_root / ".gitignore").write_text("# local rules\n", encoding="utf-8")

        self.bootstrap()

        self.assertFalse(staging.exists())
        gitignore = (self.project_root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("# local rules\n", gitignore)
        for entry in (
            "agent_docs/",
            ".codex_workflow_hidden_resources/",
            ".orchestration/",
            "AGENTS.md",
        ):
            self.assertEqual(gitignore.splitlines().count(entry), 1)

        # A repeated project install is idempotent and does not duplicate rules.
        plan_project_install(self.package, self.project).apply()
        repeated = (self.project_root / ".gitignore").read_text(encoding="utf-8")
        for entry in (
            "agent_docs/",
            ".codex_workflow_hidden_resources/",
            ".orchestration/",
            "AGENTS.md",
        ):
            self.assertEqual(repeated.splitlines().count(entry), 1)

    def test_unactivated_workers_are_materialized_for_codex(self) -> None:
        self.bootstrap()
        for worker in self.package.worker_names:
            self.assertTrue((self.runtime.agents / f"{worker}.toml").is_file())
            self.assertTrue(
                (self.runtime.runtime / "templates" / "agents" / f"{worker}.toml").is_file()
            )
        state = json.loads((self.runtime.runtime / "install_state.json").read_text())
        self.assertEqual(set(state["owned_workers"]), self.package.worker_names)

    def test_configure_switches_default_executor_without_touching_local_region(self) -> None:
        self.bootstrap(existing_agents="Local policy.\n")
        plan = plan_configure(
            self.runtime,
            {
                "default_executor": "executor_terra",
                "default_executor_reasoning_effort": "max",
                "max_concurrent_workers": 7,
            },
        )
        plan.apply()
        configured = json.loads(
            (self.runtime.runtime / "workflow_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(configured["default_executor"], "executor_terra")
        self.assertEqual(configured["max_concurrent_workers"], 7)
        self.assertNotIn("end_of_session_context_turns", configured)
        self.assertIn(
            'fork_turns="200"',
            (self.runtime.runtime / "end_of_session.md").read_text(encoding="utf-8"),
        )
        self.assertTrue((self.runtime.agents / "executor_luna.toml").is_file())
        terra = (self.runtime.agents / "executor_terra.toml").read_text(encoding="utf-8")
        self.assertIn('model_reasoning_effort = "max"', terra)
        self.assertEqual(extract(self.project.active.read_text(), PROJECT_LOCAL), "Local policy.")

    def test_configure_keeps_unactivated_worker_definitions_materialized(self) -> None:
        self.bootstrap()
        current = json.loads(
            (self.runtime.runtime / "workflow_config.json").read_text(encoding="utf-8")
        )
        current["enabled_workers"].remove("explorer")
        plan_configure(self.runtime, {"enabled_workers": current["enabled_workers"]}).apply()
        self.assertTrue((self.runtime.agents / "explorer.toml").is_file())
        state = json.loads((self.runtime.runtime / "install_state.json").read_text())
        self.assertIn("explorer", state["owned_workers"])

    def test_configure_skips_unchanged_global_entry_and_codex_config(self) -> None:
        self.bootstrap()
        plan = plan_configure(self.runtime, {})
        targets = {mutation.path for mutation in plan.mutations}
        self.assertNotIn(self.runtime.user_agents, targets)
        self.assertNotIn(self.runtime.config_toml, targets)

    def test_configure_materializes_auto_check_instruction_when_changed(self) -> None:
        self.bootstrap()
        plan_configure(self.runtime, {"auto_check_update": True}).apply()
        configured = json.loads(
            (self.runtime.runtime / "workflow_config.json").read_text(encoding="utf-8")
        )
        self.assertTrue(configured["auto_check_update"])
        self.assertIn(
            "auto-check-update --json",
            self.runtime.user_agents.read_text(encoding="utf-8"),
        )

    def test_personalize_and_enable_disable_preserve_regions(self) -> None:
        self.bootstrap(existing_agents="Local policy.\n")
        customized = (PACKAGE / "resources" / "personalization.md").read_text(
            encoding="utf-8"
        ).replace(
            "Status: default\nDecision: Preserve the workflow-managed default Design Principles.",
            "Status: customized\nDecision: Prefer explicit ports and adapters.",
        )
        plan_personalize(self.project, customized).apply()
        entry = self.project.active.read_text(encoding="utf-8")
        self.assertEqual(extract(entry, PROJECT_PERSONALIZATION), "Prefer explicit ports and adapters.")
        self.assertEqual(extract(entry, PROJECT_LOCAL), "Local policy.")
        plan_enable(self.project, enable=False).apply()
        self.assertFalse(self.project.active.exists())
        self.assertTrue(self.project.disabled.exists())
        plan_enable(self.project, enable=True).apply()
        self.assertTrue(self.project.active.exists())
        self.assertFalse(self.project.disabled.exists())

        self.project.personalization.unlink()
        defaults = (PACKAGE / "resources" / "personalization.md").read_text(
            encoding="utf-8"
        )
        plan_personalize(self.project, defaults).apply()
        self.assertEqual(self.project.personalization.read_text(encoding="utf-8"), defaults)
        self.assertEqual(
            extract(self.project.active.read_text(encoding="utf-8"), PROJECT_PERSONALIZATION),
            "",
        )

    def test_install_rejects_personalization_resource_drift(self) -> None:
        self.bootstrap()
        resource = self.project.personalization.read_text(encoding="utf-8")
        self.project.personalization.write_text(
            resource.replace(
                "Status: default\nDecision: Preserve the workflow-managed default Design Principles.",
                "Status: customized\nDecision: Prefer explicit ports and adapters.",
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValidationError):
            plan_project_install(self.package, self.project)

    def test_update_preserves_configuration_managed_and_local_content(self) -> None:
        self.bootstrap(existing_agents="Local policy.\n")
        plan_configure(
            self.runtime,
            {
                "default_executor": "executor_terra",
                "default_executor_reasoning_effort": "high",
                "max_concurrent_workers": 7,
            },
        ).apply()
        plan_auto_check_update_setting(self.runtime, enabled=True).apply()
        self.assertIn(
            "auto-check-update --json",
            self.runtime.user_agents.read_text(encoding="utf-8"),
        )
        installed_config_path = self.runtime.runtime / "workflow_config.json"
        (self.runtime.agents / "executor_luna.toml").write_text(
            "# local worker override\n", encoding="utf-8"
        )
        incoming_root = self.root / "incoming" / "codex_workflow"
        shutil.copytree(PACKAGE, incoming_root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (incoming_root / "VERSION").write_text("1.1.3\n", encoding="utf-8")
        user_agents = (incoming_root / "user_AGENTS.md").read_text(encoding="utf-8")
        (incoming_root / "user_AGENTS.md").write_text(
            user_agents.replace(
                f"codex-workflow-version: {PACKAGE_VERSION}",
                "codex-workflow-version: 1.1.3",
            ),
            encoding="utf-8",
        )
        incoming = PackageLayout.resolve(incoming_root)
        plan_update(incoming, self.runtime, self.project).apply()
        entry = self.project.active.read_text(encoding="utf-8")
        self.assertEqual(extract(entry, PROJECT_LOCAL), "Local policy.")
        self.assertEqual((self.runtime.runtime / "VERSION").read_text(), "1.1.3\n")
        updated_config = json.loads(installed_config_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_config["default_executor"], "executor_terra")
        self.assertEqual(updated_config["max_concurrent_workers"], 7)
        self.assertEqual(updated_config["default_executor_reasoning_effort"], "high")
        self.assertTrue(updated_config["auto_check_update"])
        self.assertIn(
            "auto-check-update --json",
            self.runtime.user_agents.read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "local worker override",
            (self.runtime.agents / "executor_luna.toml").read_text(encoding="utf-8"),
        )
        self.assertTrue(any((self.runtime.runtime / ".backups").iterdir()))

    def test_projects_update_against_their_recorded_historical_sources(self) -> None:
        self.bootstrap()
        second_root = self.root / "second-project"
        second_root.mkdir()
        second = ProjectPaths(second_root)
        plan_project_install(self.package, second).apply()

        incoming = self.incoming_package("multi-project-incoming", "1.3.0")
        incoming_template = incoming.project_template.read_text(encoding="utf-8")
        incoming.project_template.write_text(
            incoming_template.replace("## Working State", "## Working State (1.2)"),
            encoding="utf-8",
        )
        incoming = PackageLayout.resolve(incoming.root)

        plan_update(incoming, self.runtime, self.project).apply()
        second_plan = plan_update(incoming, self.runtime, second)
        self.assertEqual(second_plan.details["from_version"], "1.3.0")
        self.assertEqual(second_plan.details["project_from_version"], PACKAGE_VERSION)
        second_plan.apply()
        self.assertIn(
            "## Working State (1.2)", second.active.read_text(encoding="utf-8")
        )

    def test_update_applies_config_migration_without_resetting_user_values(self) -> None:
        self.bootstrap()
        config_path = self.runtime.runtime / "workflow_config.json"
        configured = json.loads(config_path.read_text(encoding="utf-8"))
        configured["schema_version"] = 3
        configured["default_executor_reasoning_effort"] = "max"
        configured["max_concurrent_workers"] = 9
        configured["end_of_session_context_turns"] = 150
        config_path.write_text(json.dumps(configured) + "\n", encoding="utf-8")

        incoming = self.incoming_package("config-migration-incoming", "1.2.1")
        plan_update(incoming, self.runtime, self.project).apply()
        migrated = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["schema_version"], 4)
        self.assertEqual(migrated["default_executor_reasoning_effort"], "max")
        self.assertEqual(migrated["max_concurrent_workers"], 9)
        self.assertNotIn("end_of_session_context_turns", migrated)

    def test_cli_install_reports_enabled_disabled_and_stale_states(self) -> None:
        self.bootstrap()
        command = [
            sys.executable,
            "-B",
            str(self.runtime.runtime / "workflow.py"),
            "install",
            "--codex-home",
            str(self.codex_home),
            "--project",
            str(self.project_root),
            "--json",
        ]
        recovery = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(recovery.returncode, 0, recovery.stderr)
        recovery_summary = json.loads(recovery.stdout)
        self.assertTrue(recovery_summary["applied"])
        self.assertEqual(
            set(recovery_summary["agent_actions"][0]["recovery_files"]),
            set(recovery_summary["agent_actions"][0]["framework"]),
        )
        for document in self.project.docs.glob("*.md"):
            document.write_text(
                document.read_text(encoding="utf-8").replace(
                    "<!-- codex-workflow-bootstrap-template -->\n", ""
                ),
                encoding="utf-8",
            )

        enabled = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        self.assertEqual(json.loads(enabled.stdout)["status"], "already enabled")
        self.assertEqual(json.loads(enabled.stdout)["instruction"], "No action is required.")

        plan_enable(self.project, enable=False).apply()
        disabled = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertEqual(json.loads(disabled.stdout)["status"], "already disabled")
        self.assertIn("--enable", json.loads(disabled.stdout)["instruction"])

        plan_enable(self.project, enable=True).apply()
        text = self.project.active.read_text(encoding="utf-8")
        self.project.active.write_text(
            text.replace("## Working State", "## Locally Changed Working State"),
            encoding="utf-8",
        )
        stale = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(stale.returncode, 1)
        self.assertIn("--update", json.loads(stale.stdout)["error"])

    def test_update_preserves_disabled_project_state(self) -> None:
        self.bootstrap()
        plan_enable(self.project, enable=False).apply()
        plan_update(
            self.incoming_package("disabled-incoming"),
            self.runtime,
            self.project,
        ).apply()
        self.assertFalse(self.project.active.exists())
        self.assertTrue(self.project.disabled.exists())
        state = json.loads(self.project.state.read_text(encoding="utf-8"))
        self.assertFalse(state["enabled"])

    def test_cli_install_applies_without_confirmation_flag(self) -> None:
        project_root = self.root / "cli-project"
        project_root.mkdir()
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(PACKAGE / "workflow.py"),
                "install",
                "--package-root",
                str(PACKAGE),
                "--codex-home",
                str(self.codex_home),
                "--project",
                str(project_root),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertTrue(summary["applied"])
        self.assertEqual(len(summary["agent_actions"]), 1)
        self.assertTrue(summary["agent_actions"][0]["required"])
        self.assertEqual(
            summary["agent_actions"][0]["required_context_files"],
            [
                "project_structure.md",
                "project_overview.md",
                "project_core_tech.md",
            ],
        )
        self.assertTrue((project_root / "AGENTS.md").is_file())

    def test_remove_requires_second_confirmation_and_cleans_owned_files(self) -> None:
        self.bootstrap(existing_agents="Local policy.\n")
        user_agents = self.runtime.user_agents.read_text(encoding="utf-8")
        self.runtime.user_agents.write_text(
            "# Keep this user policy.\n\n" + user_agents,
            encoding="utf-8",
        )
        config = self.runtime.config_toml.read_text(encoding="utf-8")
        config = config.replace(
            "[agents]\nenabled = true",
            "[agents]\nenabled = true\nkeep_agent = true",
        )
        config = config.replace(
            "[features.multi_agent_v2]\nenabled = true",
            '[features.multi_agent_v2]\nenabled = true\nkeep_feature = "keep"',
        )
        if "keep_feature" not in config:
            config += '\n[features.multi_agent_v2]\nkeep_feature = "keep"\n'
        self.runtime.config_toml.write_text(
            'model = "keep"\n\n' + config,
            encoding="utf-8",
        )
        unrelated_worker = self.runtime.agents / "unrelated.toml"
        unrelated_worker.write_text('model = "keep"\n', encoding="utf-8")

        command = [
            sys.executable,
            "-B",
            str(self.runtime.runtime / "workflow.py"),
            "remove",
            "--codex-home",
            str(self.codex_home),
            "--project",
            str(self.project_root),
            "--json",
        ]
        planned = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        planned_summary = json.loads(planned.stdout)
        self.assertFalse(planned_summary["applied"])
        self.assertTrue(planned_summary["confirmation_required"])
        self.assertTrue(self.project.active.is_file())
        self.assertTrue(self.runtime.runtime.is_dir())

        confirmed = subprocess.run(
            [*command[:-1], "--confirm", "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        self.assertTrue(json.loads(confirmed.stdout)["applied"])
        self.assertFalse(self.project.active.exists())
        self.assertFalse(self.project.hidden_dir.exists())
        self.assertTrue((self.project.docs / "project_overview.md").is_file())
        self.assertFalse(self.runtime.runtime.exists())
        self.assertTrue(unrelated_worker.is_file())
        self.assertEqual(
            self.runtime.user_agents.read_text(encoding="utf-8"),
            "# Keep this user policy.\n",
        )
        remaining_config = self.runtime.config_toml.read_text(encoding="utf-8")
        self.assertIn('model = "keep"', remaining_config)
        self.assertIn("keep_agent = true", remaining_config)
        self.assertIn('keep_feature = "keep"', remaining_config)
        self.assertNotIn("max_concurrent_threads_per_session", remaining_config)

    def test_update_allows_missing_optional_codex_config(self) -> None:
        self.bootstrap()
        self.runtime.config_toml.unlink()
        plan = plan_update(
            self.incoming_package("missing-config-incoming"),
            self.runtime,
            self.project,
        )
        self.assertEqual(plan.operation, "update")

    def test_update_rejects_unsafe_owned_runtime_state(self) -> None:
        self.bootstrap()
        outside = self.root / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        state_path = self.runtime.runtime / "install_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["owned_runtime_files"] = ["../../outside.txt"]
        state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        with self.assertRaises(ValidationError):
            plan_update(
                self.incoming_package("unsafe-state-incoming"),
                self.runtime,
                self.project,
            )
        self.assertTrue(outside.is_file())

    def test_disable_auto_check_is_scoped_and_skips_network_check(self) -> None:
        self.bootstrap()
        default_user_agents = self.runtime.user_agents.read_text(encoding="utf-8")
        self.assertNotIn("auto-check-update --json", default_user_agents)
        self.runtime.user_agents.write_text(
            default_user_agents + "\nUser-level custom instruction.\n",
            encoding="utf-8",
        )
        plan = plan_auto_check_update_setting(self.runtime, enabled=False)
        self.assertEqual(len(plan.mutations), 1)
        self.assertNotIn(
            self.runtime.user_agents,
            {mutation.path for mutation in plan.mutations},
        )
        plan.apply()
        configured = json.loads(
            (self.runtime.runtime / "workflow_config.json").read_text(encoding="utf-8")
        )
        self.assertFalse(configured["auto_check_update"])
        self.assertNotIn(
            "auto-check-update --json",
            self.runtime.user_agents.read_text(encoding="utf-8"),
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.runtime.runtime / "workflow.py"),
                "auto-check-update",
                "--codex-home",
                str(self.codex_home),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "disabled")

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.runtime.runtime / "workflow.py"),
                "enable-auto-check-update",
                "--codex-home",
                str(self.codex_home),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(
            json.loads(
                (self.runtime.runtime / "workflow_config.json").read_text(
                    encoding="utf-8"
                )
            )["auto_check_update"]
        )
        enabled_user_agents = self.runtime.user_agents.read_text(encoding="utf-8")
        self.assertIn("auto-check-update --json", enabled_user_agents)
        self.assertIn("User-level custom instruction.", enabled_user_agents)
        self.assertNotIn(AUTO_CHECK_UPDATE_PLACEHOLDER, enabled_user_agents)

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.runtime.runtime / "workflow.py"),
                "disable-auto-check-update",
                "--codex-home",
                str(self.codex_home),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(
            json.loads(
                (self.runtime.runtime / "workflow_config.json").read_text(
                    encoding="utf-8"
                )
            )["auto_check_update"]
        )
        disabled_user_agents = self.runtime.user_agents.read_text(encoding="utf-8")
        self.assertNotIn("auto-check-update --json", disabled_user_agents)
        self.assertIn("User-level custom instruction.", disabled_user_agents)

    def test_legacy_entry_with_edits_requires_reviewed_local_instructions(self) -> None:
        self.bootstrap()
        installed_template_path = self.runtime.runtime / "templates" / "AGENTS.md"
        legacy_template = installed_template_path.read_text(encoding="utf-8")
        legacy_template = legacy_template.replace(
            "<!-- codex-workflow-managed-start -->\n", ""
        ).replace("<!-- codex-workflow-managed-end -->\n\n", "")
        legacy_template = legacy_template.replace(
            "\n<!-- codex-workflow-project-local-instructions-start -->\n"
            "<!-- codex-workflow-project-local-instructions-end -->\n",
            "\n",
        )
        installed_template_path.write_text(legacy_template, encoding="utf-8")
        self.project.active.write_text(
            legacy_template + "\nLocal legacy addition.\n", encoding="utf-8"
        )
        incoming = self.incoming_package("legacy-incoming")
        with self.assertRaises(ValidationError):
            plan_update(incoming, self.runtime, self.project)
        plan_update(
            incoming,
            self.runtime,
            self.project,
            legacy_local_instructions="Local legacy addition.",
        ).apply()
        self.assertEqual(
            extract(self.project.active.read_text(encoding="utf-8"), PROJECT_LOCAL),
            "Local legacy addition.",
        )

    def test_update_rejects_drift_in_workflow_managed_region(self) -> None:
        self.bootstrap()
        entry = self.project.active.read_text(encoding="utf-8")
        self.project.active.write_text(
            entry.replace("## Working State", "## Locally Changed Working State"),
            encoding="utf-8",
        )
        incoming = self.incoming_package("drift-incoming")
        with self.assertRaises(ValidationError):
            plan_update(incoming, self.runtime, self.project)

    def test_installed_launcher_delegates_to_incoming_update_runtime(self) -> None:
        self.bootstrap()
        incoming_root = self.root / "delegated-incoming" / "codex_workflow"
        shutil.copytree(
            PACKAGE,
            incoming_root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (incoming_root / "VERSION").write_text("1.2.2\n", encoding="utf-8")
        user_agents = (incoming_root / "user_AGENTS.md").read_text(encoding="utf-8")
        (incoming_root / "user_AGENTS.md").write_text(
            user_agents.replace(
                f"codex-workflow-version: {PACKAGE_VERSION}",
                "codex-workflow-version: 1.2.2",
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.runtime.runtime / "workflow.py"),
                "update",
                "--source",
                str(incoming_root),
                "--codex-home",
                str(self.codex_home),
                "--project",
                str(self.project_root),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["details"]["to_version"], "1.2.2")
        self.assertTrue(summary["applied"])


class PersonalizationTests(unittest.TestCase):
    def test_only_customized_decisions_are_materialized(self) -> None:
        text = (PACKAGE / "resources" / "personalization.md").read_text(encoding="utf-8")
        self.assertEqual(materialize_personalization(text), "")
        customized = text.replace(
            "Status: default\nDecision: No additional frontend profile.",
            "Status: customized\nDecision: Use the frontend profile.",
        )
        self.assertEqual(materialize_personalization(customized), "Use the frontend profile.")


if __name__ == "__main__":
    unittest.main()
