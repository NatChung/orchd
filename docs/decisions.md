# Design overview

An Orch decomposes tasks, dispatches workers and checks evidence. A worker executes one task
in an isolated worktree. Work outside the task scope requires an explicit decision.
SQLite stores task state, queued questions, delivery receipts and goal metadata locally.
External writes require a full preview and approval. A worker report is distinct from review,
deployment and acceptance. See [architecture decisions](adr/) for the context boundaries,
multi-Orch groups and Desktop entry design.

Machine-specific accounts, project inventories and private decision history are not part of
this public source distribution.
