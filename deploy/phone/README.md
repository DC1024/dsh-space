# 手机服务

```bash
sudo apt-get install -y android-tools-adb android-udev python3
adb devices
sudo HER_ANDROID_ALLOW_IP=<SPACE_HOST> ./install.sh
```

- `android_agent.py`：ADB 固定动作 API（19992）
- `firewall.sh`：iptables 只放行空间宿主
- `her-android.service`：systemd 单元
- `install.sh`：复制文件、生成 token、安装服务

中文输入需 ADBKeyboard；不要使用屏幕常亮。详见 [docs/PHONE.md](../../docs/PHONE.md)。
