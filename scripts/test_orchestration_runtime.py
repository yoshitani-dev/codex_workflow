"""Integration tests for the persistent Heavy-route state extension."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "codex_workflow"
sys.path.insert(0, str(PACKAGE))

from runtime.config import WorkflowConfig
from runtime.errors import ValidationError
from runtime.orchestration import (
    OrchestrationEngine,
    initial_orchestration_mutations,
    route_request,
)
from runtime.transaction import apply


def task(
    task_id: str,
    *,
    dependencies: list[str] | None = None,
    scope: str | None = None,
    owner: str = "executor_luna",
) -> dict[str, object]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "description": f"Bounded work for {task_id}",
        "dependencies": dependencies or [],
        "acceptance_criteria": [{"id": "AC1", "text": "Behavior is verified"}],
        "verification": {"required": [f"verify {task_id}"]},
        "owner": owner,
        "write_scope": [scope or f"src/{task_id}/**"],
    }


PASS = {
    "status": "PASS",
    "criteria": {"AC1": {"status": "PASS", "evidence": "focused check passed"}},
    "evidence": ["test artifact"],
}


FAILURE = {
    "failed_test": "test_behavior",
    "exception_type": "AssertionError",
    "message": "expected true, got false at src/core.py:42",
    "command": "python -m unittest test_behavior",
    "stack_location": "src/core.py:42",
    "acceptance_criterion": "AC1",
    "classification": "routine",
    "strategy": "repair invariant",
    "acceptance_progress": True,
}


SPECIALIST = {
    "ROOT_CAUSE": "The invariant is updated after its consumer.",
    "EVIDENCE": ["focused trace"],
    "RECOMMENDED_FIX": "Update the invariant before the consumer.",
    "ALTERNATIVES": ["serialize the operation"],
    "RISKS": ["ordering regression"],
    "VERIFICATION_PLAN": ["rerun the focused test"],
    "CONFIDENCE": "high",
    "IMPLEMENTATION_OWNER": "executor_luna",
}


class OrchestrationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        defaults = (PACKAGE / "resources" / "orchestration_config.default.json").read_text(
            encoding="utf-8"
        )
        apply(
            initial_orchestration_mutations(
                self.project,
                workflow_version="test",
                config_text=defaults,
                now="2026-08-26T00:00:00Z",
            )
        )
        raw = json.loads(
            (PACKAGE / "resources" / "workflow_config.default.json").read_text(
                encoding="utf-8"
            )
        )
        self.workflow_config = WorkflowConfig.from_mapping(raw)
        self.engine = OrchestrationEngine(self.project, self.workflow_config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start(self, tasks: list[dict[str, object]]) -> None:
        self.engine.start_deployment("test_deployment")
        self.engine.import_heavy_plan(
            {"source": "heavy_plan", "plan_id": "HP-1", "tasks": tasks}
        )

    def execute_and_pass(self, task_id: str, suffix: str = "1") -> dict[str, object]:
        self.engine.dispatch(task_id, agent_instance=f"luna-{suffix}")
        self.engine.record_execution_result(task_id, {"status": "implemented"})
        self.engine.register_auxiliary_agent(
            role="tester", agent_instance=f"tester-{suffix}", task_id=task_id
        )
        return self.engine.record_verification(task_id, PASS)

    def test_a_trivial_auto_route_uses_light_without_agents(self) -> None:
        result = route_request(
            {
                "subtasks": 1,
                "dependency_depth": 0,
                "file_count": 1,
                "parallelizable_tasks": 0,
                "expected_iterations": 1,
                "risk": "low",
                "verification_need": "low",
            }
        )
        self.assertEqual(result["route"], "light")
        self.assertEqual(self.engine.status()["active_agents"], [])

    def test_b_bounded_luna_then_tester_then_end_of_session(self) -> None:
        routed = route_request(
            {
                "subtasks": 1,
                "dependency_depth": 0,
                "file_count": 1,
                "parallelizable_tasks": 0,
                "expected_iterations": 1,
                "risk": "low",
                "verification_need": "high",
            }
        )
        self.assertEqual(routed["route"], "heavy")
        self.start([task("T1")])
        schedule = self.engine.begin_iteration()["schedule"]
        self.assertEqual([item["task_id"] for item in schedule["dispatchable"]], ["T1"])
        result = self.execute_and_pass("T1")
        self.assertTrue(result["closure_ready"])
        self.engine.register_auxiliary_agent(
            role="end_of_session", agent_instance="eos-1"
        )
        closed = self.engine.close(
            closure_state="complete", end_of_session_status="PASS"
        )
        self.assertEqual(closed["phase"], "DONE")

    def test_c_parallel_scheduler_caps_luna_at_four_and_avoids_conflicts(self) -> None:
        self.start([task(f"T{index}") for index in range(1, 6)])
        schedule = self.engine.begin_iteration()["schedule"]
        self.assertEqual(schedule["allocated_luna"], 4)
        self.assertLessEqual(len(schedule["dispatchable"]), 4)
        scopes = [tuple(item["write_scope"]) for item in schedule["dispatchable"]]
        self.assertEqual(len(scopes), len(set(scopes)))

        other = OrchestrationEngine(self.project, self.workflow_config)
        for index, item in enumerate(schedule["dispatchable"], start=1):
            other.dispatch(item["task_id"], agent_instance=f"luna-{index}")
        self.assertEqual(other.schedule()["schedule"]["active_luna"], 4)

    def test_write_scope_conflict_is_not_dispatched_concurrently(self) -> None:
        self.start(
            [
                task("T1", scope="src/shared/**"),
                task("T2", scope="src/shared/file.py"),
            ]
        )
        schedule = self.engine.begin_iteration()["schedule"]
        self.assertEqual(len(schedule["dispatchable"]), 1)
        self.assertEqual(len(schedule["write_scope_conflicts"]), 1)

    def test_parallel_result_remains_valid_after_another_task_enters_repair(self) -> None:
        self.start([task("T1"), task("T2")])
        schedule = self.engine.begin_iteration()["schedule"]
        self.assertEqual(len(schedule["dispatchable"]), 2)
        self.engine.dispatch("T1", agent_instance="luna-1")
        self.engine.dispatch("T2", agent_instance="luna-2")
        self.engine.record_failure("T1", FAILURE)
        result = self.engine.record_execution_result("T2", {"status": "implemented"})
        self.assertEqual(result["task_counts"]["verifying"], 1)

    def test_d_routine_repair_stays_between_executor_and_tester(self) -> None:
        self.start([task("T1")])
        self.engine.begin_iteration()
        self.engine.dispatch("T1", agent_instance="luna-1")
        decision = self.engine.record_failure("T1", FAILURE)["decision"]
        self.assertEqual(decision["action"], "routine_repair")
        self.assertFalse(decision["root_attention"])
        result = self.execute_and_pass("T1", "2")
        self.assertTrue(result["closure_ready"])

    def test_e_repeated_signature_escalates_to_sol_then_luna_and_tester(self) -> None:
        self.start([task("T1")])
        self.engine.begin_iteration()
        self.engine.dispatch("T1", agent_instance="luna-1")
        self.engine.record_failure("T1", FAILURE)
        self.engine.dispatch("T1", agent_instance="luna-2")
        relocated = {
            **FAILURE,
            "message": "expected true, got false at src/core.py:99",
            "stack_location": "src/core.py:99",
        }
        decision = self.engine.record_failure("T1", relocated)["decision"]
        self.assertEqual(decision["action"], "escalate_sol")
        self.assertTrue(decision["root_attention"])
        self.engine.dispatch("T1", agent_instance="sol-1")
        result = self.engine.record_specialist_result("T1", SPECIALIST)
        self.assertEqual(result["phase"], "READY")
        passed = self.execute_and_pass("T1", "3")
        self.assertTrue(passed["closure_ready"])

    def test_f_restart_recovery_reopens_missing_running_agent(self) -> None:
        self.start([task("T1")])
        self.engine.begin_iteration()
        self.engine.dispatch("T1", agent_instance="luna-1")
        restarted = OrchestrationEngine(self.project, self.workflow_config)
        result = restarted.reconcile({"active_agent_ids": [], "verification": {}})
        self.assertEqual(result["task_counts"]["ready"], 1)
        self.assertEqual(result["active_agents"], [])

    def test_g_capacity_keeps_auxiliary_slots_and_enforces_sol_one(self) -> None:
        self.start([task(f"T{index}") for index in range(1, 6)])
        self.engine.begin_iteration()
        self.engine.register_auxiliary_agent(role="tester", agent_instance="tester-1")
        self.engine.register_auxiliary_agent(role="explorer", agent_instance="explorer-1")
        self.engine.register_auxiliary_agent(
            role="end_of_session", agent_instance="eos-1"
        )
        self.engine.register_auxiliary_agent(
            role="executor_sol", agent_instance="sol-1", write_scope=["analysis/**"]
        )
        schedule = self.engine.schedule()["schedule"]
        self.assertEqual(schedule["platform_child_capacity"], 20)
        self.assertEqual(schedule["allocated_luna"], 4)
        self.assertEqual(schedule["max_sol_executors"], 1)
        with self.assertRaises(ValidationError):
            self.engine.register_auxiliary_agent(
                role="executor_sol", agent_instance="sol-2", write_scope=["other/**"]
            )

    def test_h_objective_failure_reopens_stored_done_task(self) -> None:
        self.start([task("T1")])
        self.engine.begin_iteration()
        self.execute_and_pass("T1")
        result = self.engine.reconcile({"verification": {"T1": "FAIL"}})
        self.assertFalse(result["closure_ready"])
        self.assertEqual(result["task_counts"]["ready"], 1)

    def test_not_tested_is_not_pass(self) -> None:
        self.start([task("T1")])
        self.engine.begin_iteration()
        self.engine.dispatch("T1", agent_instance="luna-1")
        self.engine.record_execution_result("T1", {"status": "implemented"})
        self.engine.register_auxiliary_agent(
            role="tester", agent_instance="tester-1", task_id="T1"
        )
        result = self.engine.record_verification(
            "T1", {"status": "NOT_TESTED", "criteria": {}}
        )
        self.assertFalse(result["closure_ready"])
        self.assertEqual(result["decision"]["action"], "routine_repair")

    def test_cycle_is_rejected_before_persistence(self) -> None:
        self.engine.start_deployment("test_deployment")
        with self.assertRaises(ValidationError):
            self.engine.import_heavy_plan(
                {
                    "source": "heavy_plan",
                    "tasks": [
                        task("T1", dependencies=["T2"]),
                        task("T2", dependencies=["T1"]),
                    ],
                }
            )
        self.assertEqual(self.engine.status()["task_counts"]["planned"], 0)

    def test_events_are_append_only_and_sequenced(self) -> None:
        self.start([task("T1")])
        self.engine.begin_iteration()
        lines = (self.project / ".orchestration" / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        events = [json.loads(line) for line in lines]
        self.assertEqual([item["seq"] for item in events], list(range(1, len(events) + 1)))
        self.assertTrue(any(item["event"] == "heavy_plan_imported" for item in events))

    def test_corrupt_persisted_task_is_rejected_before_progress(self) -> None:
        self.start([task("T1")])
        path = self.project / ".orchestration" / "tasks.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["tasks"][0]["status"] = "invented"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.engine.status()

    def test_macro_iteration_budget_is_finite(self) -> None:
        config_path = self.project / ".orchestration" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["max_macro_iterations"] = 1
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.start([task("T1")])
        self.engine.begin_iteration()
        exhausted = self.engine.begin_iteration()
        self.assertEqual(exhausted["phase"], "FAILED")
        self.assertEqual(exhausted["macro_iteration"], 2)

    def test_reimporting_same_plan_does_not_reset_retry_or_failure_memory(self) -> None:
        original = task("T1")
        self.start([original])
        self.engine.begin_iteration()
        self.engine.dispatch("T1", agent_instance="luna-1")
        first = self.engine.record_failure("T1", FAILURE)
        signature = first["decision"]["signature"]
        self.engine.import_heavy_plan(
            {"source": "heavy_plan", "plan_id": "HP-2", "tasks": [original]}
        )
        stored = json.loads(
            (self.project / ".orchestration" / "tasks.json").read_text(encoding="utf-8")
        )["tasks"][0]
        self.assertEqual(stored["attempts"], 1)
        self.assertEqual(stored["failure_history"], [signature])

    def test_unresolved_deployment_blocker_prevents_done_until_reconciled(self) -> None:
        self.start([task("T1")])
        self.engine.begin_iteration()
        self.execute_and_pass("T1")
        self.engine.register_auxiliary_agent(
            role="end_of_session", agent_instance="eos-blocked"
        )
        self.engine.close(
            closure_state="blocked",
            end_of_session_status="PASS",
            reason="external approval",
        )
        unresolved = self.engine.reconcile({"verification": {"T1": "PASS"}})
        self.assertFalse(unresolved["closure_ready"])
        self.engine.register_auxiliary_agent(
            role="end_of_session", agent_instance="eos-retry"
        )
        with self.assertRaises(ValidationError):
            self.engine.close(
                closure_state="complete", end_of_session_status="PASS"
            )
        resolved = self.engine.reconcile(
            {
                "verification": {"T1": "PASS"},
                "deployment_blocker_resolved": True,
            }
        )
        self.assertTrue(resolved["closure_ready"])
        closed = self.engine.close(
            closure_state="complete", end_of_session_status="PASS"
        )
        self.assertEqual(closed["phase"], "DONE")

    def test_cli_initializes_imports_and_schedules_the_heavy_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            plan = project / "plan.json"
            plan.write_text(
                json.dumps(
                    {"source": "heavy_plan", "plan_id": "cli", "tasks": [task("T1")]}
                ),
                encoding="utf-8",
            )
            base = [
                sys.executable,
                "-B",
                str(PACKAGE / "workflow.py"),
                "orchestrate",
            ]
            commands = [
                [*base, "init", "--project", str(project), "--json"],
                [
                    *base,
                    "start",
                    "--project",
                    str(project),
                    "--deployment-id",
                    "cli_test",
                    "--json",
                ],
                [
                    *base,
                    "import-plan",
                    "--project",
                    str(project),
                    "--payload",
                    str(plan),
                    "--json",
                ],
                [*base, "begin-iteration", "--project", str(project), "--json"],
            ]
            result = None
            for command in commands:
                completed = subprocess.run(
                    command, cwd=ROOT, capture_output=True, text=True, check=False
                )
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
                result = json.loads(completed.stdout)
            assert result is not None
            self.assertEqual(result["schedule"]["dispatchable"][0]["task_id"], "T1")


if __name__ == "__main__":
    unittest.main()
