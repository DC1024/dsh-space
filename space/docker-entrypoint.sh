#!/bin/sh
# Linux entrypoint for the space container: observer -> console -> runtime.
# Replaces start_space.pyw (which relies on Win32 mutex/message box and phones).
set -eu
cd /opt/dsh-space
mkdir -p data workdir
# The runtime RPC helper imports js-yaml; make the runtime's node_modules resolvable.
if [ ! -e node_modules ]; then
    ln -s /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules node_modules 2>/dev/null || true
fi
export PYTHONUNBUFFERED=1
# Thinking archive capacity. The default 1 GiB is too small for this space
# (2.3 GiB of history); the owner already expanded the space quota to 12 GiB.
export DSH_THINKING_MAX_BYTES="${DSH_THINKING_MAX_BYTES:-8589934592}"

wait_port() {
    python3 - "$1" <<'PY'
import socket, sys, time
port = int(sys.argv[1])
deadline = time.time() + 30
while time.time() < deadline:
    try:
        socket.create_connection(('127.0.0.1', port), 1).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.5)
sys.exit(1)
PY
}

python3 observer_server.py >> data/service-observer.log 2>&1 &
OBSERVER=$!
wait_port 19993 || { echo 'observer did not open 19993' >&2; exit 1; }

python3 space.py >> data/service-console.log 2>&1 &
CONSOLE=$!
wait_port 19991 || { echo 'console did not open 19991' >&2; exit 1; }

if ! node prepare_runtime.mjs >> data/service-prepare.log 2>&1; then
    echo 'prepare_runtime failed; see data/service-prepare.log' >&2
    exit 1
fi

node start_runtime.mjs >> data/service-runtime.log 2>&1 &
RUNTIME=$!

# Thinking archive retention: keep observations below the 8 GiB capacity guard.
# The daemon takes its own flock; a duplicate start exits without touching a live run.
python3 thinking_cleanup.py --daemon --keep-hours 6 --interval-hours 6 >> data/observations/cleanup.log 2>&1 &

# Opt-in: clear the start-up pause once both services are ready.
wait_port 19992 || true
if [ "${DSH_AUTO_RESUME:-0}" = "1" ]; then
    python3 auto_resume.py >> data/service-auto-resume.log 2>&1 || true
fi

while kill -0 "$OBSERVER" 2>/dev/null && kill -0 "$CONSOLE" 2>/dev/null && kill -0 "$RUNTIME" 2>/dev/null; do
    sleep 5
done
echo 'one of the space services exited' >&2
exit 1
