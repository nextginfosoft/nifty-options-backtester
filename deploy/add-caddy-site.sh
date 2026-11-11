#!/bin/sh
# Adds ONE site block (niftysell.caddy) to your existing Caddy config, safely:
#   * default is a DRY RUN: it only shows what it would do. Nothing changes without --apply.
#   * backs up the Caddyfile, appends the block, validates the result, and only then does a
#     graceful reload. If validation fails the backup is restored and Caddy is not reloaded.
#   * a graceful reload keeps serving existing sites; other apps are not restarted.
#
# Usage:
#   sh add-caddy-site.sh [--apply] [--caddyfile /etc/caddy/Caddyfile] [--docker-container NAME] [--upstream HOST:PORT]
#
# systemd Caddy (default):  validate/reload with the caddy binary.
# Docker Caddy:             pass --docker-container NAME; the Caddyfile path is then the HOST path of the
#                           mounted file (see preflight.sh) and validate/reload run via docker exec.
# Override commands for unusual setups: CADDY_VALIDATE_CMD, CADDY_RELOAD_CMD.
set -u

CADDYFILE="/etc/caddy/Caddyfile"
DOMAIN="niftysell.nextginfosoft.com"
UPSTREAM="127.0.0.1:18080"
CONTAINER=""
APPLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --caddyfile) CADDYFILE="$2"; shift ;;
    --docker-container) CONTAINER="$2"; shift ;;
    --upstream) UPSTREAM="$2"; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -f "$CADDYFILE" ] || { echo "Caddyfile not found: $CADDYFILE" >&2; exit 1; }

if grep -q "^[[:space:]]*${DOMAIN}[[:space:],{]" "$CADDYFILE" || grep -q "^${DOMAIN}\$" "$CADDYFILE"; then
  echo "$DOMAIN is already configured in $CADDYFILE. Nothing to do."
  exit 0
fi

BLOCK="
# --- niftysell dashboard (added by add-caddy-site.sh) ---
${DOMAIN} {
	encode zstd gzip

	header {
		Strict-Transport-Security \"max-age=31536000; includeSubDomains\"
		-Server
	}

	reverse_proxy ${UPSTREAM}
}
# --- end niftysell ---
"

if [ -n "$CONTAINER" ]; then
  VALIDATE_DEFAULT="docker exec $CONTAINER caddy validate --config /etc/caddy/Caddyfile"
  RELOAD_DEFAULT="docker exec $CONTAINER caddy reload --config /etc/caddy/Caddyfile"
else
  VALIDATE_DEFAULT="caddy validate --config $CADDYFILE"
  RELOAD_DEFAULT="caddy reload --config $CADDYFILE"
fi
VALIDATE="${CADDY_VALIDATE_CMD:-$VALIDATE_DEFAULT}"
RELOAD="${CADDY_RELOAD_CMD:-$RELOAD_DEFAULT}"

echo "Caddyfile : $CADDYFILE"
echo "Site      : $DOMAIN -> $UPSTREAM"
echo "Validate  : $VALIDATE"
echo "Reload    : $RELOAD   (graceful; other sites keep serving)"
echo
echo "Block to append:"
printf '%s\n' "$BLOCK"

if [ "$APPLY" != 1 ]; then
  echo "DRY RUN: nothing was changed. Re-run with --apply to do it."
  exit 0
fi

BACKUP="${CADDYFILE}.bak.$(date +%Y%m%d%H%M%S)"
cp -p "$CADDYFILE" "$BACKUP" || { echo "could not create backup" >&2; exit 1; }
printf '%s\n' "$BLOCK" >> "$CADDYFILE"

# For Docker Caddy the validate command reads the file inside the container, which sees the
# same bind-mounted content. If the file is NOT a bind mount this check cannot see the change.
if ! sh -c "$VALIDATE"; then
  echo "Validation FAILED: restoring the original Caddyfile. Caddy was not reloaded." >&2
  cp -p "$BACKUP" "$CADDYFILE"
  exit 1
fi

if ! sh -c "$RELOAD"; then
  echo "Reload FAILED: restoring the original Caddyfile (backup kept at $BACKUP)." >&2
  cp -p "$BACKUP" "$CADDYFILE"
  exit 1
fi

echo "Done. Backup: $BACKUP"
echo "To undo: cp -p $BACKUP $CADDYFILE && $RELOAD"
