# Apply committed instance settings

An instance recipe adds reproducible local instructions and a project inventory to a pinned workflow repository. Applying it generates profile files and selects that workflow for the instance. It does not install Python, create tasks, start or restart processes, configure production routes, or grant provider trust.

## Define the recipe

At the same Git root as `orchd-workflow.json`, commit `orchd-instance.json`:

```json
{
  "schema_version": "orchd-instance/v1",
  "name": "example-instance",
  "workflow": "example-workflow",
  "agents_file": "profile/AGENTS.local.md",
  "projects_file": "profile/PROJECTS.md"
}
```

These are the only accepted fields. The workflow must match the name in the workflow manifest at the same commit. Both documents must be regular committed UTF-8 Markdown files of at most 1 MiB. Paths use the workflow installer's relative-path rules. Documents cannot contain orchd managed or instance markers. Dirty working-tree edits do not change the selected committed files.

The recipe does not supply permission configuration, executables, hooks or arbitrary environment variables. Generated permission configs come from the installed orchd templates. The local instructions do not grant extra runtime permissions or replace the workflow's engineering method.

## Check and apply

Use a trusted local Operator shell and explicit physical paths:

```sh
orchd instance check /path/to/example-workflow --commit FULLSHA \
  --root /path/to/example-instance --projects /path/to/projects
orchd instance apply /path/to/example-workflow --commit FULLSHA \
  --root /path/to/example-instance --projects /path/to/projects
```

Check validates the complete prospective application without writing. It reports the profile files that would change and roles that would require restart. Apply repeats the checks under an exclusive instance lock, installs the workflow and binds this instance's default, then writes the checked profile files. A held lock fails without waiting or stealing it.

The profile uses root as `ORCHD_ROOT`, root/state as `ORCHD_HOME`, root/config as `ORCHD_CONFIG_DIR`, the explicit projects directory and the current orchd executable. Inherited profile overrides do not redirect these operations into the caller's profile. No task database or trust configuration is created. Provider authentication and any later startup remain separate Operator actions.

Root and managed paths cannot contain symlinks or special-file targets. Root cannot contain its source repository or be Git metadata. An instance inside its source checkout, such as `.runtime/`, is permitted only when the entire subtree is Git-ignored and contains no tracked files. Otherwise use a separate physical directory.

## Preserve edits and detect drift

`home/AGENTS.md` receives a distinct instance marker section outside the orchd managed block. Later recipe updates replace only that instance section; the managed block and local notes outside it are preserved byte for byte. `home/PROJECTS.md` is owned as a complete file. Generated skeleton files are protected by receipt hashes. Unknown existing files are not adopted unless they exactly match a recognizable generated template or the desired managed content.

The successful receipt is root/.orchd-instance-receipt.json. It records the physical source repository, source commit and hashes, profile paths, pinned workflow identity and managed target hashes. Manual changes to an owned section or target, missing files, binding changes and conflicting cache content stop application before writes. There is no force override. Inspect and preserve edits or rebuild at a new root rather than silently overwriting them.

Apply returns `changed_files` for profile targets and `restart_required` containing `orch` and/or `interface` when their files changed. Workflow installation identity is recorded separately. A repeated application with unchanged inputs and targets has no profile-file changes. Individual files and the receipt are published atomically; the operation is not a transaction across all files. An interrupted update can require explicit investigation before retrying.

For compare-and-swap application, pass `--expected-receipt sha256:...` using the digest of the current receipt bytes. The comparison runs both before any directory creation and under the instance apply lock. A stale digest refuses the update. `orchd.instances.read_receipt(root)` returns the decoded receipt and its raw-byte digest; `apply(repo, commit, root, projects, check=False, expected_receipt=None)` provides the same operation to an authorized control process.

Installation and apply policy do not prove human identity or OS isolation. The CLI refuses instance commands from identified Orch or worker environments. A worker should change the reviewed Git source and hand off to the authorized control plane rather than remove identity markers or write its own home permissions. See [the controlled interface bridge](instance-entry.md) for that separate entry point. This command does not automatically restart either role or parse conversation transcripts.
