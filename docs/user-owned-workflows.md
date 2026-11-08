# Create your own method and Orch configuration

Use this guide to create repositories such as `example-pstack` and `example-orch` on your own computer. Keep your engineering method in Git and apply a reviewed configuration to a separate local instance. Start an agent with the [reusable creation and maintenance prompt](prompts/create-user-workflow.md).

## Establish the machine and scope

Record the OS, shell, installed orchd commit, provider CLI versions, projects directory, and existing instances. Confirm which repositories and local profiles you may change. Do not assume a colleague's computer has the same OS, credentials, paths, or tools.

Follow the [installation instructions](../README.md#install-and-update) for macOS or the [native Windows instructions](../README.md#native-windows-and-powershell). Check the actual machine with `orchd --help` and `orchd doctor`. The [setup wizard](install-verification.md) lives in the source checkout, not the installed package.

On Windows, use the supported PowerShell installer and update procedure. Keep old installation directories while existing MCP processes use them. POSIX shell commands below need translation to PowerShell. LaunchAgents belong to macOS. Windows Task Scheduler configuration and a real Windows startup test are separate work; this guide does not install either startup mechanism.

## Separate the repositories and runtime state

Create two repositories and a separate runtime directory:

```text
projects/
  example-pstack/       engineering principles, playbooks, tools, and tests
  example-orch/         workflow manifest, stage instructions, and instance recipe
  example-app/          product code and its native checks
agent-instances/
  example/              generated profile, local configuration, and private state
```

Keep `example-pstack` independent of the runtime. If you derive another method, preserve its license and attribution. Record the upstream repository and full source commit in your method repository. Commit your changes there and review them before adoption.

Keep credentials, databases, generated homes, provider sessions, and real channel mappings outside public Git history. Instance recipes contain instructions and project inventory. They do not contain executable plugin hooks or permission overrides.

## Create a committed workflow and instance recipe

Use the [workflow manifest format](workflow-plugins.md#author-a-plugin) in `example-orch`. Start with a scoped investigation stage. Declare the complete request as a required string input, and reference committed Markdown files for instructions and completion criteria.

Add an [instance recipe](instances.md#define-the-recipe) at the same Git root. Write the referenced local instructions and project inventory. Require separate worker tasks for independent review. Keep the engineering playbooks and scripts in the method repository.

The workflow manifest accepts only its documented schema. Keep the method pin in project-owned configuration instead of adding unsupported manifest fields. Commit all referenced files before checking the recipe. The loader reads committed Git blobs and ignores dirty source edits.

## Check and apply the selected commit

Set `SOURCE_SHA` to the reviewed full commit of `example-orch`. Use actual absolute physical paths in place of the examples:

```sh
orchd instance check /path/to/example-orch --commit "$SOURCE_SHA" \
	--root /path/to/agent-instances/example --projects /path/to/projects
orchd instance apply /path/to/example-orch --commit "$SOURCE_SHA" \
	--root /path/to/agent-instances/example --projects /path/to/projects
```

Check the reported changes before apply. The commands do not start an Orch, grant provider trust, configure a channel, or authorize product work. Keep the apply receipt and its source commit. For an existing instance, follow the [drift and receipt checks](instances.md#preserve-edits-and-detect-drift).

Supply the complete profile environment to every caller, including supervisors and MCP launch configuration. Derive `ORCHD_ROOT`, `ORCHD_HOME`, `ORCHD_CONFIG_DIR`, `ORCHD_CONFIG_FILE`, the Orch, interface, and channel homes, `ORCHD_PROJECTS`, and `ORCHD_EXECUTABLE` from that instance. An omitted value can inherit another profile from a shared background process. A different current directory alone does not select a database.

Use the [basic startup procedure](../README.md#basic-use) in that explicit profile after its trust and authentication prerequisites are satisfied. Check the actual MCP process and a harmless request through the intended interface. CLI inventory alone does not prove that an existing MCP process uses the same profile. Do not start or rebind an existing instance merely to inspect it.

## Supply the method to each worker

Record a reviewed full method commit in project-owned configuration. Orchd has no built-in method repository pin field or method-bundle verifier. Define that contract in your own adapter and tests.

Have the host provide the worker with the selected method files, accepted input, and a verifier command through trusted dispatch instructions. Supply an expected manifest digest independently of the bundle. A bundle cannot establish its own trust by declaring itself verified.

Verify the complete resource set, file hashes, safe paths, and intended destination before execution. Make the resources readable inside the worker's actual environment. Do not depend on a global skill cache, another worker's checkout, or a path that only exists on the coordinator's computer. Relocate the bundle in an isolated test and repeat verification.

Record the method files returned by the reader, their hashes, the method commit, task ID, input version, result, and actual check outputs. Keep failed results. Reader records prove returned bytes, not compliance or human acceptance. Independent review, QA, merge, deployment, and acceptance remain separate results.

For automatic sequences, declare [committed run templates](workflow-run-templates.md). Bind context and captured outputs instead of asking the model to reconstruct the graph. Use [ordered-run evidence export](workflow-run-evidence.md) for the actual native results. State exactly which records your auditor supports.

## Keep channel routing and startup separate

Configure channels in the adapter that owns their credentials and transports. See the [channel model](channels.md) and [rollout procedure](channels-rollout.md). A workflow binding selects workflow instructions for dispatch. It does not route Slack, email, or another channel to that instance.

Test each intended route and a route that must stay on the original backend. Use unique probe interfaces, isolated local state, and a local transport capture before authorizing an external send. Verify actual submit, model reply, fetch, acknowledgement, and cleanup. Do not fetch or acknowledge existing production requests as a probe.

Define startup separately for the actual OS. Verify an already running binding is unchanged before testing process recovery. Preserve the same owner and conversation history when resuming a retired session. Native resume can assign new job and session IDs. An explicit stop is distinct from idle retirement. Do not add `--new` to a recurring job or use replacement startup to hide missing state.

A controlled process-recovery test does not prove login or physical reboot behavior. Record each tested lifecycle condition and leave untested conditions pending.

## Maintain the configuration through Git

1. Make method changes in the method repository and workflow changes in the configuration repository. Keep product changes in their own authorized branches.
2. Pin the comparison base and candidate commit. Preserve upstream provenance, accepted inputs, and previous receipts.
3. Reproduce a reported method defect from its original evidence. Add a behavioral check where a reliable check is possible. Record semantic rules as guidance when they cannot be enforced mechanically.
4. Run the repository's native checks and an isolated install, render, and instance-check exercise. Obtain an independent verdict for the exact candidate head.
5. Merge through the repository's review policy. Update the consumer's method pin in a separate reviewed adoption change. Do not edit an active bundle in place.
6. Run instance check against the new configuration commit. Apply through the authorized control plane and inspect its receipt. Follow `restart_required` and [controlled entry updates](instance-entry.md). Existing processes can still use old imported code.
7. Verify the actual runtime profile, one harmless round trip, and retained evidence. Report installed version, loaded process version, review, deployment, and acceptance separately.

Review the installer update policy before changing a pinned orchd installation. An ordinary POSIX `orchd upgrade` can drop a branch pin. An exact pin needs an explicit adoption decision and the appropriate installer procedure. Keep a rollback source and private backups before changing a live installation.
