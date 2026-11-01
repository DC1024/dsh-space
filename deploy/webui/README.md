# nginx 网关

```bash
cp .env.example .env
htpasswd -c htpasswd admin
set -a; . ./.env; set +a
./render-nginx.sh
docker compose -f docker-compose.example.yml up -d
```

- `nginx.conf.template`：18081 控制台 / 18082 noVNC
- `render-nginx.sh`：替换 `__DSH_PROXY_SECRET__`、`__HER_DESKTOP_HOST__`
- 只应在局域网开放；详见 [docs/WEBUI.md](../../docs/WEBUI.md)。
