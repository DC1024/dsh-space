# 去敏说明

这个仓库是实际部署的去敏快照。上传前做了以下处理：

## 1. 已移除

- `data/`：`space.sqlite`、对话/记忆/预算、`ext.key`、`bridge.key`、`owner-http.key`、`observer-http.key`、`her-agent.token`、`android.token`、`schedule.json`、日志
- `config.json` 及所有 `config.json.bak-*`
- `deploy/webui/htpasswd`、渲染后的 `nginx.conf`
- 桌面 `home/`：壁纸、编辑历史、个人文件
- 运行时 `__pycache__`、`node_modules`、`workdir`、`exports`
- 旧 Windows/Hyper-V/访客机部署脚本与 Electron 桌面壳（Linux 部署不需要）

## 2. 已替换为占位符

| 实际内容 | 仓库中的占位 |
|---|---|
| 空间宿主 IP | `SPACE_HOST=192.168.1.10` |
| 桌面/手机宿主 IP | `DESKTOP_HOST=192.168.1.20` |
| 模型网关地址 | `DSH_MODEL_BASE_URL=http://127.0.0.1:3425/v1` |
| nginx 代理签名 | `__DSH_PROXY_SECRET__` |
| 桌面/手机 Bearer token | `HER_AGENT_TOKEN` / `HER_ANDROID_TOKEN_FILE` 占位 |
| QQ Agent 的 ext key | `space-bridge.skill.json` 中 `token` 为空 |
| SMTP 账号/授权码/收件人 | `deploy/space/schedule.example.json` 为空 |
| 手机序列号/群号/QQ 号 | 不出现 |

## 3. 代码级改动

- `desktop.py` 默认 `DSH_DESKTOP_BASE=http://127.0.0.1:19990`
- `her_android.py` 默认 `DSH_ANDROID_BASE=http://127.0.0.1:19992`，token 路径可 `DSH_ANDROID_TOKEN_FILE` 覆盖
- `start_runtime.mjs` 不再读取作者机器上的 Windows 凭据路径，改为 `DSH_CREDENTIALS_FILE` / `$HOME/.dsh` / `data/runtime-home`
- `prepare_runtime.mjs` 去掉作者的 Windows runtime 路径，模型地址改为 `DSH_MODEL_BASE_URL`
- `her_pc_agent.py` 的 `HER_ALLOWED_IP` 默认改为 `127.0.0.1`
- `android_agent.py` 的 token 路径改为 `HER_ANDROID_TOKEN_FILE`（默认 `/etc/her-android/token`）
- `firewall.sh` 的放行 IP 改为 `HER_ANDROID_ALLOW_IP`
- nginx 模板去掉真实 IP 和真实签名
- `dist/index.html` 的 VNC 链接改为示例 IP `192.168.1.10`

## 4. 仍需你在部署时处理

- `space/config.json`：从 `config.example.json` 复制
- `data/` 下所有 token/key：首次启动生成或手动写入
- `deploy/webui/htpasswd`：`htpasswd -c`
- `deploy/webui/nginx.conf`：`render-nginx.sh`
- `.env`：所有 `*.env.example` 复制为 `.env`
- SMTP：在控制台或 `schedule.json` 填自己的邮箱

## 5. 自查

```bash
# 用你原来部署的真实值替换下面的占位，再跑；不要把真实值写进仓库
git grep -nE '<旧 IP 前缀>|<旧 QQ 号>|<旧手机序列号>|<旧 token 前缀>'
```

应无结果。若你发现残留，请立刻删除并重新提交历史（`git filter-repo`），不要只删最新文件。
