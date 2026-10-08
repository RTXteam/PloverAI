# Deploying PloverAI on AWS EC2

Single-VM deployment guide for the PloverAI lab demo. Target:
one `t3.medium` instance (2 vCPU / 4 GB RAM / ~$30/mo) on Amazon Linux
2023 or Ubuntu 22.04 LTS, fronted by nginx with TLS. The site is
public, with no login. Section 1 lists what protects it instead.

> This file and the templates next to it in `deploy/` are the deploy
> reference. The repository README gives the architecture. This file
> gives the exact commands. As of 2026-09-29 no instance has been
> launched from it.

---

## 1. Architecture

```
internet ──► nginx (port 443, TLS via Let's Encrypt + certbot)
              │  limit_req / limit_conn — per-IP limits, questions stricter
              │  adds X-API-Key (the browser never has it)
              │
              ├─► proxy_pass http://127.0.0.1:8000   FastAPI service
              │     (uvicorn, systemd unit, /api/v1/*)
              │
              └─► static files at /var/www/ploverai/out/
                    (Next.js static export — no Node runtime in prod)
```

Three reasons for this shape:

1. **Single origin** at runtime: the browser hits one domain. No CORS
   to configure. The frontend's `NEXT_PUBLIC_API_BASE=""` resolves
   `/api/v1/*` against the page origin, which nginx proxies to the
   backend.
2. **No Node runtime in production**: the frontend ships as static
   `out/` (`output: "export"` in `next.config.ts`). nginx serves it
   directly. Saves ~400 MB of RAM that `next start` would otherwise
   want.
3. **No login, layered limits instead.** From the outside in:
   - nginx: 10 API requests per minute per address, 2 questions per
     minute, 4 open connections. Floods stop here.
   - nginx adds `X-API-Key` itself, and uvicorn listens on 127.0.0.1
     only, so the API cannot be reached around nginx.
   - The API's public mode (`PLOVERAI_PUBLIC=1`): only gpt-6-luna,
     only ARAX, one question at a time per address, 20 per hour per
     address, 3 at once overall, 300 per day overall. The numbers live
     in the `public:` block of `pipeline/config.yaml`. The run history
     is shared: every visitor sees every run, whoever asked it.
   - OpenRouter: a key for this server only, with a credit limit. This
     cap holds even if everything above fails.

   The daily cap mostly protects ARAX (each question keeps it busy for
   30 to 60 s) and the disk, since money is small at $0.002 per
   question.

An ARAX question streams for up to 600 s (`arax.timeout_s` in
`pipeline/config.yaml`). nginx waits up to 300 s between two reads
(`proxy_read_timeout`), and the service relays ARAX's progress every few
seconds while ARAX works, so the stream stays open.

---

## 2. Prerequisites

Before you launch the EC2 instance, line these up:

- [ ] **AWS account** with permission to launch t3.medium + create an
      EBS-backed instance + create a security group.
- [ ] **Domain name** with DNS you control (e.g. `ploverai.example.com`).
      You'll point an A record at the EC2's public IP.
- [ ] **An OpenRouter API key for this server only**, with a **credit
      limit** set on that key in the OpenRouter dashboard. It is the
      last backstop: everything else only slows abusers down.

---

## 3. Launch the EC2 instance

In the AWS console (or via CLI):

- **AMI**: Amazon Linux 2023 (or Ubuntu 22.04 LTS — instructions below
  cover Ubuntu; AL2023 differs only in `apt` → `dnf`).
- **Type**: `t3.medium` (2 vCPU, 4 GB RAM).
- **Storage**: 30 GB gp3 EBS. ~5 GB for the OS and Node/Python deps,
  ~25 GB for run artifacts (`code/outputs/RUN_*/...`). Snapshot daily.
- **Security group**: inbound TCP 22 (SSH from your IP only), 80 (HTTP
  for certbot challenge), 443 (HTTPS). No other inbound.
- **Key pair**: use one you have access to; you'll SSH in with it.

After it boots, point your domain's A record at the public IPv4 address
and wait for DNS to propagate (verify with `dig ploverai.example.com`).

---

## 4. Initial server setup

SSH in as the default user (`ubuntu` on Ubuntu, `ec2-user` on AL2023).
Everything below assumes Ubuntu 22.04; adjust package commands for
AL2023.

### 4a. System packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
    nginx \
    python3.12 python3.12-venv python3-pip \
    certbot python3-certbot-nginx \
    git \
    rsync
```

### 4b. Node.js for building the frontend

The frontend only needs Node at *build* time. Once `npm run build`
finishes, nginx serves the static `out/` and Node is no longer needed
at runtime. Easiest: install via the official package script.

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node --version    # → v20.x.x
npm --version
```

### 4c. Create the app user

Running services as `root` is unnecessary. Create a dedicated user
that owns the code and the artifact volume.

```bash
sudo useradd --system --create-home --shell /bin/bash --home-dir /var/lib/ploverai ploverai
sudo mkdir -p /var/log/ploverai
sudo chown ploverai:ploverai /var/log/ploverai
```

### 4d. Clone the repo

```bash
sudo -u ploverai -i
cd /var/lib/ploverai
git clone https://github.com/<your-org>/<your-repo>.git app
cd app
```

Everything after this is run as the `ploverai` user inside
`/var/lib/ploverai/app/`.

---

## 5. Backend — FastAPI service

```bash
# from /var/lib/ploverai/app
cd pipeline
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
```

### 5a. Environment file

The FastAPI process reads three env vars: `OPENROUTER_API_KEY` (the
server's own OpenRouter key), `PLOVERAI_API_KEY` (the key nginx adds as
`X-API-Key`) and `PLOVERAI_PUBLIC=1` (public site mode). See
[`pipeline.env.example`](pipeline.env.example).

```bash
cat > /var/lib/ploverai/app/pipeline/.env <<EOF
OPENROUTER_API_KEY=sk-or-v1-...your-server-key...
PLOVERAI_API_KEY=$(openssl rand -hex 32)
PLOVERAI_PUBLIC=1
EOF
chmod 600 /var/lib/ploverai/app/pipeline/.env
```

Keep `PLOVERAI_API_KEY` at hand: the nginx vhost needs it (Step 7b).
The service log says `public site mode ON` at start-up when the mode
is active.

### 5b. systemd unit

Copy [`ploverai-api.service.template`](ploverai-api.service.template)
to `/etc/systemd/system/ploverai-api.service` (as `sudo` from outside
the ploverai shell), then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ploverai-api
sudo systemctl status ploverai-api
```

Verify the API responds:

```bash
curl -H "X-API-Key: $(grep PLOVERAI_API_KEY /var/lib/ploverai/app/pipeline/.env | cut -d= -f2)" \
     http://127.0.0.1:8000/api/v1/info
```

You should get a JSON blob with `service`, `version`, `endpoints`.

---

## 6. Frontend — build the static `out/`

The Next.js app embeds `NEXT_PUBLIC_API_BASE` and
`NEXT_PUBLIC_API_KEY` at build time, where every visitor can read them.
Leave both empty: the API is on the same origin, and nginx adds the key.

```bash
cd /var/lib/ploverai/app/frontend
cat > .env.local <<EOF
NEXT_PUBLIC_API_BASE=
NEXT_PUBLIC_API_KEY=
EOF

npm install
npm run build       # emits frontend/out/
```

Copy the static export into the location nginx will serve from. We use
`/var/www/ploverai/out/` so nginx's default `www-data` group can read it.

```bash
sudo mkdir -p /var/www/ploverai
sudo rsync -a --delete /var/lib/ploverai/app/frontend/out/ /var/www/ploverai/out/
sudo chown -R www-data:www-data /var/www/ploverai
```

---

## 7. nginx — TLS, limits, reverse proxy

### 7a. Login (optional)

The public site has none. To close it to a lab again, create
`/etc/nginx/.auth/ploverai` with `htpasswd` (package `apache2-utils`)
and uncomment the two `auth_basic` lines in the vhost.

### 7b. nginx vhost

Copy [`nginx.conf.template`](nginx.conf.template) to
`/etc/nginx/sites-available/ploverai`, replacing the three placeholders
(`__DOMAIN__`, `__APP_DIR__`, and `__API_KEY__` with `PLOVERAI_API_KEY`
from Step 5a). The vhost then holds the key, so keep it readable by
root only (`sudo chmod 600`). Then:

```bash
sudo ln -sf /etc/nginx/sites-available/ploverai /etc/nginx/sites-enabled/ploverai
sudo rm -f /etc/nginx/sites-enabled/default      # remove the AWS default page
sudo nginx -t                                    # syntax check
```

### 7c. TLS via certbot

```bash
sudo certbot --nginx -d ploverai.example.com --non-interactive --agree-tos -m you@example.com
```

certbot will modify the vhost to add the TLS block + the auto-renewal
cron. Verify renewal works:

```bash
sudo certbot renew --dry-run
```

### 7d. Start nginx

```bash
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

Open `https://ploverai.example.com` in a browser. The PloverAI UI
loads, the model list shows only gpt-6-luna, and the Runs tab lists
every run on the server (the history is shared by all visitors).

---

## 8. First smoke test

From your laptop:

```bash
# 200 with "public": true; no key needed from outside (nginx adds it)
curl -s https://ploverai.example.com/api/v1/info

# only m8 is listed
curl -s https://ploverai.example.com/api/v1/models

# an expensive model is refused with 403
curl -s -X POST https://ploverai.example.com/api/v1/query \
     -H 'Content-Type: application/json' \
     -d '{"question": "What drugs treat asthma?", "model": "m1"}'
```

Then in the UI, ask one question and confirm the result view works.
A second question sent while the first is running gets a 429 with
"one question at a time".

---

## 9. Updates / re-deploy

Once the initial deploy is up, future updates are:

```bash
sudo -u ploverai -i
cd /var/lib/ploverai/app
git pull
# backend
cd pipeline && source .venv/bin/activate && pip install -r requirements.txt && deactivate
# frontend
cd ../frontend && npm install && npm run build
# copy static into the served dir
sudo rsync -a --delete out/ /var/www/ploverai/out/
sudo chown -R www-data:www-data /var/www/ploverai
# restart backend
sudo systemctl restart ploverai-api
```

[`update.sh`](update.sh) does all of the above as one
command.

---

## 10. Monitoring + logs

```bash
sudo journalctl -u ploverai-api -f          # FastAPI logs
sudo tail -f /var/log/nginx/access.log      # nginx access
sudo tail -f /var/log/nginx/error.log       # nginx errors
ls /var/lib/ploverai/app/pipeline/code/logs/RUN_*/run.log
                                            # per-run pipeline logs
```

### Disk usage

Run artifacts live under `/var/lib/ploverai/app/pipeline/code/outputs/RUN_*/`.
An ARAX run is 0.1 to 30 MB (measured 2026-09-29), most of it ARAX's
raw response. 1,000 runs can take about 10 GB of the 30 GB EBS.
If disk fills up, you can safely `rm -rf` the oldest RUN_* folders;
the UI will gracefully skip missing folders.

```bash
du -sh /var/lib/ploverai/app/pipeline/code/outputs/RUN_* | sort -h | head -20
```

On the public site, keep two weeks of runs. As the `ploverai` user,
`crontab -e` and add this line (every night at 04:00, deletes run
folders older than 14 days; links to them stop working):

```bash
0 4 * * * find /var/lib/ploverai/app/pipeline/code/outputs -maxdepth 1 -name 'RUN_*' -mtime +14 -exec rm -rf {} +
```

---

## 11. Backup

EBS snapshots cover everything (code + artifacts + nginx config +
certs). Set up a daily snapshot via AWS Backup or a cron'd
`aws ec2 create-snapshot`. Retention: 7 daily + 4 weekly is plenty
for a lab demo.

---

## 12. Cost monitoring

Two cost surfaces:

1. **AWS** — `t3.medium` is ~$30/mo; 30 GB gp3 EBS is ~$2.50/mo;
   data egress is negligible at lab-demo traffic. Total ~$35/mo.
2. **OpenRouter** — set the credit limit on the server's key BEFORE
   you point the domain at the world. The public site serves only
   gpt-6-luna, about $0.002 per question (measured 2026-09-29), so
   the daily cap of 300 questions is about $0.60 a day. A limit of
   $20 a month is plenty.
3. **Disk** — a run is 0.1 to 30 MB, about 6 MB on average. At the
   daily cap that is up to about 2 GB a day, so delete old runs on a
   schedule (Section 10).

---

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser shows "Welcome to nginx!" page | Default vhost not removed | `sudo rm /etc/nginx/sites-enabled/default && sudo systemctl reload nginx` |
| 502 Bad Gateway on `/api/v1/*` | uvicorn isn't running | `sudo systemctl status ploverai-api` — check logs |
| 401 "invalid api key" | `__API_KEY__` in the vhost differs from `PLOVERAI_API_KEY` | put the same value in both, `sudo systemctl reload nginx` |
| 429 or 503 with a "try again" message | a public-site limit was hit (the message says which) | expected; the numbers are in the `public:` block of `pipeline/config.yaml` |
| every model is listed | public mode is off | add `PLOVERAI_PUBLIC=1` to `pipeline/.env`, restart `ploverai-api` |
| UI shows stale model dropdown after editing `config.yaml` | uvicorn caches config at boot | `sudo systemctl restart ploverai-api` |
| Frontend changes not reflected | `out/` wasn't re-synced | re-run `npm run build && rsync ... /var/www/ploverai/out/` |
| `certbot renew` fails | Port 80 blocked or vhost edited away | re-run `sudo certbot --nginx -d ...` |

---

## 14. What this deployment intentionally does NOT do

- No autoscaling, no load balancer, no multi-AZ. Single VM. Lab demo.
- No CDN / CloudFront in front. nginx serves directly. Fine for
  lab-scale traffic; revisit if you ever go public.
- No login. Limits per address can be dodged by someone with many
  addresses; the daily cap and the OpenRouter credit limit bound the
  damage. If bots show up, add a bot check such as Cloudflare
  Turnstile, or turn on the optional login (Section 7a).
- No DB. Filesystem is the source of truth. See `pipeline/code/README.md`
  for the artifact layout.

These are deliberate scope choices for a lab-scale, single-instance
demo.
