# Orch inbox paging

`inbox(group_id=None, limit=20, message_id=None)` never loses a message.

- At most `limit` unread messages are returned (default 20). Only those are marked read; the rest stay unread.
- Every returned row has `remaining`: how many unread messages are still left (after the `group_id` filter).
  Call `inbox` again until it is 0 or the list is empty.
- Which messages make the page: worker `question`s first (even of closed tasks), then messages of open tasks,
  then old messages of closed tasks (`task_status` is `closed`). The chosen page is returned oldest first.
- A body over 2000 chars is cut and ends with `[truncated, N chars; ...]`. Each row has `message_id`;
  `inbox(message_id=ID)` returns that one full message (read-only, read state unchanged, own tasks only).
- `group_id` works as before and leaves other groups unread. Calls without `limit` still work.
- Deleting old messages is not part of this (see #46).
