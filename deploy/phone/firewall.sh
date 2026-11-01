#!/bin/sh
# Only the space host may call the phone action API.
# Set HER_ANDROID_ALLOW_IP to the space host's LAN IP before starting the service.
set -u
ALLOW_IP="${HER_ANDROID_ALLOW_IP:-10.0.0.2}"
add_once() {
  if ! iptables -C "$@" 2>/dev/null; then
    iptables -I "$@" >/dev/null 2>&1 || true
  fi
}
iptables -D INPUT -p tcp --dport 19992 -j DROP 2>/dev/null || true
iptables -D INPUT -p tcp --dport 19992 -s "$ALLOW_IP" -j ACCEPT 2>/dev/null || true
add_once INPUT 1 -p tcp --dport 19992 -s "$ALLOW_IP" -j ACCEPT
add_once INPUT 2 -p tcp --dport 19992 -j DROP
iptables -L INPUT -n --line-numbers | head -8
