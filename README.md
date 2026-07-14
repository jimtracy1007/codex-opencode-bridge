# Codex ↔ OpenCode Bridge

让 Codex App 把 OpenCode Web 当作一个可验证的外部执行代理：Codex 负责规划、划定文件范围和最终 review，OpenCode 负责实现与测试，再把结构化结果回传到发起任务的 Codex 会话。

这不是一个常驻中间服务器。桥接由两个项目级 Skill 完成：

- `send2opencode`：Codex 通过 OpenCode HTTP API 派发受限任务。
- `send2codex`：OpenCode 读取任务 manifest，通过 Codex App IPC 回传 `BRIDGE_RESULT`。

## 能解决什么

- Codex 把耗时实现交给 OpenCode，同时保留架构与验收权。
- 每个任务固定 `task_id`、Codex thread、OpenCode session 和 Git baseline。
- 用 `allowed_files` 或 `read_only` 限制写入范围。
- 回调自动报告真实 changed/unexpected files、验证证据和剩余风险。
- 默认新建隔离的 OpenCode session；需要连续上下文时可显式复用 session。

## 前置条件

- Codex App，且任务在 Codex App 中运行。
- OpenCode CLI/Web。本文验证版本为 `1.17.18`。
- Python 3.11+；测试额外需要 pytest。
- Codex App 与 OpenCode Web 运行在同一台 macOS/Linux 主机。回调依赖 Codex App 的本地 Unix IPC socket。

## 1. 安装两个 Skill

在目标代码仓库根目录执行：

```bash
git clone https://github.com/jimtracy1007/codex-opencode-bridge.git /tmp/codex-opencode-bridge

mkdir -p .codex/skills .opencode/skills
cp -R /tmp/codex-opencode-bridge/skills/send2opencode .codex/skills/
cp -R /tmp/codex-opencode-bridge/skills/send2codex .opencode/skills/
```

安装后的关键路径：

```text
your-project/
├── .codex/skills/send2opencode/
└── .opencode/skills/send2codex/
```

## 2. 启动固定端口 OpenCode Web

推荐仅监听本机回环地址：

```bash
cd /path/to/your-project
opencode web --hostname 127.0.0.1 --port 4096
```

OpenCode 默认使用随机端口；桥接为了稳定连接必须显式指定 `4096`。只监听 `127.0.0.1` 时可以不设置用户名和密码。

可选 Basic Auth：

```bash
export OPENCODE_SERVER_USERNAME=opencode
export OPENCODE_SERVER_PASSWORD='replace-with-a-strong-password'
opencode web --hostname 127.0.0.1 --port 4096
```

Codex App 进程也必须能够读取相同的环境变量。不要把密码写入 Git 仓库。

## 3. 从 Codex 派发任务

在 Codex App 中要求使用 `send2opencode`，或直接运行：

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

只读 review 使用 `--read-only`，不要与 `--allow-file` 同时使用。

默认会新建标题为 `bridge:<task_id>` 的隔离 OpenCode session。只有在确实需要沿用上下文时才传 `--session-id`。

## 4. OpenCode 回传结果

OpenCode 完成后运行：

```bash
python .opencode/skills/send2codex/send_message.py result \
  --task-id <TASK_ID> \
  --status completed \
  --summary "Implemented the scoped change" \
  --verification "pytest: passed" \
  --verification "ruff: passed" \
  --risk "none" \
  --json
```

脚本从 `.opencode/task-bridge/<TASK_ID>.json` 读取发起任务时固定的 `codex_thread_id`，因此结果会回到正确的 Codex 会话，而不是“最近的会话”。

Codex 收到回调后仍应独立检查 diff 并重跑关键验证。`contract_valid=true` 只证明范围和回调契约成立，不等于代码天然正确。

## 文档

- [工作原理](docs/architecture.md)
- [任务和回调协议](docs/protocol.md)
- [安全与认证](docs/security.md)
- [排障指南](docs/troubleshooting.md)

## 测试

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

OpenCode Web 的端口、hostname 和 Basic Auth 行为以 [OpenCode 官方 Server 文档](https://opencode.ai/docs/server/) 与 [Web 文档](https://opencode.ai/docs/web/) 为准。

## 支持边界

- 推荐 OpenCode Web 固定端口 HTTP，不默认回退到 `opencode run`。
- 推荐项目级安装，确保两个 Skill 共享同一个 `.opencode/task-bridge` manifest。
- 不允许 OpenCode 在桥接任务中自行 commit。
- 不把回调投递到猜测的 Codex 会话；任务结果必须使用 manifest 固定的 thread ID。

## License

MIT
