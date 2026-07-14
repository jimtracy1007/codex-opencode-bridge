# 任务和回调协议

## BRIDGE_TASK

`send2opencode` 生成的任务提示至少包含：

- `TASK_ID`
- `PROJECT_PATH`
- `CODEX_THREAD_ID`
- `OPENCODE_SESSION_ID`
- `BASELINE_HEAD`
- `ALLOWED_FILES` 或 `READ_ONLY`
- `ACCEPTANCE`
- 用户任务正文

`accepted: true` 仅表示 OpenCode HTTP API 接收了提示，不表示任务完成。

## BRIDGE_RESULT

OpenCode 必须用 `result` 子命令返回：

```json
{
  "type": "BRIDGE_RESULT",
  "task_id": "task_example_001",
  "status": "completed",
  "contract_valid": true,
  "project_path": "/absolute/project/path",
  "codex_thread_id": "pinned-thread-id",
  "opencode_session_id": "ses_example",
  "baseline_head": "...",
  "current_head": "...",
  "allowed_files": ["src/example.py"],
  "read_only": false,
  "changed_files": ["src/example.py"],
  "unexpected_files": [],
  "acceptance": ["focused tests pass"],
  "summary": "Implemented the scoped change",
  "verification": ["pytest: passed"],
  "remaining_risks": ["none"]
}
```

## 状态

- `completed`：实现和验证完成。
- `blocked`：缺少权限、凭据或关键输入。
- `failed`：实现或验证失败。
- `scope_violation`：检测到范围外变更。
- `incomplete_evidence`：声称完成但没有验证证据。

## Codex 验收

只有以下条件同时成立时才进入代码验收：

1. `type == BRIDGE_RESULT`
2. `task_id` 与当前派发任务一致
3. `contract_valid == true`
4. `unexpected_files` 为空
5. 验证证据满足 acceptance

随后 Codex 仍需查看实际 diff，并重新运行风险对应的关键测试。
