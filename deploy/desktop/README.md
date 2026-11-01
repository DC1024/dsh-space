# 桌面容器

```bash
cp .env.example .env
mkdir -p home
docker compose -f docker-compose.example.yml up -d --build
```

- `Dockerfile`：Debian slim + Xvfb + openbox + Chromium + x11vnc + websockify
- `her_pc_agent.py`：固定动作 API（19990）
- `start-her-pc.sh`：启动 X11/桌面/VNC/agent
- 端口：`19990`（API）、`6080`（noVNC）
- 资源：3 GB 内存 / 3 核

把 `HER_AGENT_TOKEN` 写到空间 `deploy/space/data/her-agent.token`。
