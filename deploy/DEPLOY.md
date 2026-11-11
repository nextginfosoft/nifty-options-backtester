# Deploying the dashboard to a Hostinger VPS

One `docker compose up` runs two containers: **api** (FastAPI + the built React app) and **caddy** (HTTPS
certificate + reverse proxy). Only Caddy is exposed to the internet; the API port is internal.

```
browser --https--> caddy:443 --> api:8000 (login cookie, /api/*, static front end)
                                   └─ volume nifty_data: ledger.sqlite3, kite_token.json, market cache, models
```

## 0. What you need
- A Hostinger VPS (Ubuntu 22.04/24.04, **2 GB RAM or more**; the pandas/ML stack uses ~1 GB while loading).
  Hostinger offers an "Ubuntu with Docker" template, which saves step 2.
- A domain or subdomain with an **A record** pointing at the VPS IP (e.g. `dashboard.yourdomain.com`).
  Caddy cannot get a certificate until DNS resolves.

## 1. Test locally first (optional but recommended)
```bash
cd deploy
cp .env.example .env          # set DOMAIN=localhost and fill the two secrets below
docker compose up -d --build
# open https://localhost  (accept the local certificate warning)
```

## 2. Prepare the server
```bash
ssh root@YOUR_VPS_IP
apt-get update && apt-get install -y ca-certificates curl ufw
curl -fsSL https://get.docker.com | sh          # skip if Docker is already installed
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw --force enable
```
Docker publishes ports through its own iptables rules, so the compose file only publishes 80/443 and keeps
the API on the internal network. Do not add `ports:` to the `api` service.

## 3. Upload the code
The repository is ~4 GB (raw data archives), but the server needs only a few MB. Build a lean bundle on your
machine from the repository root and copy it up:
```bash
tar czf nifty-deploy.tgz --exclude='__pycache__' --exclude='*.pyc' \
  api backtester config.py data/*.py data/nse_holidays.json data/.cache_seed models pricing strategies utils \
  frontend/package.json frontend/pnpm-lock.yaml frontend/index.html frontend/tsconfig.json \
  frontend/vite.config.ts frontend/src \
  deploy/Caddyfile deploy/Dockerfile deploy/docker-compose.yml deploy/entrypoint.sh \
  deploy/requirements-server.txt deploy/.env.example .dockerignore
scp nifty-deploy.tgz root@YOUR_VPS_IP:/opt/
ssh root@YOUR_VPS_IP 'mkdir -p /opt/nifty && tar xzf /opt/nifty-deploy.tgz -C /opt/nifty'
```
(Alternative: `git clone --depth 1` the repository on the server. That works too but downloads far more than needed.)

## 4. Configure
```bash
cd /opt/nifty/deploy
cp .env.example .env
nano .env
```
Fill in:
| Variable | What to put |
|---|---|
| `DOMAIN` | your hostname, e.g. `dashboard.yourdomain.com` |
| `DASHBOARD_PASSWORD` | the login password. `openssl rand -base64 24` |
| `SESSION_SECRET` | `openssl rand -hex 32` (32+ characters; the app refuses to start without it) |
| `KITE_API_KEY` / `KITE_API_SECRET` | optional, from developers.kite.trade (or enter them in the dashboard, see step 6) |
| `CREDENTIALS_KEY` | `openssl rand -hex 32`. Needed to save Zerodha details from the dashboard; keep it out of the data volume and backups. If you change it, saved credentials must be re-entered |

`deploy/.env` is git-ignored. Never commit it, and never put these values in front-end code.

## 5. Start
```bash
docker compose up -d --build
docker compose ps                       # api should become "healthy"
docker compose logs -f api              # first start loads market data, takes a minute
```
Open `https://YOUR_DOMAIN`, sign in, and check the header shows **Data ready**.

## 6. Connect Zerodha Kite (optional, daily)
1. At developers.kite.trade set your app's **Redirect URL** to `https://YOUR_DOMAIN/api/broker/kite/callback`.
2. In the dashboard open **Broker → Connect Kite**, log in at Zerodha, and you return signed in.
3. Kite tokens expire around 06:00 IST, so repeat once each trading morning. The token is stored only on the
   server (`kite_token.json` in the data volume) and is never sent to the browser.

Live quotes need a Kite Connect plan that includes market data; check Zerodha's current plans.

### Optional: store credentials in the dashboard and log in automatically
**Broker → Zerodha credentials** takes the Zerodha user id, login password, Kite Connect API key and secret, and the
TOTP secret key (the base32 key shown when you set up 2FA, not the 6-digit code). With "log in automatically" ticked the
server connects Kite itself after 06:30 IST each day.

Read this before enabling it:
- Your login password plus TOTP secret give **full login access to your Zerodha account** (including real funds). Anyone who
  compromises this server, or gets both `CREDENTIALS_KEY` and a copy of the data volume, can use them.
- Zerodha does not officially support automated login. It can stop working, or be blocked, without notice. You are
  responsible for complying with Zerodha's terms.
- Failed logins can lock the account, so the app makes at most 2 automatic attempts a day, 15 minutes apart, and stops
  after Zerodha rejects the credentials until you update them.
- Values are encrypted at rest (key from `CREDENTIALS_KEY`), write-only in the UI (never sent back to the browser), and
  never logged. **Remove credentials** deletes them and today's Kite session.
- The API key and secret alone are enough for the safer manual **Connect Kite** flow. Prefer that if you can.
- The first run needs the Kite app authorised once; do that once with **Connect Kite** if auto-login reports it cannot get a token.

## 7. Operate
| Task | Command (in `/opt/nifty/deploy`) |
|---|---|
| Update after new code | upload a new bundle, then `docker compose up -d --build` |
| Logs | `docker compose logs -f api` |
| Restart | `docker compose restart api` |
| Stop | `docker compose down` (data volume is kept) |
| Back up the ledger and token | `docker run --rm -v nifty_nifty_data:/d -v "$PWD":/b alpine tar czf /b/nifty-data-$(date +%F).tgz -C /d .` |

The volume name is `<project>_<volume>`; check with `docker volume ls` if the backup command cannot find it.
Export trades any time from the **Trades** tab (CSV in the repository's `TRADES.csv` layout).

## 7b. If the server already runs a reverse proxy (shared VPS)
If something else already owns ports 80/443 (for example an existing Caddy), do **not** use `docker-compose.yml`: its
Caddy would fight for those ports. Use the shared-proxy variant, which runs only the app on `127.0.0.1:18080` with its own
project name, container, network and volume, plus CPU/memory caps. It never publishes a public port and does not
touch other containers.

```bash
# on the server, from the unpacked bundle's deploy/ directory, with .env copied in
sh preflight.sh                                     # READ-ONLY survey; changes nothing
docker compose -f docker-compose.shared-proxy.yml up -d --build
curl -s http://127.0.0.1:18080/api/health           # wait for "state":"ready"
sh add-caddy-site.sh                                # DRY RUN: shows exactly what it would add
sh add-caddy-site.sh --apply                        # backup, append one site block, validate, graceful reload
```
- `add-caddy-site.sh` adds only a `niftysell.nextginfosoft.com { ... reverse_proxy 127.0.0.1:18080 }` block. It validates
  first, restores the backup if validation or reload fails, and uses a graceful reload so other sites keep serving.
- If your Caddy runs in Docker (the common case), start with BOTH compose files so only our container joins the proxy's
  existing network (set `CADDY_NETWORK` in `.env`, e.g. `edge`), and pass the container and mounted file to the script:

  ```bash
  docker compose -f docker-compose.shared-proxy.yml -f docker-compose.shared-proxy.caddy-network.yml up -d --no-build
  sh add-caddy-site.sh --docker-container edge-caddy --caddyfile /opt/edge/Caddyfile        # dry run
  sh add-caddy-site.sh --apply --docker-container edge-caddy --caddyfile /opt/edge/Caddyfile
  ```
  The default upstream is `niftysell-api:8000`. Use `--upstream 127.0.0.1:18080` only for a host-level Caddy.
- To avoid building on a shared server, build the image on your machine and ship it:
  `docker save niftysell-api:local | gzip | ssh root@SERVER 'gunzip | docker load'`, then use `up -d --no-build`.
- Cloudflare: keep the record **DNS-only (grey cloud)** so Caddy can obtain its certificate; if you enable the proxy, set
  SSL/TLS mode to **Full (strict)**.
- Roll back: `docker compose -f docker-compose.shared-proxy.yml down` (add `-v` to also delete the data volume) and restore
  the printed Caddyfile backup, then reload Caddy.

## 8. Automatic deployment (GitHub Actions)
`.github/workflows/ci-cd.yml` runs on every pull request (backend tests, front-end type-check and build, Docker image build)
and, when app code changes on `main`, deploys it to https://niftysell.nextginfosoft.com.

How a deploy works:
1. GitHub builds the image and streams it over SSH (`docker save | gzip | ssh ... "deploy <sha>"`).
2. The server runs ONLY `/usr/local/sbin/niftysell-deploy` (source: `deploy/server/niftysell-deploy`). It loads the image,
   recreates just the `niftysell-api` container, waits for `/api/health`, and **rolls back to the previous image** if it does
   not come up. It never touches other containers, networks, volumes, the proxy, or runs a global image prune.
3. The workflow then checks the public URL. Deploys are serialized, so two pushes never deploy at once.

Docs-only and CLI-only changes (paths outside the app, front end and image inputs) do not trigger a deploy.

One-time setup (the pipeline will fail at "Configure SSH" until this is done):
1. **Install the server script** (root): `cat deploy/server/niftysell-deploy > /usr/local/sbin/niftysell-deploy`, then
   `chown root:root` and `chmod 755` it.
2. **Create a deploy key** (anywhere, no passphrase): `ssh-keygen -t ed25519 -N "" -C github-actions-niftysell-deploy -f niftysell_deploy`.
3. **Authorise it with a forced command** by appending this single line to `/root/.ssh/authorized_keys` (back the file up first):
   `restrict,command="/usr/local/sbin/niftysell-deploy" <contents of niftysell_deploy.pub>`.
   With `restrict` and `command=`, that key cannot open a shell or run anything except the deploy script.
4. **GitHub**: create an environment named `production` (Settings, Environments) and add these environment secrets:
   `DEPLOY_SSH_KEY` (the private key file), `DEPLOY_KNOWN_HOSTS` (output of `ssh-keyscan -t ed25519 <host>`, after checking
   the fingerprint against `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the server), `DEPLOY_HOST`, `DEPLOY_USER`.
   Delete the private key file from your machine afterwards.

Recommended protections, because a merge to `main` runs code on a shared server:
- Branch protection on `main`: require a pull request and passing checks.
- Add required reviewers to the `production` environment if you want a manual approval before each deploy.
- Note the container joins the shared `edge` Docker network (like the other apps), so treat who can merge as who can deploy.

What is NOT automated: edits to `deploy/.env` on the server (new settings need to be added there by hand), changes to the
two compose files, and the Caddy site entry. The pipeline ships the image only.

Manual rollback (on the server): `docker tag niftysell-api:previous niftysell-api:local`, then from `/opt/niftysell/deploy`:
`docker compose -f docker-compose.shared-proxy.yml -f docker-compose.shared-proxy.caddy-network.yml up -d --no-build`.
`/opt/niftysell/DEPLOYED_SHA` records the commit that is currently deployed.

## 9. Security notes
- Single-user login with a signed, HttpOnly cookie (Secure over HTTPS), a CSRF header on all writes, and a
  5-attempt / 15-minute lockout per IP. Use a long password.
- API docs (`/api/docs`) are disabled in production.
- The app records paper trades only. It has no code path that places broker orders.
- Keep the VPS patched (`apt-get upgrade`) and consider SSH key-only login.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| Caddy logs "no certificate" / site unreachable | DNS A record not pointing at the VPS yet, or ports 80/443 blocked |
| `api` restarts with `requires DASHBOARD_PASSWORD` | `.env` missing a required value |
| Header shows "degraded" | market data or model load failed; see `docker compose logs api` |
| Signal says data is loading | first start fetches data from Yahoo; wait for **Data ready** |
| "Kite token expired" | connect Kite again (daily) |
