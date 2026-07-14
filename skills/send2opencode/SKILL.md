---
name: send2opencode
description: Use when Codex App needs OpenCode Web to execute a bounded repository task and return a correlated, verifiable result to the originating Codex task.
---

# Send to OpenCode

Dispatch one bounded task through the fixed OpenCode Web HTTP service. Bind executable work to a manifest containing its task ID, Codex thread, OpenCode session, Git baseline, allowed files, and acceptance criteria.

The default endpoint is `http://127.0.0.1:4096`. Require OpenCode Web to be started with a fixed loopback endpoint:

```bash
opencode web --hostname 127.0.0.1 --port 4096
```

## Dispatch

Choose exactly one write boundary:

- Pass one or more `--allow-file PATH` values for implementation work. Paths may be files, directories, or glob patterns.
- Pass `--read-only` for review, inspection, or bridge tests.

Run:

```bash
python .codex/skills/send2opencode/scripts/send_message.py send \
  --project-path "$PWD" \
  --allow-file src/example.py \
  --allow-file tests/test_example.py \
  --acceptance "focused tests pass" \
  --acceptance "lint and typecheck pass" \
  --json \
  "Implement the scoped change. Use send2codex to return the result."
```

The default path creates an isolated OpenCode session titled `bridge:<task_id>`. Pass `--session-id` only when intentionally continuing a known session. Use `--no-callback` only for passive notifications; it does not create a task manifest.

Set `OPENCODE_SERVER_URL` only when the service is not on port 4096. Set `OPENCODE_SERVER_USERNAME` and `OPENCODE_SERVER_PASSWORD` only when OpenCode Web authentication is enabled. Never silently fall back to the CLI transport.

## Accept callbacks

Accept completion only when the incoming message is `BRIDGE_RESULT`, its `task_id` matches the dispatched task, and `contract_valid` is true. Treat scope violations, incomplete evidence, unknown task IDs, and unrelated callbacks as unverified.

Independently review the actual diff and rerun required checks before committing. OpenCode must not commit on this path.

## Diagnostics

```bash
python .codex/skills/send2opencode/scripts/send_message.py sessions --project-path "$PWD" --json
python .codex/skills/send2opencode/scripts/send_message.py status --project-path "$PWD" --json
```
