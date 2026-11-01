#!/bin/sh
set -eu
export DISPLAY=:99
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix

Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &
for _ in $(seq 1 30); do
    if xdpyinfo >/dev/null 2>&1; then break; fi
    sleep 0.3
done

openbox >/dev/null 2>&1 &
sleep 0.5
if [ -f /home/her/wallpaper.ppm ]; then
    HOME=/home/her feh --bg-fill /home/her/wallpaper.ppm >/dev/null 2>&1 &
else
    xsetroot -solid '#22304a' >/dev/null 2>&1 || true
fi
lxpanel >/dev/null 2>&1 &

# VNC only on loopback; the LAN gateway is the authenticated entry point.
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -localhost -quiet >/dev/null 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

exec python3 /opt/her/her_pc_agent.py
