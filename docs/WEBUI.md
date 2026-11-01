# WebUI 网关

`deploy/webui` 是一个很小的 nginx 容器，使用 **host network**，把浏览器请求转给空间控制台。

## 端口

| 端口 | 用途 | 认证 |
|---|---|---|
| `18081` | 控制台 | Basic Auth + 控制台 owner Cookie |
| `18082` | noVNC | 无额外认证（只应在局域网开放） |

## 请求头改写

nginx 会把请求改写成控制台预期的「本机同源」身份：

```nginx
proxy_set_header Host 127.0.0.1:19991;
proxy_set_header Origin http://127.0.0.1:19991;
proxy_set_header X-DSH-Proxy-Authorized "<DSH_PROXY_SECRET>";
```

`space/owner_http_auth.py` 只有在 Basic Auth 通过、且这个头和 `DSH_PROXY_SECRET` 一致时，才会给浏览器签发 owner Cookie。这样浏览器不需要知道 `owner-http.key`，也无法直接伪造 owner 请求。

## SSE

以下三个端点关闭 Basic Auth：

- `/api/events`
- `/api/thinking`
- `/api/thinking/stream`

原因：EventSource 不能可靠地携带 Basic Auth 凭据，浏览器会反复弹登录框。它们仍要求 owner Cookie（由 18081 首次页面请求签发），并且只读。`proxy_buffering off` + `proxy_read_timeout 3600s` 保证长连接。

## noVNC

`18082` 直接代理到桌面宿主的 `6080`：

```nginx
proxy_pass http://<HER_DESKTOP_HOST>:6080;
```

websockify 的 WebSocket 升级需要 `Upgrade`/`Connection` 头。VNC 本身在桌面容器里只监听 `localhost:5900`，由 websockify 转成 6080。

## 部署

```bash
cd deploy/webui
cp .env.example .env
# 编辑 DSH_PROXY_SECRET、HER_DESKTOP_HOST
htpasswd -c htpasswd admin
set -a; . ./.env; set +a
./render-nginx.sh
docker compose -f docker-compose.example.yml up -d
```

`render-nginx.sh` 只是 `sed` 替换两个占位符：

- `__DSH_PROXY_SECRET__`
- `__HER_DESKTOP_HOST__`

不要把渲染后的 `nginx.conf` 和 `htpasswd` 提交到 Git（已在 `.gitignore`）。

## 安全建议

- 只在局域网开放 18081/18082；需要公网访问时，前置 VPN 或带 TLS 的反向代理。
- Basic Auth 密码至少 12 位；`htpasswd` 文件权限 600。
- 不要给 `18082` 加公网访问；noVNC 等于远程键鼠。
- 修改 `nginx.conf.template` 后必须重新渲染并重启 `dsh-webui` 容器（文件级 bind mount 替换 inode 后旧进程仍指向旧文件）。
