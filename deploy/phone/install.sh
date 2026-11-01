#!/bin/sh
# Install the fixed-action phone API on the host that has the phone plugged in.
# Run as root after installing: android-tools-adb, android-udev, python3.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST=/opt/her-android
TOKEN_FILE=/etc/her-android/token

mkdir -p "$DEST" "$(dirname "$TOKEN_FILE")"
cp "$HERE/android_agent.py" "$DEST/android_agent.py"
cp "$HERE/firewall.sh" "$DEST/firewall.sh"
chmod 0755 "$DEST/firewall.sh" "$DEST/android_agent.py"

if [ ! -s "$TOKEN_FILE" ]; then
  python3 - <<'PY'
import secrets
path = '/etc/her-android/token'
with open(path, 'w', encoding='ascii') as f:
    f.write(secrets.token_urlsafe(32))
PY
  chmod 600 "$TOKEN_FILE"
fi

ALLOW_IP=${HER_ANDROID_ALLOW_IP:-10.0.0.2}
# Render the unit with the space host IP instead of the example placeholder.
sed "s|Environment=HER_ANDROID_ALLOW_IP=.*|Environment=HER_ANDROID_ALLOW_IP=${ALLOW_IP}|" \
  "$HERE/her-android.service" > /etc/systemd/system/her-android.service
systemctl daemon-reload
systemctl enable --now her-android

sleep 2
systemctl --no-pager status her-android | head -12
echo
echo "Token: $TOKEN_FILE (copy this value into the space data/android.token)"
echo "Check: adb devices"
