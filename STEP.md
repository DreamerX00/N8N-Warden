# STEP.md — Setting up n8n behind CloudKeeper Prism SSO

A start-to-finish deployment guide: from an EC2 box running n8n with nginx on the host, to n8n behind your organisation's SAML single sign-on, with nothing about n8n itself modified.

Follow it top to bottom. Every command is meant to be pasted as-is; every value you have to supply is called out with where to find it.

**Time:** about 30 minutes, most of it waiting for containers.
**Downtime:** roughly 1–2 minutes, during the cutover in [Step 3](#step-3--run-the-installer).
**Reversible:** yes, completely — see [Step 10](#step-10--rollback).

---

## What you are building

```
Browser ──▶ ALB (TLS) ──▶ nginx ──▶ oauth2-proxy ──▶ Keycloak ──▶ Prism (SAML 2.0)
                          (container)                             MFA lives here
                            │
                            └──────▶ n8n  (unmodified, same data, same volume)
```

Four moving parts get added — nginx moves into Docker Compose, and oauth2-proxy, Keycloak and a small Postgres join it. n8n itself is untouched: same image, same volume, no Enterprise licence, no patches.

**Why Keycloak is in the chain.** Prism issues **SAML 2.0** to third-party applications. oauth2-proxy — the component that actually guards each URL — speaks **OIDC only**. Keycloak sits between them purely as a protocol translator: it accepts Prism's SAML assertion and re-issues it as an OIDC token. It stores no passwords; Prism remains the only place anyone types a credential.

> **Know this before you start.** n8n Community Edition has no hook for an external identity, so it keeps its own login *underneath* the SSO gate. Users sign in at Prism, then once more at n8n per browser session. You create those n8n accounts yourself (Step 8). This is a limitation of n8n CE, not of this setup — see [`SSO-SETUP/README.md`](SSO-SETUP/README.md#the-double-login-reality).

---

## Step 0 — Before you begin

Collect these before you start — Steps 2, 3 and 6 each depend on one of them.

| You need | Where it comes from |
|---|---|
| **Root/sudo on the EC2 host** | your usual access |
| **Admin access to the Prism portal** | to create the application in Step 2 |
| **Your public hostname** | the name users type, e.g. `n8n.example.com`. Must already resolve to your ALB. |
| **Ability to edit the ALB target group** | Step 6 |
| **Docker + Compose v2** | already present if n8n runs under Compose |

Check the host is ready:

```bash
docker compose version          # must print v2.x, not docker-compose 1.x
docker ps                       # your n8n container should be listed
systemctl is-active nginx       # 'active' if nginx runs on the host
df -h /                         # need ~4 GB free for the new images
```

If `docker compose version` fails, install the Compose v2 plugin before going further — the installer refuses to run without it.

---

## Step 1 — Take your own backup first

The installer backs up automatically, but take one you control before anything begins. This is the file that saves you if something goes badly wrong.

```bash
# find where n8n keeps its data
docker inspect n8n --format '{{range .Mounts}}{{.Type}} {{.Name}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
```

If it reports a **volume** (e.g. `n8n_data`):

```bash
sudo docker run --rm -v n8n_data:/data:ro -v /root:/backup alpine \
  tar czf /backup/n8n-manual-backup-$(date +%F).tgz -C /data .
```

If it reports a **bind mount** (e.g. `/opt/n8n_data`):

```bash
sudo tar czf /root/n8n-manual-backup-$(date +%F).tgz -C /opt/n8n_data .
```

Verify it is real and contains the encryption key — without `config`, encrypted credentials cannot be recovered:

```bash
tar tzf /root/n8n-manual-backup-*.tgz | grep -E 'config|database.sqlite'
```

You should see both `./config` and `./database.sqlite`. **Copy this file off the box** (S3, your laptop) before continuing.

---

## Step 2 — Create the application in Prism

Prism needs to know about n8n before n8n can send anyone to Prism.

In the Prism admin portal go to **Applications → Custom Applications → Create**, and fill it in using **your** hostname in place of `n8n.example.com`:

| Prism field | Value |
|---|---|
| **Application Name** | `n8n` |
| **Client ID** | `https://n8n.example.com/auth/realms/n8n` |
| **ACS URL** | `https://n8n.example.com/auth/realms/n8n/broker/prism/endpoint` |
| **Name ID Format** | `Persistent` |
| **Single Logout URL** *(optional)* | `https://n8n.example.com/auth/realms/n8n/broker/prism/endpoint` |
| **IDP Initiated SSO Relay State** *(optional)* | `https://n8n.example.com/` |
| Description / Icon | anything you like |

> ⚠️ **Client ID cannot be changed after creation.** Copy it exactly, including `https://` and the trailing `/auth/realms/n8n` with no slash at the end. Getting it wrong means deleting the application and starting over.
>
> The `prism` in the ACS URL is the internal name of the identity provider inside Keycloak. Leave it as `prism` — it is not your tenant name.

Save the application. Prism then shows you three things — **you need the second and third**:

1. IdP Metadata (not needed)
2. **SSO URL** — copy it
3. **X.509 signing certificate** — download it

Put the certificate on the EC2 host:

```bash
scp prism.crt ec2-user@your-host:/root/prism.crt
```

The installer accepts a PEM file (`-----BEGIN CERTIFICATE-----`) or a bare base64 blob; either is fine.

### Assign your users

Still in Prism, grant the users or groups who should reach n8n access to this application. Anyone without it will authenticate at Prism and then be refused at the gate.

---

## Step 3 — Run the installer

One command, on the EC2 host:

```bash
curl -fsSL https://raw.githubusercontent.com/DreamerX00/N8N-Warden/main/SSO-SETUP/prism-saml/install.sh | sudo bash
```

It reads your running deployment and **pre-fills every answer it can work out**. Press <kbd>Enter</kbd> to accept a value, or edit it in place — <kbd>Ctrl</kbd>+<kbd>U</kbd> clears the line first.

### What it asks, in order

| # | Prompt | What to do |
|---|---|---|
| 1 | **Public hostname** | Pre-filled from your running n8n or nginx config. Confirm it is the name users type, not the EC2 private DNS. |
| 2 | **n8n version to run** | Pre-filled with the tag you already run. **Keep it** — SSO does not need an upgrade, and changing two things at once makes failures ambiguous. |
| 3 | **Is an ALB in front of this host?** | `yes` for a normal EC2 + ALB setup. Sets `N8N_PROXY_HOPS=2`. Wrong value ⇒ n8n logs wrong client IPs and builds wrong webhook URLs. |
| 4 | **Host port for nginx** | `80` unless you deliberately run elsewhere. This is where your ALB target group points. |
| — | *It now prints the Prism application values from Step 2* | Cross-check them against what you entered in Prism. If they differ, fix Prism now. |
| 5 | **Prism SSO URL** | Paste the SSO URL from Step 2. |
| 6 | **Prism Single Logout URL** | Paste it, or leave `-` to skip. |
| 7 | **Path to Prism's certificate** | `/root/prism.crt`. It may already be pre-filled if it found a certificate on the host. |
| 8 | **Email domain allowed to reach n8n** | e.g. `example.com`. Only users whose Prism email ends in this get past the gate. Enter `-` to gate on a group instead (see [Step 9](#step-9--hardening)). |
| 9 | **Timezone** | Pre-filled from your container or the host. Drives Cron/Schedule nodes. |

Then it prints a **review block** and asks to proceed. **Read it.** Nothing has changed on the host up to this point — answering anything but `y` exits cleanly.

You do not choose any passwords: the cookie secret, OIDC client secret, Keycloak admin password and database password are all generated and written to `/opt/n8n-sso/.env` (mode `600`).

### What happens after you confirm

```
4/9  Fetching n8n-warden          clones the repo to /opt/n8n-sso/src
5/9  Backing up                   tars /home/node/.n8n and /etc/nginx
6/9  Rendering the stack          writes compose file, nginx conf, realm, .env
7/9  Cutting over                 stops host nginx + old container, starts the stack
8/9  Verifying                    ten checks
9/9  Done                         prints files, commands, next steps
```

Step 7 takes 1–2 minutes: Keycloak has to start and import its realm on first boot. **This is your downtime.**

---

## Step 4 — Read the verification output

The installer ends with a checklist. All ten should be ticked:

```
✔ health endpoint is un-gated
✔ editor is gated (302 → sign-in)
✔ webhook path bypasses the gate
✔ API path bypasses the gate
✔ spoofed identity header is ignored
✔ gate redirects to oauth2-proxy sign-in
✔ OIDC issuer is the public URL
✔ login goes straight to the Prism broker, no Keycloak login page
✔ realm posts its SAML AuthnRequest to <your Prism SSO URL>
✔ existing n8n data present in the new container
```

That last line is the one to read carefully — it lists the files it found in the new container. **`database.sqlite` and `config` must both be there.** If they are not, stop and go to [Step 10](#step-10--rollback).

Anything not ticked: see [Troubleshooting](#troubleshooting) before continuing.

---

## Step 5 — Confirm the containers are up

```bash
cd /opt/n8n-sso
docker compose -p n8n-sso ps
```

Expected — five services, `nginx`, `keycloak`, `keycloak-db` and `n8n` healthy:

```
n8n-sso-nginx         Up (healthy)
n8n-sso-oauth2-proxy  Up
n8n-sso-keycloak      Up (healthy)
n8n-sso-keycloak-db   Up (healthy)
n8n-sso-n8n           Up (healthy)
```

Only nginx publishes a port. n8n, Keycloak and oauth2-proxy are reachable **only** on the private Compose network — verify nothing else is exposed:

```bash
docker compose -p n8n-sso ps --format '{{.Name}}\t{{.Ports}}'
```

Only the nginx line should show a host binding (`0.0.0.0:80->80/tcp`). If `5678` appears anywhere, n8n is internet-facing — stop and fix it before opening the ALB.

---

## Step 6 — Point the ALB at it

Three settings in the AWS console, all of which matter:

1. **Target group → port `80`** (or whatever you chose in prompt 4), protocol HTTP.
2. **Health check path → `/healthz`.** Not `/`. The editor now returns a 302 to Prism, which the ALB reads as unhealthy and takes the target out of service. `/healthz` is deliberately un-gated for exactly this.
3. **Idle timeout → 3600 seconds or more.** The n8n editor holds a WebSocket at `/rest/push`; a short idle timeout kills it and the UI stops updating live.

Then confirm the ALB sees the target as healthy before testing in a browser.

---

## Step 7 — Test the login end to end

Open `https://n8n.example.com/` in a **private/incognito window** — an existing session would hide problems.

Expected sequence:

1. Browser bounces straight to **Prism** (no Keycloak page in between — that is what the browser-flow redirector is for).
2. You authenticate at Prism, with MFA if your policy requires it.
3. You land back on the n8n **login page**. This is expected — see the double-login note at the top.
4. Sign in with an n8n account (Step 8), and the editor loads.

Now check the paths that must **not** be gated, from anywhere:

```bash
# health — must be 200, no redirect
curl -sI https://n8n.example.com/healthz | head -1

# a webhook — must be 404 from n8n, NOT a 302 to Prism
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://n8n.example.com/webhook/does-not-exist

# the editor — must be 302
curl -s -o /dev/null -w '%{http_code}\n' https://n8n.example.com/
```

If a webhook returns `302`, external services (Slack, GitHub, cron) can no longer trigger your workflows — that is a routing fault; check `/opt/n8n-sso/nginx/n8n-sso.conf`.

Finally, test a **real workflow's** production webhook URL with whatever normally calls it.

---

## Step 8 — Create the n8n accounts

The gate controls *who gets in*; n8n still has its own users, and its own owner account. Manage them with `n8n-warden`.

The single-file build ships as a **GitHub release asset**, not as a tracked file — grab the latest:

```bash
cd /opt/n8n-sso
curl -fsSLO https://github.com/DreamerX00/N8N-Warden/releases/latest/download/n8n-warden.pyz
chmod +x n8n-warden.pyz
./n8n-warden.pyz doctor          # read-only: confirms it can see your instance
./n8n-warden.pyz                 # interactive menu
```

`doctor` is the safe first command — it only reads. Prefer to build it yourself from the clone the installer already made? `cd /opt/n8n-sso/src && ./build.sh` produces the same file (stdlib `zipapp`, no dependencies), or run straight from source with `./warden.py doctor`.

Give each person an n8n account whose email **matches their Prism email**. Nothing enforces that link automatically — it is a convention that keeps ownership legible when you audit later.

> Identity mapping between Prism and n8n is manual. That is the one genuine gap versus n8n's paid Enterprise SAML, which provisions users automatically.

---

## Step 9 — Hardening

Work through this before calling it live.

- [ ] **Access is restricted.** Check `email_domains` in `/opt/n8n-sso/oauth2-proxy.cfg` is your domain and not `*`.
- [ ] **Or gate on a Prism group** instead — stricter, and revocation is immediate on next login. Edit the same file:
      ```ini
      allowed_groups   = ["n8n-users"]
      oidc_groups_claim = "groups"
      ```
      then `docker compose -p n8n-sso restart oauth2-proxy`. The realm already maps Prism's SAML `groups` attribute into the OIDC `groups` claim, and re-applies it on **every** login.
- [ ] **`.env` is `600` and backed up somewhere safe.** It holds every secret; losing it while keeping the Keycloak volume is unrecoverable.
- [ ] **Back up the `n8n-sso_keycloak_db` volume** along with your n8n data.
- [ ] **MFA is enforced in Prism**, and n8n users are provisioned/deprovisioned through the Prism group.
- [ ] **Sensitive webhooks carry their own auth** (Header Auth or an HMAC-verification node). Webhook paths bypass the gate by design — they are protected by unguessable IDs and whatever the trigger node enforces, not by Prism.
- [ ] **`/api/*` also bypasses the gate**, authenticating with `X-N8N-API-KEY`. If you have no API callers, delete the `location /api/` block from `/opt/n8n-sso/nginx/n8n-sso.conf` and reload nginx.
- [ ] **Keycloak admin console** at `https://n8n.example.com/auth/admin/` — credentials in `.env`. Consider removing `KC_BOOTSTRAP_ADMIN_*` from the compose file once the realm works.

---

## Step 10 — Rollback

The installer writes `/opt/n8n-sso/ROLLBACK.md` with the exact commands for your host. Nothing was deleted, and your n8n data never moved.

```bash
docker compose -p n8n-sso -f /opt/n8n-sso/docker-compose.yml down
docker start <your-old-n8n-container>      # it was stopped, never removed
sudo systemctl enable --now nginx          # host nginx comes back
```

Change the ALB health check back to `/` if you had changed it. You are then exactly where you started.

To restore data from the installer's backup:

```bash
sudo docker run --rm -v <your-volume>:/data -v /opt/n8n-sso/backups/<timestamp>:/b alpine \
  sh -c 'rm -rf /data/* && tar xzf /b/n8n-data.tgz -C /data'
```

---

## Troubleshooting

Symptoms in the order you are likely to meet them.

### The editor returns 500

The gate is failing closed, which is the correct way to fail — nobody is getting through unauthenticated. It means oauth2-proxy is not running.

```bash
docker compose -p n8n-sso logs --tail=30 oauth2-proxy
```

- `cookie_secret must be 16, 24, or 32 bytes` — the secret in `.env` contains `+` or `/` from a plain `openssl rand -base64 32`. Regenerate with `openssl rand -base64 32 | tr '+/' '-_'`, or let the installer generate it.
- `failed to discover OIDC configuration` — Keycloak is not answering yet. Wait a minute; if it persists, see the next section.

### Keycloak restarts in a loop

```bash
docker compose -p n8n-sso logs --tail=40 keycloak
```

`password authentication failed for user "keycloak"` means the database volume was created with a **different** password than the one now in `.env`. Postgres only applies its password when initialising an empty data directory, so a regenerated secret can never match an existing volume.

- If you still have the original `.env`, restore it — that is the fix.
- If it is gone, the realm has to be rebuilt (n8n data is untouched):
  ```bash
  docker compose -p n8n-sso down
  docker volume rm n8n-sso_keycloak_db
  # re-run the installer; it will re-import the realm
  ```

This is why re-running the installer reuses existing secrets rather than generating new ones.

### Login lands on a Keycloak login form instead of Prism

The realm's browser flow is not redirecting to the broker. Confirm the identity provider exists and is enabled at `https://n8n.example.com/auth/admin/` → realm `n8n` → Identity Providers. The realm ships with the redirector pinned to `prism`; if you re-imported by hand without the custom `browser-prism` flow, set **Authentication → Flows → browser → Identity Provider Redirector → Default Identity Provider = `prism`**.

### Prism rejects the login

Usually a mismatch between what Prism has and what Keycloak sends:

- **Client ID / Entity ID** must match exactly, character for character.
- **ACS URL** must be `https://<host>/auth/realms/n8n/broker/prism/endpoint`.
- **Signature validation failed** — the certificate in the realm is not the one Prism signs with. Re-download it and update Keycloak → Identity Providers → prism → Signing Certificate.

### Signed in at Prism, then "You do not have permission"

The gate authenticated you but the allow-list refused you — almost always because **no email attribute arrived**, so `email_domains` had nothing to match.

The realm ships importers for every realistic spelling of email, so this should be rare. To see exactly what Prism sent, capture the assertion in the browser — this always works and needs no server change:

1. Devtools → **Network**, "Preserve log" on, then log in.
2. Find the `POST` to `/auth/realms/n8n/broker/prism/endpoint`.
3. Copy the `SAMLResponse` form field and decode it:

```bash
printf %s '<paste SAMLResponse>' | base64 -d | xmllint --format - | grep -i 'Attribute Name'
```

Then add the real name as an importer: Keycloak console → realm `n8n` → Identity Providers → `prism` → Mappers → Add, type **Attribute Importer**, attribute name as printed, user attribute `email`. Log in again.

Same procedure if group gating is not working — look for the groups attribute and map it to `prism-groups`. Full detail in [`SSO-SETUP/prism-saml/README.md`](SSO-SETUP/prism-saml/README.md#attribute-mapping--how-it-copes-with-not-knowing-prisms-names).

### Keycloak asks you to complete your profile on first login

Keycloak's first-broker-login prompts only when a required field is **missing** — so this means email, first name or last name did not map. Fill it in to proceed, then fix the mapping with the capture above so later users are not prompted.

### Webhooks stopped firing

Confirm the caller uses the **public** URL (`https://n8n.example.com/webhook/...`) and that it returns something other than a 302. Then check `WEBHOOK_URL` in `/opt/n8n-sso/.env` is your public URL — n8n hands that out when workflows display their webhook addresses.

### The editor loads but does not update live

The `/rest/push` WebSocket is being cut. Raise the **ALB idle timeout** to 3600s or more. Confirm with the browser devtools Network tab, filtered to WS.

### nginx will not start

```bash
docker compose -p n8n-sso logs nginx
```

`host not found in upstream` should not happen — this config resolves upstreams per request precisely so a restarting container cannot block nginx. If you see it, you are running a hand-edited config with `upstream {}` blocks; use the shipped one.

---

## Day-two operations

### Upgrading n8n

n8n is unmodified, so an upgrade is a tag change:

```bash
cd /opt/n8n-sso
sudo sed -i 's/^N8N_TAG=.*/N8N_TAG=2.34.7/' .env
docker compose -p n8n-sso pull n8n && docker compose -p n8n-sso up -d n8n
```

Re-run the checks in Step 7 afterwards, especially a webhook.

### Upgrading the gateway

Watch oauth2-proxy's security releases — its bugs are authentication bypasses. Edit the image tag in `/opt/n8n-sso/docker-compose.yml`, then `docker compose -p n8n-sso up -d oauth2-proxy`.

### When Prism rotates its signing certificate

Update it in the Keycloak console: realm `n8n` → Identity Providers → `prism` → Signing Certificate. Editing `realm-n8n.json` does nothing after the first import — the realm lives in the database from then on.

### Re-running the installer

Safe and idempotent. It recovers every previous answer, reuses the existing secrets, and converges to the same state. Use it after editing `.env` or to repair a half-finished install.

### Logs

```bash
cd /opt/n8n-sso
docker compose -p n8n-sso logs -f nginx          # requests and routing
docker compose -p n8n-sso logs -f oauth2-proxy   # who was allowed or refused
docker compose -p n8n-sso logs -f keycloak       # SAML exchange with Prism
```

---

## What lives where

| Path | What it is |
|---|---|
| `/opt/n8n-sso/.env` | every secret and setting — mode `600`, back it up |
| `/opt/n8n-sso/docker-compose.yml` | the stack |
| `/opt/n8n-sso/nginx/n8n-sso.conf` | **the auth boundary** — which paths are gated and which bypass |
| `/opt/n8n-sso/oauth2-proxy.cfg` | who is allowed in |
| `/opt/n8n-sso/keycloak/realm-n8n.json` | the realm as first imported (later changes live in the DB) |
| `/opt/n8n-sso/backups/<timestamp>/` | pre-install backups of n8n data and `/etc/nginx` |
| `/opt/n8n-sso/ROLLBACK.md` | generated undo instructions |
| `/opt/n8n-sso/src/` | this repository, as cloned by the installer |

---

## Reference

- [`SSO-SETUP/prism-saml/README.md`](SSO-SETUP/prism-saml/README.md) — the Prism variant in depth, including the manual (non-installer) path
- [`SSO-SETUP/README.md`](SSO-SETUP/README.md) — the traffic-class table, the security reasoning behind each bypass, and the gateway comparison
- [`README.md`](README.md) — `n8n-warden` itself, for managing the n8n accounts behind the gate
- [Prism — Custom Applications](https://docs.prism.cloudkeeper.com/admin-portal/custom-applications/)
