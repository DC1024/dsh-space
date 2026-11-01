# 桌面服务（dsh-her-pc）

她的桌面是一个隔离的 Linux X11 容器，不是宿主桌面。

## 组成

- `Xvfb :99 -screen 0 1280x800x24`
- `openbox` 窗口管理器
- `lxpanel` 任务栏
- `x11vnc` 只监听 `localhost:5900`
- `websockify` 把 noVNC 暴露到 `6080`
- `her_pc_agent.py` 固定动作 API，监听 `19990`

Chromium 由 agent 的 `/exec` 或桌面操作按需启动；容器内没有宿主目录，工作区在 `/home/her`。

## 固定动作 API

| 端点 | 作用 |
|---|---|
| `POST /` | `{action: "screen" \| "click" \| "type" \| "key" \| "scroll", ...}` |
| `/fs/list`、`/fs/read`、`/fs/write` | 限制在 `/home/her`，单文件 256 KB |
| `/exec` | argv 数组，无 shell 字符串，白名单环境 |
| `/health` | 版本、启动时间、显示尺寸 |

鉴权：`Authorization: Bearer $HER_AGENT_TOKEN`；来源限制：`HER_ALLOWED_IP`（只允许空间宿主）。

## 部署

```bash
cd deploy/desktop
cp .env.example .env
# DESKTOP_HOST / SPACE_HOST / HER_AGENT_TOKEN
mkdir -p home
docker compose -f docker-compose.example.yml up -d --build
```

把 `HER_AGENT_TOKEN` 写到空间宿主的 `deploy/space/data/her-agent.token`（一行，600 权限）。

## 资源

- `mem_limit: 3g`，`cpus: 3`；空闲时约 115 MB / 0.09 核。
- noVNC 的 `6080` 只给网关；不要把 19990/6080 暴露到公网。
- Xvfb + x11vnc + websockify 会常驻；测试用的 xterm/僵尸进程可以手动清理。

## 常见问题

- **401**：token 不一致。
- **403**：`HER_ALLOWED_IP` 不是空间宿主 IP。
- **noVNC 黑屏**：确认 `x11vnc` 在 5900、websockify 在 6080；容器内 `DISPLAY=:99`。
- **点击不准**：坐标是 1280×800 画面坐标，不是浏览器窗口坐标。
