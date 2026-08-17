# n8n SSO with CloudKeeper Prism

Prism is a **SAML 2.0 identity provider** when it talks to third-party apps. oauth2-proxy — the auth engine in the [parent example](../README.md) — speaks **OIDC and only OIDC**. This directory adds the one piece that makes them meet: **Keycloak as a SAML↔OIDC broker**.

```
Browser → edge (Caddy | nginx)  →  oauth2-proxy   OIDC relying party
                                →  Keycloak       OIDC provider ⇄ SAML service provider
                                →  Prism          SAML 2.0 IdP — MFA and deprovisioning live here
                                →  n8n            unmodified, no Enterprise licence
```

Keycloak stores **no passwords**. It receives Prism's SAML assertion, mints an OIDC token from it, and forgets. Prism stays the only place anyone ever types a credential.

---

## Just install it

On the host that already runs n8n:

```bash
curl -fsSL https://raw.githubusercontent.com/DreamerX00/N8N-Warden/main/examples/sso/prism-saml/install.sh | sudo bash
```

That one command does the whole job:

1. **Finds your current deployment** — the n8n container, its data volume, the image tag it runs, the hostname and proxy-hop count it was already configured with, and your host nginx.
2. **Backs everything up first** — a full tar of `/home/node/.n8n` (workflows, credentials, encryption key) and of `/etc/nginx`, before touching anything.
3. **Asks for what it cannot know**, with every answer **pre-filled from what it found** — press Enter to accept, or edit in place (Ctrl-U clears the line). It explains what each value is and where in Prism to find it, and prints the exact Prism Custom Application fields *before* asking for Prism's output.
4. **Moves nginx into Compose** and renders the whole stack into `/opt/n8n-sso`, generating every secret for you.
5. **Cuts over** — stops host nginx, stops the old container (never deletes it), starts the stack on your existing n8n volume.
6. **Smoke-tests it** and prints a pass/fail table, plus a `ROLLBACK.md` for undoing all of it.

Re-running is safe and expected: it recovers every previous answer, **reuses the existing secrets** (regenerating them would lock Keycloak out of its own database), and converges to the same state.

```bash
# non-interactive, e.g. from your own automation
curl -fsSL .../install.sh | sudo env ASSUME_YES=1 \
  N8N_DOMAIN=n8n.example.com \
  PRISM_SSO_URL=https://<prism>/sso/saml \
  PRISM_CERT_INPUT=/root/prism.crt \
  ALLOWED_EMAIL_DOMAIN=cloudkeeper.com bash
```

The rest of this page is the manual path, and the reference for what the installer built.

---

## Why a broker at all

From the [Prism admin-portal docs](https://docs.prism.cloudkeeper.com/admin-portal/):

- **Custom Applications** — *"Custom Applications allow you to extend Prism's single sign-on (SSO) capabilities to third-party services that support SAML 2.0."* SAML only; the docs list no OIDC option for apps.
- **Identity Providers** — *"Configure single sign-on (SSO) identity providers to allow users to authenticate to Prism using their existing corporate credentials."* That's Prism as a **relying party** consuming Google / Microsoft / a custom OIDC provider **upstream** — not something a downstream app can point oauth2-proxy at.

So the OIDC that appears in Prism's docs is on the wrong side of Prism. n8n sits downstream, where the only protocol on offer is SAML. Hence Keycloak.

> If Prism later ships OIDC for custom applications, delete this whole directory: set `OIDC_ISSUER_URL` to Prism directly and the parent example works unchanged.

## What you need from Prism first

Create the Custom Application in the Prism admin portal, then keep what it hands back. Per Prism's docs the create form asks for:

| Prism field | What to enter |
|---|---|
| Application Name | `n8n` |
| **Client ID** | `https://n8n.example.com/auth/realms/n8n` — *"A unique identifier or URL for the application. Cannot be changed after creation."* Must equal `entityId` in `realm-n8n.json`. Get it wrong and it cannot be edited — you delete and recreate. |
| **ACS URL** | `https://n8n.example.com/auth/realms/n8n/broker/prism/endpoint` — *"The URL where Prism sends the SAML assertion after authentication."* This is Keycloak's broker endpoint; `prism` in the path is the IdP alias in `realm-n8n.json`. |
| **Name ID Format** | `Persistent` (matches the realm's `nameIDPolicyFormat`) |
| Single Logout URL *(optional)* | `https://n8n.example.com/auth/realms/n8n/broker/prism/endpoint` |
| IDP Initiated SSO Relay State *(optional)* | `https://n8n.example.com/` |

After creation Prism gives you **IdP Metadata**, an **X.509 signing certificate**, and an **SSO URL**. You need the last two.

## Configure

**1. Fill in `realm-n8n.json`** — five values, all marked `REPLACE-ME`:

```bash
grep -n REPLACE-ME realm-n8n.json
```

| JSON path | Value |
|---|---|
| `clients[0].secret` | any strong random string — must equal `OIDC_CLIENT_SECRET` in `../.env` |
| `clients[0].redirectUris[0]` | `https://n8n.example.com/oauth2/callback` |
| `identityProviders[0].config.entityId` | the **Client ID** you gave Prism |
| `identityProviders[0].config.singleSignOnServiceUrl` | Prism's **SSO URL** |
| `identityProviders[0].config.singleLogoutServiceUrl` | Prism's logout URL (or delete the line) |
| `identityProviders[0].config.signingCertificate` | the body of Prism's X.509 cert — **base64 on one line, no `-----BEGIN/END-----` headers, no newlines** |

Keycloak does **not** substitute environment variables inside realm import files — `$(env:VAR)` and `${env.VAR}` both arrive as literal text and the import fails on the first malformed URL (verified on 26.7.1). The values go in the file.

> Your filled-in `realm-n8n.json` holds a client secret. Treat it like `.env` — don't commit it.

**2. Add to `../.env`:**

```bash
# Point oauth2-proxy at Keycloak instead of at an IdP directly
OIDC_ISSUER_URL=https://n8n.example.com/auth/realms/n8n
OIDC_CLIENT_ID=n8n-gateway
OIDC_CLIENT_SECRET=<the same secret you put in realm-n8n.json>

KC_DB_PASSWORD=<openssl rand -base64 24>
KC_ADMIN_USER=admin
KC_ADMIN_PASSWORD=<openssl rand -base64 24>
```

**3. Restrict who gets in**, in `../oauth2-proxy.cfg` — it ships fail-closed with a placeholder domain:

```ini
email_domains = ["cloudkeeper.com"]
# or, to gate on a Prism group (the realm already maps SAML `groups` → the OIDC `groups` claim):
# allowed_groups   = ["n8n-users"]
# oidc_groups_claim = "groups"
```

## Deploy

From `examples/sso/`:

```bash
docker compose -f docker-compose.yml -f prism-saml/docker-compose.yml up -d
docker compose logs -f keycloak      # wait for "Realm 'n8n' imported"
```

Already on ALB + nginx? Same idea, with the `--env-file` flag that variant needs:

```bash
docker compose --env-file .env -f nginx-alb/docker-compose.yml -f prism-saml/docker-compose.yml up -d
```

Both edge configs already route `/auth/*` to Keycloak, un-gated. Keycloak is served under `/auth/` on the **same hostname** as n8n — no second DNS record, no second certificate. It is never published to the host; only the edge reaches it.

## Verify

```bash
# 1. Discovery document is live and the issuer is the PUBLIC url
curl -s https://n8n.example.com/auth/realms/n8n/.well-known/openid-configuration | jq .issuer
#    → "https://n8n.example.com/auth/realms/n8n"

# 2. An unauthenticated editor request bounces toward Prism
curl -sI https://n8n.example.com/ | head -1        # → 302

# 3. Keycloak sends you straight to Prism, with no Keycloak login page in between
#    (follow it in a browser: you should land on Prism, not on a Keycloak form)
```

Step 3 works because `realm-n8n.json` replaces the built-in browser flow with one whose Identity Provider Redirector is pinned to `prism`. Verified: the authorize endpoint answers `303 → /realms/n8n/broker/prism/login`, which renders a SAML POST-binding form targeting Prism's SSO URL.

Then run the parent [testing matrix](../README.md#18-testing-matrix) — webhooks, API, WebSocket push and credential OAuth callbacks are unaffected by the broker.

## Group-based access

The realm ships an IdP mapper copying the SAML `groups` attribute onto the brokered user, and a protocol mapper publishing it as the OIDC `groups` claim — so `allowed_groups` in oauth2-proxy gates on **Prism** group membership. `syncMode: FORCE` re-applies it on **every** login, so removing someone from the group in Prism takes effect on their next sign-in.

Prism's attribute names are whatever your tenant emits. If group gating doesn't work, look at a real assertion (Keycloak admin console → *Realm* → *Sessions*, or turn on `KC_LOG_LEVEL=DEBUG` briefly) and fix `attribute.name` in `identityProviderMappers`.

## Logout

Keycloak gives you a real `end_session_endpoint`, which the parent example lacked:

```
https://n8n.example.com/oauth2/sign_out?rd=https://n8n.example.com/auth/realms/n8n/protocol/openid-connect/logout
```

That clears the gateway cookie **and** the Keycloak session, and — with `singleLogoutServiceUrl` set — propagates to Prism. n8n's own cookie is still separate; see the parent README's double-login note.

## Operating notes

- **The realm imports once.** On first boot against an empty database, then never again (`IGNORE_EXISTING`). Later edits to `realm-n8n.json` do nothing — change the realm in the admin console at `/auth/admin/`, or `docker compose down -v` the `keycloak_db` volume to re-import from scratch.
- **Back up the `keycloak_db` volume.** It holds the brokered user records; losing it means every user re-federates on next login (harmless) and any console-side changes are gone (not harmless).
- **Certificate rotation.** When Prism rotates its SAML signing certificate, update `signingCertificate` in the admin console. If Prism publishes a metadata **URL**, prefer setting the IdP's `useMetadataDescriptorUrl`/`metadataDescriptorUrl` instead and Keycloak tracks rotation itself.
- **Pin ≥ 26.7.** It carries the SAML broker fixes — a disabled SAML IdP could still complete IdP-initiated logins, and encrypted assertions are now verified to be properly signed.
- **Two extra containers** (Keycloak + Postgres) is the real cost of Prism being SAML-only. If that is too much, the alternative is Prism → an OIDC-capable IdP → n8n, which is more moving parts, not fewer.

## Sources

- [Prism — Custom Applications](https://docs.prism.cloudkeeper.com/admin-portal/custom-applications/) · [Identity Providers](https://docs.prism.cloudkeeper.com/admin-portal/identity-providers/) · [Admin Portal](https://docs.prism.cloudkeeper.com/admin-portal/)
- [CloudKeeper Prism product page](https://www.cloudkeeper.com/cloudkeeper-prism)
- [Keycloak 26.7 release notes](https://www.keycloak.org/2026/07/keycloak-2670-released) · [SAML identity brokering](https://www.keycloak.org/docs/latest/server_admin/index.html#_identity_broker)
