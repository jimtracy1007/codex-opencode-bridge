# 排障指南

## Codex 无法连接 OpenCode

确认固定端口服务存在：

```bash
curl -fsS http://127.0.0.1:4096/session
```

如果启用了 Basic Auth：

```bash
curl -fsS -u "$OPENCODE_SERVER_USERNAME:$OPENCODE_SERVER_PASSWORD" \
  http://127.0.0.1:4096/session
```

确认启动命令包含固定端口：

```bash
opencode web --hostname 127.0.0.1 --port 4096
```

## OpenCode 收到任务但 Codex 没收到回调

1. 确认任务使用 `result --task-id ...`，而不是通用 `send`。
2. 确认 `.opencode/task-bridge/<task_id>.json` 存在。
3. 确认运行的是 Codex App，并且本地 IPC socket 存在。
4. 确认 manifest 中有 `codex_thread_id`。
5. 用 `--json` 查看 `callback_status`。

## `scope_violation`

检查返回的 `unexpected_files`。常见原因：

- OpenCode 修改了任务未授权的文件。
- 任务开始前的用户改动没有被正确识别。
- 工具生成了 lock、cache 或报告文件。
- `--allow-file` 范围过窄或 glob 写错。

不要通过扩大到整个仓库来消除提示。先确认每个额外文件是否确实属于任务。

## 复用还是新建 session

- 同一功能的连续修复：复用现有 session。
- 新功能、架构变化、旧上下文已经错误：使用默认隔离 session。
- 只发通知且不需要回调：使用 `--no-callback`。

## 任务回到错误的 Codex 会话

任务完成必须走 manifest-bound `result`。通用通知可以按最近会话发送，但不能用于正式任务结果。删除错误或陈旧 manifest 后重新由正确的 Codex 会话派发任务。
