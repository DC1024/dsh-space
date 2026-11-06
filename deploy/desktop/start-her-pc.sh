#!/bin/sh
set -eu
export DISPLAY=:99
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix

# The GUI session stays unprivileged (uid 1000), while the action agent runs as
# the container user (root in the shipped compose) so her_run can install
# packages or change system settings inside this isolated container.
run_gui() {
    if [ "$(id -u)" = "0" ]; then
        su her -s /bin/sh -c "HOME=/home/her $1" >/dev/null 2>&1 &
    else
        HOME=/home/her sh -c "$1" >/dev/null 2>&1 &
    fi
}

# -ac: root's xdotool/scrot must be able to connect to the X server started here.
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp -ac >/dev/null 2>&1 &
for _ in $(seq 1 30); do
    if xdpyinfo >/dev/null 2>&1; then break; fi
    sleep 0.3
done

run_gui "openbox"
sleep 0.5
if [ -f /home/her/wallpaper.ppm ]; then
    run_gui "feh --bg-fill /home/her/wallpaper.ppm"
else
    xsetroot -solid '#22304a' >/dev/null 2>&1 || true
fi
run_gui "lxpanel"

# VNC only on loopback; the LAN gateway is the authenticated entry point.
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -localhost -quiet >/dev/null 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

exec python3 /opt/her/her_pc_agent.py
