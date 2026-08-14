# Migrate n8n: npm → Docker (zero data loss)

All your data (workflows, executions, credentials, **encryption key**) lives in
one folder: `~/.n8n`. Migration = copy that folder in, fix ownership, start.

Run these on the AWS instance.

## 1. Stop the npm n8n (so the SQLite DB is at rest)

```bash
# whichever you used:
pm2 stop n8n        # if pm2
# or
sudo systemctl stop n8n   # if a systemd service
# or just Ctrl-C the process
```

## 2. Copy the existing data into the project

From this project directory (where docker-compose.yml is):

```bash
cp -a ~/.n8n ./n8n_data
```

`cp -a` preserves the `config` file (your encryption key) and `database.sqlite`.

## 3. Fix ownership — the container runs as uid 1000 (`node`)

```bash
sudo chown -R 1000:1000 ./n8n_data
chmod 600 ./n8n_data/config      # settings-file permission check requires this
```

## 4. Start

```bash
docker compose up -d
docker compose logs -f n8n
```

Open `http://<instance>:5678`. Your workflows and credentials should all be there.
Test-open a credential and run one workflow to confirm the encryption key came across.

## 5. Once verified, disable the old npm service permanently

```bash
pm2 delete n8n && pm2 save     # or: sudo systemctl disable --now n8n
```

## Notes
- **Backups:** the whole `./n8n_data` folder is the backup. `tar czf n8n-backup.tgz n8n_data` anytime the container is stopped.
- **Upgrades:** `docker compose pull && docker compose up -d`. `:latest` pins to whatever's current at pull time — fine for a single instance; pin a version tag (e.g. `n8n:1.x.x`) if you want reproducible deploys.
- **Behind a domain/HTTPS:** put Caddy or nginx in front, then set `N8N_PROTOCOL=https`, `N8N_SECURE_COOKIE=true`, and the real `WEBHOOK_URL`/`N8N_EDITOR_BASE_URL` in `.env`.
