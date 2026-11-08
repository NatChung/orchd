# Installed run templates

A run template stores a fixed ordered plan in the committed workflow package. The caller supplies context strings. The loader supplies the installed workflow pin. Template resolution reads JSON and never executes plugin code.

## Package declaration

`orchd-workflow.json` accepts an optional `run_templates` map. Names use the same lowercase kebab-case rules as stage names. Each value is a safe repository-relative `.json` file path.

```json
{
  "run_templates": {
    "example": "run-plans/example.template.json"
  }
}
```

This example shows the additional manifest field. Existing required manifest fields and stages remain required. A package can declare up to 32 run templates. Packages without this field keep their existing behavior.

Install reads the declared template from a regular committed Git blob. Its bytes join the existing per-file hashes and cache receipt. Dirty source changes are ignored. Missing files, symlinks, traversal, altered cache bytes, duplicate JSON keys, invalid UTF-8, and unknown template root fields fail validation. Each template is limited to 1 MiB; the existing 8 MiB package limit also applies.

`workflow show` exposes the manifest's template names and file hashes. A template cannot name a script or register an executable hook.

## Template data

A template has schema `orchd-run-template/v1`. Required fields are `schema_version`, `context`, and `steps`. Optional fields are `limits` and `failure_steps`. It has no `workflow` field: embedding its own commit or receipt digest would create a circular pin.

```json
{
  "schema_version": "orchd-run-template/v1",
  "context": {"request": "Default exact string\n"},
  "limits": {"max_attempts": 1, "deadline_seconds": 3600},
  "steps": [
    {
      "id": "prepare",
      "repo": "reminder-service",
      "stage": "prepare",
      "task_type": "ops",
      "model": "sonnet",
      "model_reason": "Prepare the scoped request",
      "instructions": "Only perform the declared work.",
      "done_when": "Report the declared result.",
      "inputs": {"request": {"context": "request"}},
      "outputs": []
    }
  ]
}
```

The installed workflow must declare the matching stage and inputs. `steps`, verification sidecars, repositories, and `failure_steps` are committed data. The caller cannot override them through the context argument.

Installation validates JSON packaging, root fields, string context defaults, and fixed step arrays. The existing run engine remains responsible for complete graph, input reference, model, verification, binding, repository, and runtime validation before task or worktree effects. Successful template resolution is not run authorization or proof that the plan can run on this host.

## Resolution API

```python
plan = workflows.run_template(
    "example-workflow", "example", {"request": "Original UTF-8 request\n"}
)
```

`run_template(workflow, template_name, context)` resolves the installed cache and returns `orchd-run-plan/v1`. It injects `workflow = {name, commit, digest}` from that installation's identity and receipt. It does not read the source checkout or create tasks.

Context overrides may contain a subset of the declared keys. Unspecified keys retain their defaults. Unknown keys and non-string values are rejected. Values preserve Unicode, whitespace, trailing newlines, and literal template or shell syntax. They are data, not code. The returned object is separate from the cached template, so caller mutation does not modify the installation.

Pass the resolved plan to the existing run start boundary with an authenticated request ID. An Orch start interface can resolve the named template internally, avoiding model reconstruction of the graph. The Operator direct-plan boundary remains available. Starting a run freezes its plan and pin; later package installation does not change that run.

## Checks

```sh
python3 -m unittest tests.test_workflow_run_templates tests.test_workflows
```
