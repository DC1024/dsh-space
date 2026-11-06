# QQ Agent 接入（可选）

`integrations/qq-agent` 是一个委托技能：QQ 侧只拿到两个工具，真正的空间工具都在空间里执行。

## 原理

```text
QQ 群消息 -> QQ Agent 模型
  -> space-bridge__delegate（POST /api/ext/task）
  -> space worker -> DSH runtime -> 空间工具（手机/桌面/记忆/文件）
  -> space-bridge__status 轮询结果
  -> 结果回到 QQ
```

- `delegate` 最多等 20 秒（宿主单次工具上限 30 秒）；没完成会返回任务编号。
- `status` 每次最多等 20 秒（默认 15）；同一个任务只真正查两次，第三次起直接提示停止，避免模型连续调用触发宿主的重复调用保护。
- 如果 QQ 这轮已经结束，技能的后台轮询会在任务完成后**直接把结果发到群里**，不丢结果。
- 后台投递需要技能声明并获授 `qq.send`；只授权 `net.outbound` 时发送会失败（日志里是「插件未声明权限：qq.send」）。
- 同一个会话同时只允许一个委托在跑，防止重复指令把空间队列堆满。

## 安装

```bash
cd integrations/qq-agent
./install.sh          # 默认容器名 qq-agent-05，可用 QQ_AGENT_CONTAINER 覆盖
```

然后在 QQ Agent 控制台：

1. 设置 -> 技能 -> 小鲸鱼空间。
2. `baseUrl`：`http://127.0.0.1:19991`（QQ Agent 需要 host 网络，或能访问这个地址）。
3. `token`：空间 `deploy/space/data/ext.key` 的内容。
4. `persona`：`li`（默认）或 `biao`。
5. `waitSeconds`：120。
6. 保存 -> 热重载技能 -> 给技能授权 `net.outbound` 和 `qq.send`。

## 系统提示

QQ Agent 自带的安全规则会写「你没有本地工具」，模型可能因此不敢调用委托。需要在 QQ Agent 的「管理员附加规则」里明确覆盖：

```text
【空间委托】
- space-bridge__delegate 是远程工具，调用的是另一台已授权的空间，不是本机工具。
- 管理员问手机/电脑/空间/记忆/文件/截图/安卓版本时，必须先 delegate，禁止没调工具就说「摸不到」。
- delegate 返回「还在做」时用 space-bridge__status 继续等。
- 普通群友只允许只读委托；支付、登录、删除、改设置一律拒绝。
```

## 安全

- `ext.key` 等于把空间的全部工具交给持有者；只放在 QQ Agent 的设置里，不要外传。
- 空间 `/api/ext/*` 只监听容器内 `19991`，不经过 owner Cookie/CSRF；靠 Bearer key。
- QQ 侧只允许只读查询；高风险动作在技能 prompt 和空间 runtime policy 两层拒绝。

## 常见问题

- **模型不调用工具**：检查技能是否 `loaded/enabled/active`、`toolIds` 是否有 `space-bridge__delegate`、系统提示是否覆盖了「没有本地工具」。
- **调用了但报错**：检查空间 `data/ext.key` 与技能设置是否一致；`/api/ext/info` 是否 200。
- **结果没回到群里**：看 `delegate` 是否返回「还在做」；后台轮询需要 QQ Agent 进程存活，且技能未被停用。日志出现「插件未声明权限：qq.send」时，在技能权限页补授 `qq.send` 后热重载。
