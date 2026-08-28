# codex_workflow

A stateful orchestration workflow for Codex that keeps lightweight work direct
while adding bounded multi-agent execution, verification, recovery, and
cross-session handoff for larger tasks.

This repository is a compatible extension of
[`viettran-edgeAI/codex_workflow`](https://github.com/viettran-edgeAI/codex_workflow),
based on version 1.1.3. The internal workflow identifier is intentionally kept
compatible so existing installations can update in place. See
[UPSTREAM.md](UPSTREAM.md) for the reviewed relationship with upstream 1.1.4.

<h3 align="center"><big><big><strong>SIMPLE&emsp;&emsp;───&emsp;&emsp;EASY&emsp;&emsp;───&emsp;&emsp;EFFICIENT</strong></big></big></h3>
<p align="center"><small>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(to use)&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(to install)&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;(token consumption)</small></p>
<hr>

![Workflow illustration](illustration.png)

Built for maximum token efficiency: swarm execution with the main agent as the
knowledge distributor, companion assistants that preserve operational context,
and built-in context and implementation-progress management across sessions.

> ⭐ For lightweight tasks, it won’t overdo things. Light route is default.

Main features:

- Light, Medium, and Heavy routes with an optional AUTO selector;
- a project-local Heavy Plan task DAG with READY scheduling and restart recovery;
- bounded Luna execution, Tester verification, Sol escalation, and failure memory;
- deterministic install, update, configuration, removal, package validation,
  and SHA-256 verification workflows.

## 1. Quick installation ⚙️

Requires a Codex CLI or Codex app version with subagent support and Python 3.11
or newer for deterministic lifecycle operations. The current implementation was
validated with `codex-cli 0.149.1`.

### Open Codex CLI / Codex app from your project directory 

▶️ Send:

```text
Download and extract the latest `codex_workflow-<version>.zip` asset (not GitHub's Source code archive) from https://github.com/yoshitani-dev/codex_workflow/releases. Verify it against `SHA256SUMS`, then read the bundled `codex_workflow/bootstrap.md` and follow it to complete the initial installation.
```
> ⭐ Recommended: use GPT-5.6 Sol `high` for the main session. The default
> bounded executor is GPT-5.6 Luna `max`; Root selection remains session-owned.

🔄 Restart Codex after installation

After this initial installation, the current project is ready to use. Whenever you need to install this workflow for a new project, simply open the codex and send: `codex_workflow --install`

## 2. Workflow usage 

### This workflow has 3 routes and an optional AUTO selector:
- Light route : No subagents, no workflow, minimal context.
- Heavy route : Deploy subagents, full workflow mode.
- Medium route: No subagents, full workflow mode.
- AUTO: Classifies into an existing Light, Medium, or Heavy route. Explicit
  route selection always wins, and unspecified work remains Light.

> Full workflow mode : Activate `explorer companion` and the ability to automatically manage context and processes.

Note that the `medium route` doesn't call subagents; it completes the task itself. It only applies `full workflow mode` to automatically manage context & progress. It's suitable for moderately sized or narrow tasks, where the main agent can do everything itself faster and more efficiently than calling a small number of workers.

### How to use
- Normally, for simple work, general Q&A, you don't need to do anything. `light route` is the default route.

--------------------------------
- When starting or continuing a plan in progress, just tell Codex in the prompt: "

```text
use medium/heavy route. [your task description]".
```
Or continue a task that was already underway in the previous session: 
```text
use medium/heavy route. Continue ongoing work.
```
> Codex stays on the selected route until you change it, so you don’t need to repeat it in every prompt.
---------------
> **⭐ Recommendation:** Assign very large and complex tasks to the `heavy route` to make the most of its capabilities and maximize token usage savings.

Substantive Heavy work now persists the Heavy Plan as a project-local Task DAG
under `.orchestration/`. A READY scheduler allocates only necessary Luna
executors (maximum four), blocks overlapping write scopes, records bounded
failure signatures and escalation, and resumes after session restart. The
existing Tester repair loop, Explorer, and End-of-Session ownership remain in
place.

## Light benchmark

![Light benchmark analysis](light_benchmark/analysis.png)

## 3. More details 

Send these exact commands to Codex from the relevant project directory:

| Command | Purpose |
| --- | --- |
| `codex_workflow --install` | Install workflow in the current project and initialize its documentation framework. |
| `codex_workflow --configure` | Configure the default executor, reasoning effort, and worker limits. |
| `codex_workflow --personal` | Add or update project-specific workflow preferences. |
| `codex_workflow --check-update` | Check for a newer release without installing it. |
| `codex_workflow --update` | Download, verify, and install the latest eligible release. |
| `codex_workflow --disable` / `codex_workflow --enable` | Disable or re-enable the workflow for the current project. |
| `codex_workflow --remove` | Remove the installed workflow after a destructive dry-run and confirmation. |

For the complete command reference, installed-file map, scripted customization
guide, and Heavy-route design, see [workflow_usage.md](workflow_usage.md).

## 4. Development and verification

From the repository root, run:

```powershell
python -m pip install -r requirements-dev.txt
python -m ruff check codex_workflow scripts
python -m mypy
python -B scripts/test_workflow_runtime.py -v
python -B scripts/test_orchestration_runtime.py -v
python -B scripts/test_package_release.py -v
python -B codex_workflow/workflow.py validate --package-root codex_workflow --json
python scripts/package_release.py --version 1.2.2 --output-dir dist
python scripts/package_release.py --verify dist/codex_workflow-1.2.2.zip
```

The package builder rejects generated Python caches and creates a deterministic
ZIP plus `SHA256SUMS`. See [RELEASING.md](RELEASING.md) for the release process.

## License

No reuse license is granted. The upstream source used for this fork did not
include a license grant, so this fork cannot safely apply an open-source license
to the combined work. See [LICENSE](LICENSE) for the rights notice.
