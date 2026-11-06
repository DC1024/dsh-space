FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11-utils x11-xserver-utils xdotool scrot xclip openbox dbus-x11 lxpanel pcmanfm \
    x11vnc novnc websockify \
    chromium fonts-noto-cjk fonts-wqy-zenhei \
    xterm mousepad evince feh galculator mpv xarchiver unzip p7zip-full \
    python3 ca-certificates procps passwd \
 && rm -rf /var/lib/apt/lists/*

# GUI session runs as an unprivileged her (uid 1000); the action agent runs as
# root so her_run can install packages or change system settings inside this
# isolated container. Xvfb is started with -ac so root xdotool can drive it.
RUN groupadd -g 1000 her \
 && useradd -u 1000 -g her -m -d /home/her -s /bin/bash her \
 && chown -R 1000:1000 /home/her

COPY her_pc_agent.py /opt/her/her_pc_agent.py
COPY start-her-pc.sh /opt/her/start-her-pc.sh
RUN chmod 0755 /opt/her/start-her-pc.sh

EXPOSE 19990
CMD ["/opt/her/start-her-pc.sh"]
