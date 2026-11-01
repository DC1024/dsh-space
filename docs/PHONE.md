# 手机服务（her-android）

一台 Android 真机 + ADB + 一个固定动作 HTTP 服务；没有任意 shell 透传，也不使用屏幕常亮。

## 前置

```bash
sudo apt-get install -y android-tools-adb android-udev python3
adb devices
```

首次连接需要在手机上确认「允许 USB 调试」。设备离线时检查：

```bash
lsusb
adb kill-server && adb start-server
```

## 安装

```bash
cd deploy/phone
sudo HER_ANDROID_ALLOW_IP=<SPACE_HOST_IP> ./install.sh
```

安装内容：

- `/opt/her-android/android_agent.py`
- `/opt/her-android/firewall.sh`
- `/etc/her-android/token`（600）
- `/etc/systemd/system/her-android.service`

服务监听 `19992`，只允许 `HER_ANDROID_ALLOW_IP` 访问，其余 iptables DROP。

把 token 写入空间宿主：

```bash
printf '%s' '<token>' > deploy/space/data/android.token
chmod 600 deploy/space/data/android.token
```

## 中文输入

安装 [ADBKeyboard](https://github.com/senzhk/ADBKeyBoard) 并启用：

```bash
adb install -r ADBKeyboard.apk
adb shell ime enable com.android.adbkeyboard/.AdbIME
```

非 ASCII 文本会：

1. `ime set com.android.adbkeyboard/.AdbIME`
2. `am broadcast -a ADB_INPUT_B64 --es msg <base64>`

ASCII 文本走 `input text`。

## 固定动作 API

| 端点 | 作用 |
|---|---|
| `GET /health` | 型号、Android 版本、SDK、屏幕、电量、亮屏状态 |
| `GET /ui` | `uiautomator dump --compressed` 的界面文字/控件（约 2 秒） |
| `POST /shot` | 截图（PNG） |
| `POST /tap` | 坐标或按文字点按 |
| `POST /swipe` | 滑动 |
| `POST /text` | ASCII/中文输入 |
| `POST /key` | back/home/enter/recents/up/down/left/right/del |
| `GET /apps`、`POST /start` | 列出/启动应用 |

空间侧工具名：`android_view`（截图+健康+文字）、`android_info`（只读健康）、`android_ui`（只读文字）、`android_tap`、`android_swipe`、`android_type`、`android_key`、`android_app`。

## 屏幕与省电

- 动作前 `input keyevent 224` 唤醒 + `wm dismiss-keyguard`。
- 空闲 30 秒 `input keyevent 223` 熄屏。
- 不使用 `svc power stayon` / 屏幕常亮。
- `android_view` 会先唤醒；只看型号/版本用 `android_info` 更快（约 0.4 秒）。

## 性能提示

- `uiautomator dump` 普通模式在动画界面会等 idle 十几秒甚至失败；本服务使用 `--compressed`，约 2.3 秒。
- 3 秒内复用最近一次 UI dump；点按/滑动/按键/开应用后自动失效。
- 截图约 0.5 秒；不要连续截图，优先 `android_info` / `android_ui`。

## 常见问题

- **401**：token 不一致。
- **连接被拒**：防火墙只允许空间宿主 IP；`HER_ANDROID_BIND=0.0.0.0`。
- **`could not get idle state`**：旧版普通 dump 的问题；确认使用 `--compressed`。
- **中文没输入**：ADBKeyboard 未启用，或输入法被切走。
- **屏幕一直亮/不熄**：检查空闲循环；不要设置 `stayon`。
