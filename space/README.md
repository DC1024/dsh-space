# Space 本体

控制台、任务调度、记忆、预算、桌面/手机桥接、DSH 运行时策略和思考观察。

部署步骤见仓库根目录 [README.md](../README.md) 与 [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md)。

| 文件/目录 | 说明 |
|---|---|
| `space.py` | 控制台：HTTP API、owner 鉴权、任务、记忆、预算、活动、审批 |
| `worker.py` | 任务执行循环、观察门、问题超时、设备互斥 |
| `observer_server.py` | 思考观察服务（19993） |
| `start_runtime.mjs` / `prepare_runtime.mjs` | DSH `space` profile 启动与配置 |
| `runtime-policy.mjs` | 模型可见工具白名单与四档权限 |
| `desktop.py` | 桌面 agent 客户端 |
| `her_android.py` | 手机 agent 客户端 |
| `her_tools.py` | 桌面文件/命令工具 |
| `mail_report.py` | 时段汇总邮件 |
| `dist/` | 控制台前端 |
| `bridge/` | 给有权限的桌面 DSH 的 stdio MCP 桥 |
| `integration/` | DSH 客户端/思考流集成 |
| `image-studio/` | 生图工具 |
| `config.example.json` | 复制为 `config.json` |
| `personas/` | 默认人格包（可换成自己的） |

运行时生成的 `config.json`、`data/`、`workdir/`、`exports/` 不在 Git 里。
