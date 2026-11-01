# Space 容器

```bash
cp .env.example .env
cp ../../space/config.example.json ../../space/config.json
mkdir -p data workdir && sudo chown -R 1000:1000 data workdir
docker compose -f docker-compose.example.yml up -d --build
```

- 镜像：`Dockerfile`（Node 24 + Python 3 + 固定版本 `@deepseek-ai/dsh`）
- 代码：`../../space` 挂到 `/opt/dsh-space`
- 数据：`./data`、`./workdir`
- 端口：只发布 `127.0.0.1:19991`
- 入口：`../../space/docker-entrypoint.sh`（observer -> console -> prepare -> runtime）

首次启动后看 `data/service-*.log`，并在 `data/` 里确认 `ext.key`、`bridge.key` 等已生成。
