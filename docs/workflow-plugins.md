# Use fixed workflow instructions in dispatch

A workflow plugin supplies Markdown instructions and completion criteria for explicit tasks. Installation reads one exact Git commit and stores a verified copy under this instance's config directory. It does not install Python packages, run hooks, dispatch a worker or grant permissions.

Keep separate project instances in separate `ORCHD_CONFIG_DIR` and `ORCHD_HOME` directories. They may share the same orchd binary. Plugin installation and bindings belong to the selected config directory, not the whole host. This policy does not isolate OS access or authenticate a sender. Channel-to-project routing is outside this interface.

Background Claude Orchs receive the explicitly configured profile paths through their generated MCP environment, including config, root, state, Orch home, interface home and channel home. They do not rely on the daemon inheriting the launching shell's environment.
Initialization writes the same explicit profile paths into the generated Desktop MCP configurations for the Orch home and interface home.

## Author a plugin

At the root of a Git repository, create `orchd-workflow.json`:

```json
{
  "schema_version": "orchd-workflow/v1",
  "name": "example-workflow",
  "description": "Explicit development stages for a reminder service.",
  "stages": {
    "implementation": {
      "task_type": "code",
      "inputs": ["request"],
      "instructions_file": "stages/implementation.md",
      "done_when_file": "stages/implementation-done.md",
      "description": "Implement an accepted request."
    },
    "investigation": {
      "task_type": "investigation",
      "inputs": ["question"],
      "instructions_file": "stages/investigation.md",
      "done_when_file": "stages/investigation-done.md"
    }
  }
}
```

Write the referenced Markdown files and commit them. For example, `stages/implementation.md` can contain `Implement {{request}} within the caller's allowed scope.`, and its completion file can require the product's native checks. Use a distinct investigation or review stage for those tasks. Binding a workflow does not require every task to implement a feature.

The manifest accepts only the fields shown. A stage's description is optional. Names use lowercase kebab-case with at most 64 characters. Inputs are unique lowercase names with letters, digits and underscores. Every input is required and every rendered value must be a string. Referenced templates must be nonempty UTF-8 `.md` files, each at most 1 MiB, with at most 8 MiB across the package. Stages are limited to 128. Paths are relative without `..`, backslashes or symlink components.

Rendering replaces `{{parameter}}` once as text. Replacement values are not evaluated as shell, Python or further templates. Missing, extra and undeclared inputs fail. Credentials and tokens belong outside templates and inputs: rendered text and input metadata are retained in the task record. orchd cannot reliably detect a secret hidden in arbitrary prose.

## Install and inspect a reviewed version

From a trusted local Operator shell:

```sh
orchd workflow install /path/to/example-workflow --commit <full-40-character-sha>
orchd workflow list
orchd workflow show example-workflow
orchd workflow render example-workflow implementation --inputs '{"request":"an opt-in reminder"}'
```

Install reads committed blobs, so dirty working-tree edits cannot change the selected version. A missing commit, unsupported manifest, unsafe path or missing template blocks installation. Only the manifest and referenced Markdown files enter the cache. Other source files are not copied or executed. Review the selected Markdown as instructions before installing it.

The cache is `paths.config_dir()/workflow-plugins/<name>/<commit>/`. `paths.config_dir()` uses `ORCHD_CONFIG_DIR` when set, otherwise `~/.config/orchd`. An install receipt records each file's SHA-256; the registry pins the receipt digest and commit. List, show, render and dispatch check the receipt and exact file set. Missing, extra, modified or symlinked cache files block use. An identical reinstall succeeds; a conflicting cache is preserved and rejected. Explicitly installing a new commit selects it for future tasks. Existing task instructions do not change.

Registry changes use an exclusive lock and atomic replacement. An existing lock causes failure, not waiting or lock stealing. Investigate the owner before an Operator removes a stale lock. Interrupted installs may leave a verified but unselected cache directory, which an identical reinstall can adopt. Digests detect differing bytes, not authorship, malicious local writers or OS sandbox violations.

These local commands do not edit Orch home rules or open the task DB. Installation and binding are not exposed as MCP tools. The CLI refuses them inside an identified Orch or worker environment; that environment check is a guard, not an OS security boundary.

## Bind the instance or one repo

```sh
orchd workflow bind --workflow example-workflow
orchd workflow bind --repo reminder-service --workflow example-workflow
orchd workflow bindings
```

Omitting `--repo` sets this config instance's default workflow. A repo-specific binding overrides that default and requires an existing project repo. Repeating the same binding is harmless. Changing an existing binding requires `--replace`; it never changes a different config instance. Multiple plugins can be installed, while each dispatch uses one effective workflow. There is no automatic host-wide binding and no plugin-defined binding.

For a bound repo, core dispatch rejects missing workflow fields or a different workflow before creating a task or worktree. Unbound instances and repos retain the ordinary dispatch behavior, including optional explicit workflow selection. This first version does not include unbind or remove commands.

## Dispatch an explicit stage

```sh
orchd dispatch --repo reminder-service --title "Implement opt-in reminders" \
  --instructions "Implement the accepted request within this task's product scope." \
  --done-when "The requested behavior and native checks have evidence." \
  --model sonnet --model-reason "Scoped implementation" --task-type code \
  --workflow example-workflow --workflow-stage implementation \
  --workflow-inputs '{"request":"an opt-in reminder"}'
```

The caller still supplies repo, title, goal/scope, completion criteria, task type and model reason. Supply workflow, stage and inputs together, including `{}` for stages with no inputs. Stage task type must match the caller's task type. Worker instructions include the rendered stage, pinned identity, inputs and the caller's goal and scope. Completion criteria include both sources. Worker brief, active runtime permissions and approval rules remain unchanged. Installing or binding never authorizes an external send, push, merge or deployment.

The dispatch message records plugin name, commit, receipt digest, stage and inputs. Retry uses the task's stored rendered instructions, even after another plugin version is installed. It does not re-resolve the current registry. Orch verifies the prior task's `done_when` and explicitly dispatches the next stage. There is no automatic stage scheduler.

## Query from Orch

The read-only MCP tools are `workflow_list()`, `workflow_show(name)` and `workflow_render(name, stage, inputs)`. List includes this instance's default and repo bindings. They verify installed content without reading project repositories, executing a shell or registering the caller as a new Orch. The existing MCP server lifecycle is unchanged. Dispatch and dispatch_batch accept the three workflow fields; each batch item is validated independently before its task is created. Existing partial-success behavior remains.
