# 安全与认证

## 推荐配置

```bash
opencode web --hostname 127.0.0.1 --port 4096
```

只监听 `127.0.0.1` 时，服务不会直接暴露到局域网。不要为了方便改成 `0.0.0.0` 或启用 mDNS。

## Basic Auth

如果必须启用认证：

```bash
export OPENCODE_SERVER_USERNAME=opencode
export OPENCODE_SERVER_PASSWORD='strong-random-password'
opencode web --hostname 127.0.0.1 --port 4096
```

让 Codex App 进程读取同样的环境变量。不要把密码写进 `SKILL.md`、项目配置、Git commit、任务提示或日志。

## 网络访问

如果将 OpenCode 暴露到 `0.0.0.0`：

- 必须设置强密码。
- 使用防火墙限制来源。
- 不要直接暴露到公网。
- 考虑通过 SSH tunnel 或受控反向代理访问。

## 工作区风险

Codex 与 OpenCode 共享同一工作区。`allowed_files` 是契约校验，不是操作系统沙箱。因此：

- OpenCode 不得 commit。
- Codex 必须检查 `unexpected_files` 和真实 diff。
- 对只读任务使用 `--read-only`。
- 不要允许包含密钥、生产配置或用户数据的宽泛目录。

## 回调路由

任务结果必须使用 manifest 固定的 `codex_thread_id`。禁止把“最近活跃 Codex 会话”作为任务完成回调的默认目标，避免跨项目或跨任务误投。
