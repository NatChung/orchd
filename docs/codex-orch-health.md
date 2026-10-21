# Codex Orch health (phase 1 of #81)

`orchd orch list --json` includes `health`, `health_reason` and `capabilities` for
all registered Orchs. Health never gates shared interface delivery. Desktop and
exec Orchs remain usable with unknown health; no PID between exec turns is not
proof of death. Registry timestamps and successful queue submissions are not probes.

For a locally managed app-server whose thread is already registered as a Codex
Orch, its launcher or Operator can opt into health verification:

```sh
orchd orch register-codex-probe THREAD_ID --pid SERVER_PID --endpoint /private/directory/app.sock
```

Use the persistent **app-server** PID, never a turn's exec PID. The socket must
belong to the current user in a directory owned by that user with mode 0700.
Registration captures PID, process birth and process group, verifies the socket
and matching `thread/read` response, and saves the probe only after an active or
idle result. It does not start a server, change delivery, migrate bindings or
restart an Orch. An existing probe can be replaced by running the command again
for the same registered Orch after a launcher has replaced the server.

The local integration API is `codex_orch.register(con, orch_id, pid, endpoint)`.
Probe metadata is stored in the nullable `orchs.codex_probe` column. Existing
registrations retain unknown health until explicitly enrolled. Never infer
management from a job ID, thread ID or a Desktop process.

| health | health_reason | Meaning / diagnostic action |
| --- | --- | --- |
| alive | codex_thread_active | Matching process identity and private socket returned this active thread. |
| idle | codex_thread_idle | Same verification, thread is idle and available for another turn. |
| dead | codex_process_gone | Recorded server process is gone, a zombie, or its PID was reused. Does not claim the saved conversation is lost. |
| unknown | codex_probe_unsupported | No managed probe: Desktop/exec or another uninstrumented Orch. Do not infer death. |
| unknown | codex_probe_invalid | Incomplete/invalid registration or malformed probe response; inspect local setup. |
| unknown | codex_process_unavailable | Cannot verify process identity (including permissions or changed process group). |
| unknown | codex_socket_unavailable | Missing/untrusted socket, connection failure, RPC rejection or timeout. A living process alone does not prove a usable Orch. |
| unknown | codex_thread_mismatch | Server returned a different thread; check registration before retrying. |
| unknown | codex_thread_status_unsupported | Thread status is neither active nor idle; do not guess. |

`capabilities.health_probe` is `codex_app_server`, `claude_jobs` or `unsupported`.
The booleans `process_identity`, `private_socket`, `thread_status` describe the
configured Codex probe's evidence sources, not proof that they passed. Diagnose
using `health` and `health_reason` together. JSON inventory never exposes socket
paths, process identities or raw errors. Claude probing retains its existing
reasons and resumable-session behavior.

Phase 2 (safe migration) and phase 3 (Codex restart and Desktop entry) remain
separate work. This phase requires no reinstall of the running service.
