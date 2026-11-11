#!/bin/sh
# READ-ONLY survey of the server before deploying. It changes nothing: no installs, no
# restarts, no config edits. Run it first and read the output:
#   sh preflight.sh
# Optional: APP_PORT=18080 DOMAIN=niftysell.nextginfosoft.com sh preflight.sh
APP_PORT="${APP_PORT:-18080}"
DOMAIN="${DOMAIN:-niftysell.nextginfosoft.com}"

section() { printf '\n== %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

section "Host"
[ -r /etc/os-release ] && . /etc/os-release && echo "OS: ${PRETTY_NAME:-unknown}"
echo "User: $(id -un) (uid $(id -u))"
echo "CPUs: $(nproc 2>/dev/null)"
have free && free -m | awk 'NR==2 {printf "Memory: %s MB total, %s MB available\n", $2, $7}'
df -h / /opt 2>/dev/null | awk 'NR==1 || !seen[$1]++'

section "Docker"
if have docker; then
  docker --version
  docker compose version 2>/dev/null || echo "docker compose plugin: NOT installed"
  docker info --format 'Server: {{.ServerVersion}}, running containers: {{.ContainersRunning}}' 2>&1 | head -2
else
  echo "Docker is NOT installed. Installing it adds firewall/network rules to the host."
  echo "Decide deliberately before installing on a server that runs other apps."
fi

section "Listeners on 80 / 443 (who owns the public ports)"
if have ss; then
  ss -ltnp 2>/dev/null | awk 'NR==1 || /:(80|443)[[:space:]]/'
fi

section "Is our app port free? (127.0.0.1:${APP_PORT})"
if have ss && ss -ltn 2>/dev/null | awk '{print $4}' | grep -q "[:.]${APP_PORT}\$"; then
  echo "PORT ${APP_PORT} IS IN USE. Pick another and set APP_PORT in deploy/.env."
else
  echo "Port ${APP_PORT} is free."
fi

section "Existing containers (names only; nothing is touched)"
if have docker; then
  docker ps --format '{{.Names}}  [{{.Image}}]  {{.Ports}}' 2>/dev/null || echo "(cannot talk to Docker as this user)"
fi

section "Name collisions with this app (all should say 'free')"
chk() { if "$@" >/dev/null 2>&1; then echo "IN USE"; else echo "free"; fi; }
if have docker; then
  printf 'container niftysell-api: '; chk docker container inspect niftysell-api
  printf 'network   niftysell_net: '; chk docker network inspect niftysell_net
  printf 'volume    niftysell_data: '; chk docker volume inspect niftysell_data
  printf 'image     niftysell-api:local: '; chk docker image inspect niftysell-api:local
fi
printf 'directory /opt/niftysell: '; if [ -e /opt/niftysell ]; then echo "EXISTS"; else echo "free"; fi

section "Existing Caddy"
caddy_found=0
if have systemctl && systemctl list-unit-files 2>/dev/null | grep -q '^caddy'; then
  caddy_found=1
  echo "systemd service: $(systemctl is-active caddy 2>/dev/null)"
  for f in /etc/caddy/Caddyfile; do [ -r "$f" ] && echo "Caddyfile: $f" && grep -n '^[[:space:]]*import' "$f" 2>/dev/null | sed 's/^/  import line: /'; done
fi
if have docker; then
  for c in $(docker ps --format '{{.Names}}' 2>/dev/null | grep -i caddy); do
    caddy_found=1
    echo "docker container: $c"
    echo "  networks: $(docker inspect "$c" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null)"
    docker inspect "$c" --format '{{range .Mounts}}  mount: {{.Source}} -> {{.Destination}}{{println}}{{end}}' 2>/dev/null
  done
fi
[ "$caddy_found" = 0 ] && echo "No Caddy found via systemd or Docker names. Identify what serves 80/443 (see listeners above)."
for f in /etc/caddy/Caddyfile; do
  [ -r "$f" ] && { grep -q "$DOMAIN" "$f" && echo "NOTE: $f already mentions $DOMAIN" || echo "$f does not mention $DOMAIN yet"; }
done

section "DNS for ${DOMAIN}"
if have getent; then getent hosts "$DOMAIN" || echo "does not resolve from this server"; fi
echo "Server public IPv4: $(curl -s -m 5 https://api.ipify.org 2>/dev/null || echo 'unknown')"

section "Next step"
echo "1. If Docker is present and the port/names above are free, deploy only our project:"
echo "     docker compose -f docker-compose.shared-proxy.yml up -d --build"
echo "2. Then add ONE site block for ${DOMAIN} to your existing Caddy (see add-caddy-site.sh)."
echo "Nothing above was changed by this script."
