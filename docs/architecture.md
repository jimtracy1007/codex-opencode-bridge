# 工作原理

```text
Codex App task
  │
  │ send2opencode / HTTP prompt_async
  ▼
OpenCode Web session
  │
  │ implementation + verification
  ▼
send2codex / Codex App IPC
  │
  ▼
Originating Codex task
```

## 角色边界

Codex 是负责人：定义任务、文件范围、验收条件，接收结果后 review 和提交。

OpenCode 是执行者：只修改获准文件，运行验证，返回结构化证据，不提交代码。

## 为什么使用固定 OpenCode Web

普通 `opencode web` 默认选择随机端口。`send2opencode` 使用固定的 `http://127.0.0.1:4096`，避免每次重新发现桌面端动态端口，也让浏览器、桌面端和 HTTP API 共享同一组 session。

## 会话策略

- 默认：每个任务创建独立 session，防止旧上下文污染。
- 连续修复：显式传入已有 `--session-id`，复用对当前功能的上下文。
- 功能域改变、上下文矛盾或安全敏感：使用新 session。

## Manifest

派发时在目标项目写入：

```text
.opencode/task-bridge/<task_id>.json
```

它固定任务路由和验收边界。OpenCode 回调不得猜测 Codex thread，而必须读取 manifest 中的 `codex_thread_id`。

## 信任模型

桥接提供范围校验和证据传输，不替代代码 review。双方共享同一工作区，因此 Codex 必须区分任务前已有改动、允许变更和意外变更。
