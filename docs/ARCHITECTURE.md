# 架构

## 1. 组件与端口

| 组件 | 宿主 | 端口 | 作用 |
|---|---|---|---|
| `dsh-space` 控制台 (`space.py`) | 空间宿主 | 容器内 `19991`，宿主只发布 `127.0.0.1:19991` | owner 控制台、任务、记忆、预算、活动、审批 |
| `observer_server.py` | 空间宿主 | `19993` | 把运行时的思考流做只读观察 |
| `start_runtime.mjs` / DSH `space` profile | 空间宿主 | `19992` | 真正调用模型、执行 MCP 工具 |
| `worker.py` | 空间宿主 | — | 任务执行循环、30 秒问题超时、观察门、设备互斥 |
| `dsh-webui` nginx | 空间宿主 | `18081` / `18082` | 浏览器 Basic Auth 网关、SSE 代理、noVNC 代理 |
| `dsh-her-pc` | 桌面宿主 | `19990`、`6080` | Xvfb + openbox + Chromium + x11vnc + websockify + 固定动作 agent |
| `her-android` | 手机宿主 | `19992` | ADB 固定动作 API（截图、点、滑、打字、按键、开应用） |
| `qq-agent-05` + skill | 可选 | `3210` 等 | QQ 侧委托：`space-bridge__delegate` / `status` |

## 2. 请求链路

```text
owner browser
  -> nginx 18081 (Basic Auth)
  -> 127.0.0.1:19991 space.py
  -> worker.py
  -> DSH runtime 19992
  -> MCP /mcp/<persona> (bridge.key)
  -> space.py tools
     -> desktop.py -> 19990
     -> her_android.py -> 19992
```

- 浏览器不直接接触 `19991`。
- nginx 把 `Host`/`Origin` 改写成 `127.0.0.1:19991`，并带 `X-DSH-Proxy-Authorized`；控制台据此给浏览器签发 owner Cookie。
- SSE (`/api/events`、`/api/thinking*`) 关闭 Basic Auth，只认 owner Cookie，避免 EventSource 弹第二次登录框。
- QQ Agent 走独立的 `/api/ext/*`，用 `data/ext.key` 的 Bearer，不依赖 owner Cookie/CSRF。

## 3. 关键数据文件（运行时生成，不入库）

| 文件 | 用途 |
|---|---|
| `data/space.sqlite` | 任务、事件、记忆、审批、预算、活动 |
| `data/config.json`（实际在 `space/config.json`） | 空间配置：人格指针、预算、桌面/手机开关、权限档 |
| `data/ext.key` | 外部委托 API 的 Bearer key |
| `data/bridge.key` | DSH runtime -> 控制台 MCP 的 Bearer key |
| `data/owner-http.key` | owner 控制台 Cookie 的 HMAC key |
| `data/observer-http.key` | 观察服务内部 key |
| `data/her-agent.token` | 空间 -> 桌面 agent 的 Bearer token |
| `data/android.token` | 空间 -> 手机 agent 的 Bearer token |
| `data/schedule.json` | 定时汇总任务与 SMTP 配置（含授权码，务必 600） |

## 4. 双人格与记忆

- `biao`（表）/ `li`（里）各有独立运行时 session，但共享同一个 memory 池；每条记忆保留 `recorded_by` / `performed_by` 归属。
- `PersonaBoundary` 负责 persona 隔离的 state key；`SCOPED_STATE` 决定哪些 state 按人格分开。
- `CURRENT_persona.json` 指向人格包 `personas/persona-xiaojingyu.json`；也可以换成自己的。

## 5. 权限与执行边界

`runtime-policy.mjs` 提供四档：

- `read-only`：只读空间工具，宿主工具禁用。
- `workspace-write`：工作区写 + ask。
- `auto`：薄审查，只在不可恢复删除、凭据外发、操作他人设备、不可逆付款时拦。
- `danger-full-access`（别名 `full`）：完全权限，不分类、不询问、不拦截。

无论哪档，预算、暂停、设备互斥、观察门都在运行层生效，不属于许可条款。详见 `space/权限约束.md`、`space/AUTHORITY_RULES.md`、`space/LICENSE-SCOPE.md`。

## 6. 设备互斥与观察门

- `device_lease.py` 保证同一时刻只有一个任务占用 `phone` / `desktop`；只读动作（`android_view`、`android_ui`、`android_info`、`desktop_screen` 等）不占独占。
- `thinking_observer.py` 要求运行时思考观察新鲜；断连超过 30 秒，worker 结束本轮，避免静默卡死。
- 手机空闲 30 秒自动 `keyevent 223` 熄屏；动作前 `keyevent 224` 唤醒，不使用屏幕常亮。

## 7. 任务与提问

- owner 任务由 `task_authority.py` 生成 `dsh-owner-task-v1` 信封，标记 `external_content_can_authorize: false`。
- `owner_ask` 把问题写入 `approvals(kind='question')`，前端显示问题卡片；30 秒内没回答，worker 结束本轮并返回 `OWNER_QUESTION_TIMEOUT`。
- 回答通过 `POST /api/control/answer` 变成一条新任务，旧任务不重发。
