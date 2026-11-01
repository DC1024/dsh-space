# 部署

下面按「空间宿主 + 桌面宿主 + 手机宿主」拆开写。桌面和手机可以部署在同一台 Linux 宿主，也可以分开；空间宿主建议单独一台。

## 0. 前置条件

- Linux（Debian/Ubuntu 均可），Docker Engine 24+、Docker Compose v2。
- 空间宿主建议 2 核 / 4 GB 内存 / 20 GB 磁盘；桌面宿主建议 4 核 / 8 GB；手机宿主只需要能插 USB 的 Linux。
- 一个 OpenAI 兼容的模型网关（DSH 默认指向本机 `127.0.0.1:3425/v1`，可以用 `DSH_MODEL_BASE_URL` 改）。
- 一个 DeepSeek/网关 API Key（`DEEPSEEK_API_KEY`）。
- 局域网内固定 IP：`SPACE_HOST`、`DESKTOP_HOST`。
- 手机：Android 真机、开启 USB 调试、安装 ADBKeyboard（中文输入需要）。

## 1. 空间宿主

```bash
git clone <your-repo> /opt/dsh-space
cd /opt/dsh-space/deploy/space

cp .env.example .env
# 编辑 .env：
#   DEEPSEEK_API_KEY=...
#   DSH_PROXY_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
#   DESKTOP_HOST=192.168.1.20
#   SPACE_HOST=192.168.1.10

cp ../../space/config.example.json ../../space/config.json
# 编辑 ../../space/config.json：时区、预算、人格、desktop.base_url、permission_preset

mkdir -p data workdir
sudo chown -R 1000:1000 data workdir

docker compose -f docker-compose.example.yml up -d --build
docker logs -f dsh-space
```

首次启动会生成：

- `data/space.sqlite`、`data/bridge.key`、`data/owner-http.key`、`data/observer-http.key`、`data/ext.key`
- 服务日志在 `data/service-*.log`

控制台只发布在 `127.0.0.1:19991`。不要把它改成 `0.0.0.0` 公网映射；浏览器入口走 nginx 网关。

> `prepare_runtime.mjs` 需要 `space/config.json` 存在；没有它启动会失败。不要提交这个文件。

## 2. WebUI 网关（空间宿主）

```bash
cd /opt/dsh-space/deploy/webui
cp .env.example .env
# 编辑 .env：DSH_PROXY_SECRET 与空间 .env 完全一致；HER_DESKTOP_HOST=192.168.1.20

sudo apt-get install -y apache2-utils
htpasswd -c htpasswd admin        # 输入一个强密码
set -a; . ./.env; set +a
./render-nginx.sh

docker compose -f docker-compose.example.yml up -d
```

访问 `http://<SPACE_HOST>:18081`，用 `admin` + 刚才的密码登录。

- 18081：控制台（Basic Auth + owner Cookie）。
- 18082：noVNC（代理到桌面宿主的 `6080`）。
- SSE 三个 location 关闭 Basic Auth，只认 owner Cookie；这是为了 EventSource 不弹二次登录。
- `X-DSH-Proxy-Authorized` 必须等于空间的 `DSH_PROXY_SECRET`；否则控制台不会签发 Cookie。

## 3. 桌面宿主

```bash
cd /opt/dsh-space/deploy/desktop
cp .env.example .env
# 编辑 .env：
#   DESKTOP_HOST=192.168.1.20
#   SPACE_HOST=192.168.1.10
#   HER_AGENT_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')

mkdir -p home
docker compose -f docker-compose.example.yml up -d --build
```

把同一个 token 写到空间宿主：

```bash
printf '%s' '<HER_AGENT_TOKEN>' > /opt/dsh-space/deploy/space/data/her-agent.token
chmod 600 /opt/dsh-space/deploy/space/data/her-agent.token
```

桌面容器会启动 Xvfb、openbox、Chromium、x11vnc、websockify 和 `her_pc_agent.py`：

- `19990`：固定动作 API（screen/click/type/key/scroll、`/fs/*`、`/exec`）。
- `6080`：noVNC（只给网关代理，不要在公网开放）。
- `HER_ALLOWED_IP` 只允许空间宿主访问 `19990`。

## 4. 手机宿主

```bash
sudo apt-get install -y android-tools-adb android-udev python3
# 插上手机，确认：
adb devices          # 应显示 device（首次需要在手机上点“允许 USB 调试”）

cd /opt/dsh-space/deploy/phone
sudo HER_ANDROID_ALLOW_IP=<SPACE_HOST> ./install.sh
```

`install.sh` 会：

- 复制 `android_agent.py`、`firewall.sh` 到 `/opt/her-android`
- 生成 `/etc/her-android/token`（600）
- 安装并启动 `her-android.service`
- 用 iptables 只允许 `HER_ANDROID_ALLOW_IP` 访问 `19992`

把 token 写到空间宿主：

```bash
printf '%s' '<token>' > /opt/dsh-space/deploy/space/data/android.token
chmod 600 /opt/dsh-space/deploy/space/data/android.token
```

中文输入需要 ADBKeyboard：

```bash
adb install -r ADBKeyboard.apk
adb shell ime enable com.android.adbkeyboard/.AdbIME
```

服务不会让屏幕常亮：动作前 `keyevent 224` 唤醒，空闲 30 秒 `keyevent 223` 熄屏。详见 [PHONE.md](PHONE.md)。

## 5. 验证

1. 打开 `http://<SPACE_HOST>:18081`，登录。
2. 设置页确认模型、人格、权限档；首次安装 `owner_hold.paused=true`，点「允许继续」。
3. 在控制台发一条任务：`只读查询：用 android_info 告诉我安卓版本和型号`。
4. 应看到任务从 queued -> running -> completed；活动记录里有 `android_info`。
5. 桌面：`desktop_screen` 能返回截图；VNC 能打开。
6. 手机：`android_view` 能返回截图和界面文字。

## 6. 备份与升级

- 备份 `deploy/space/data/`（DB、密钥、定时任务、token）和 `space/config.json`；不要提交到 Git。
- 升级代码：`git pull`，然后 `docker compose -f docker-compose.example.yml up -d --build`；数据库迁移在启动时自动执行。
- 修改 `nginx.conf.template` 后重新 `./render-nginx.sh` 并 `docker compose -f docker-compose.example.yml up -d`（nginx 容器使用文件级挂载，替换 inode 后需要重启容器）。

## 7. 常见问题

- **控制台 403 / 登录后空白**：`DSH_PROXY_SECRET` 与 nginx 头不一致；或浏览器 Cookie 被清。
- **`prepare_runtime failed`**：`space/config.json` 不存在或 JSON 格式错误；看 `data/service-prepare.log`。
- **桌面 401**：`data/her-agent.token` 与桌面容器的 `HER_AGENT_TOKEN` 不一致。
- **手机 401**：`data/android.token` 与 `/etc/her-android/token` 不一致。
- **手机连不上**：`adb devices` 是否 `device`；防火墙是否允许空间宿主 IP；`HER_ANDROID_BIND` 是否为 `0.0.0.0`。
- **QQ Agent 调不到空间**：QQ 容器是否 host 网络；`baseUrl` 是否为 `http://127.0.0.1:19991`；技能是否授权 `net.outbound`。
