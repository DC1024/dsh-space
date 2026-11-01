#!/bin/sh
# Render nginx.conf from the template.
# Required environment:
#   DSH_PROXY_SECRET   same value as the space container's DSH_PROXY_SECRET
#   HER_DESKTOP_HOST   host/IP of the desktop container (for noVNC on 6080)
set -eu

: "${DSH_PROXY_SECRET:?set DSH_PROXY_SECRET to the same value used by dsh-space}"
: "${HER_DESKTOP_HOST:?set HER_DESKTOP_HOST to the desktop host/IP}"

case "$DSH_PROXY_SECRET" in
  *'|'*) echo 'DSH_PROXY_SECRET must not contain a pipe (|)' >&2; exit 1 ;;
esac

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
sed \
  -e "s|__DSH_PROXY_SECRET__|${DSH_PROXY_SECRET}|g" \
  -e "s|__HER_DESKTOP_HOST__|${HER_DESKTOP_HOST}|g" \
  "$here/nginx.conf.template" > "$here/nginx.conf"

echo "wrote $here/nginx.conf"
