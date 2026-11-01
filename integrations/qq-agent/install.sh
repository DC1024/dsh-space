#!/bin/sh
# Install the optional QQ Agent -> space bridge skill into an existing
# QQ Agent container. Adjust QQ_AGENT_CONTAINER if yours has another name.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONTAINER=${QQ_AGENT_CONTAINER:-qq-agent-05}

docker exec "$CONTAINER" mkdir -p /app/skills/space-bridge
docker cp "$HERE/space-bridge.skill.json" "$CONTAINER:/app/skills/space-bridge/skill.json"
docker cp "$HERE/space-bridge.index.js" "$CONTAINER:/app/skills/space-bridge/index.js"
docker exec "$CONTAINER" node --check /app/skills/space-bridge/index.js

cat <<'EOF'
Skill files copied.

Next steps in the QQ Agent console:
1. Open 设置 -> 技能 -> 小鲸鱼空间.
2. Fill in baseUrl (http://127.0.0.1:19991) and the token from the space
   data/ext.key file. Keep persona=li and waitSeconds=120.
3. Save, hot-reload the skill, and grant it the net.outbound permission.
4. Ask the bot to check the phone; it should call space-bridge__delegate.
EOF
