# 桥：小鲸鱼 → 有权限的桌面端 DSH

小鲸鱼（专属空间里的表/里人格）的所有内置工具都被 `prepare_runtime.mjs` 的策略禁用了，
她只剩「空间接口 + Codex 档案检索」。这个桥给她补上一条路：**把一件她办不了的事，
交给一个有完整权限的 DSH 无头实例去办，拿回结论。**

## 她看到的工具

    mcp__owner-bridge__ask_owner_side

参数：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `question` | string（必填，上限 6000 字） | 要办的事。写清目标、已知信息、想要的输出形式。 |
| `timeout_sec` | integer（60–1800，默认 600） | 最多等多久。慢活要显式给大值。 |

返回：`[<耗时>s] <结论与证据>` 的纯文本。建议她把 `timeout_sec` 给到 900–1800。

## 这套东西怎么接起来的

1. `prepare_runtime.mjs` 第 47 行把 `bridge/mcp_server.py` 注册成 stdio MCP 服务器
   （serverName `owner-bridge`，`toolCallTimeoutMs` 1500000）。
   每次运行时会重新生成 `data/runtime-home/profiles/space/cordis.patch.yml`，
   所以这条注册必须留在脚本里，不能只手改生成的 YAML。
2. 她调用时，`mcp_server.py` 起一个**无头 DSH**：
   `node <dsh>/lib/bin.js --profile headless "<她自己 + 任务>"`，
   `DSH_HOME=C:\Users\owner\.dsh`、`DSH_PERMISSION_MODE=danger-full-access`、
   `cwd=C:\dsh-space\DSH专属空间_v01\workdir`。
   同一账号、同一套运行环境、全部权限、不用人确认。
3. 无头实例干完活把最终回答打到 stdout，`mcp_server.py` 原样回给她。

## 排查与观测

- 调用记录（JSONL，一次一行）：`bridge/logs/<YYYYMMDD>.jsonl`
  字段 `at / question / answer / exit / seconds / ok / error`。
- 跨次记忆：`bridge/memory.md` —— 无头实例会在里面追加值得留的结论，
  每次调用都会把它的路径写进提示词，所以「上次查到的事」不会丢。
- 串行锁：`bridge/worker.lock`。同一时刻只跑一个活；第二个请求会排队，
  排队到 `timeout_sec + 120` 秒还拿不到锁就报错让她重发。
  锁超过 1800 秒视为残留，自动清掉。
- 手动测（不经 MCP 协议）：

      C:\Python312\python.exe C:\dsh-space\DSH专属空间_v01\bridge\mcp_server.py ask "看看 19992 是谁在监听"

## 已知边界

- 一次调用 = 一个独立的无头会话，新建 `C:\Users\owner\.dsh\sessions\...`，
  花真实 token（简单任务 25–60 秒）。别拿它做高频小事。
- 她那边是阻塞等待：她的 `timeout_sec` 到点就没答案，但无头实例可能在后台继续跑完。
- 运行时重启（`start_runtime.mjs` 重拉 19992）会把她正在跑的回合打断，
  表现为「本轮连接或接续未完成」。桥本身不受影响，重启后 MCP 客户端会重新拉起
  `mcp_server.py`。

## 变更历史

- 2026-10-23：初版。MCP 协议探针 `initialize` / `tools/list` / `tools/call` 全部通过，
  实测 `tools/call` 返回 `[60.1s] 桥通了`；运行时启动时已实际拉起 `mcp_server.py` 子进程。
