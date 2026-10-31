# Orch inbox paging

`inbox(group_id=None, limit=20, message_id=None, field="body", offset=0, length=20000)` never loses a message.

## A page

- A page holds at most `limit` unread messages (default 20) and at most 60,000 bytes once serialized
  (`INBOX_PAGE_BUDGET`, measured as UTF-8 of the JSON the MCP server sends, every field included).
  Whichever is hit first ends the page. The rest stay unread.
- Which messages make the page: worker `question`s first (even of closed tasks), then messages of open tasks,
  then old messages of closed tasks (`task_status` is `closed`); oldest first inside each group.
  The chosen page is returned oldest first.
- Every row has `remaining` (unread messages left after the `group_id` filter), split into `remaining_priority`
  (questions and open-task messages) and `remaining_closed`. Call `inbox` again until `remaining` is 0 or the list is empty.
- Starvation: closed-task messages are read last. While questions or open-task messages keep arriving, they can wait.
  This is accepted; `remaining_closed` tells you they are still there, and they are read as soon as the
  priority messages are drained.

## Read state (at-least-once)

- Messages are marked read only after the page was fully assembled and serialized, and for the MCP server only
  after the reply was written and flushed to you. If assembly, serialization or the write fails, nothing is marked
  and the same messages come back on the next `inbox` call. A reply that was written but lost on your side
  is not detectable: read those messages again with `inbox(message_id=...)` (read-only).

## Previews and full text

- `body` and `evidence` over 2000 chars, `title` over 200 and `model_reason` over 500 are cut and end with
  `[truncated, N chars; ...]`.
- `inbox(message_id=ID, field=..., offset=..., length=...)` returns `text`, `total` and `next_offset`
  (null at the end) for one field (`body`, `evidence`, `title`, `model_reason`); `length` is at most 20000, so a
  message of any size can be read in slices. Read-only, read state unchanged.
- It only serves the kinds `inbox` itself shows (ack, progress, report, question, adopt, task_question_answer,
  question_dismissed) of tasks owned by the calling Orch. Queued answers, dispatch and other event rows are refused.
- `group_id` works as before and leaves other groups unread. Calls without `limit` still work.
- Deleting old messages is not part of this (see #46).
