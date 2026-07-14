---
name: send2codex
description: Use when OpenCode completes, fails, or becomes blocked on a BRIDGE_TASK received from Codex and must return correlated evidence to the originating Codex App task.
---

# Send Result to Codex

Return exactly one manifest-bound result for the current `BRIDGE_TASK`. Do not use a generic notification as a substitute for task completion.

## Execute the task contract

Read these fields from the received task envelope:

- `TASK_ID`
- `BASELINE_HEAD`
- `ALLOWED_FILES` or `READ_ONLY`
- `ACCEPTANCE`

Work only on that task. Ignore older queued instructions. Do not modify files outside the allowed scope, overwrite pre-existing user changes, or commit code.

## Return the result

After real verification, run:

```bash
python .opencode/skills/send2codex/send_message.py result \
  --task-id <TASK_ID> \
  --status completed \
  --summary "Implemented the scoped change" \
  --verification "pytest: passed" \
  --verification "lint and typecheck: passed" \
  --risk "none" \
  --json
```

Use `--status blocked` when authority, credentials, or required information is missing. Use `--status failed` when implementation or verification failed. A completed result without verification is automatically marked `incomplete_evidence`.

The script loads `.opencode/task-bridge/<TASK_ID>.json`, uses its pinned Codex thread, computes real Git changes relative to the recorded baseline, and reports changed files, unexpected files, verification, and remaining risks.

Out-of-scope changes produce `scope_violation`. Unknown, stale, or already-closed task IDs are rejected. The manifest is marked `callback_sent` only after Codex IPC accepts the callback.

## General notifications

The generic `send --target codex` command remains available for messages not associated with a `BRIDGE_TASK`. Never use it for task completion.
