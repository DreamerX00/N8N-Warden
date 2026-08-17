# SSO for self-hosted n8n Community Edition

Organization-wide single sign-on against **your own IdP** — SAML or OIDC, MFA, central deprovisioning — **without buying Enterprise and without touching n8n**.

n8n's *built-in* SSO page is an Enterprise-licensed feature and stays locked; this does not unlock, patch, or circumvent it. Instead it authenticates every user at an **external gateway in front of n8n**, so unauthenticated traffic never reaches the editor. n8n runs completely stock — the image is unmodified, so upgrades are just a tag bump.

> **What this is, honestly.** This reproduces the *user experience and security outcome* of SSO — your IdP is the front door, MFA and deprovisioning live there, nobody sees n8n without authenticating. It is **not** identical to native Enterprise SAML: n8n keeps its own login underneath the gate (one extra click per session), and identities are not automatically mapped into n8n's user model. See [Limitations](#limitations-vs-native-enterprise-saml). For a small team, the gap is a second login screen once per session — for most, an acceptable trade to get real SSO on free CE.

Verified against **n8n 2.34.5** (all bypass paths probed live). Every version-sensitive claim is tagged. Pinned tag is now **2.34.6**, the current 2.x stable (2.35.x is beta); nothing in the routing changed between the two.

> ### Using CloudKeeper Prism? One command does all of it
>
> ```bash
> curl -fsSL https://raw.githubusercontent.com/DreamerX00/N8N-Warden/main/examples/sso/prism-saml/install.sh | sudo bash
> ```
>
> Backs up your n8n data, moves host nginx into Compose, stands up the SAML broker, and smoke-tests the result — asking only for what it cannot detect, with everything else pre-filled from your running deployment. See [`prism-saml/`](prism-saml/README.md).
>
> Prism is **SAML-only** where n8n sits. Its admin portal offers *"Custom Applications … to third-party services that support SAML 2.0"*, and the OIDC it advertises is for identity providers **upstream** of Prism (Google, Microsoft, custom OIDC), not for apps downstream of it. oauth2-proxy speaks OIDC only, so it **cannot** point at Prism directly. [`prism-saml/`](prism-saml/README.md) adds Keycloak as a SAML↔OIDC broker — two extra containers, everything else on this page unchanged.

---

## 1. Architecture

```mermaid
flowchart LR
    user([User browser])
    ext([External service<br/>Slack · GitHub · cron])
    idp[[Your IdP<br/>SAML / OIDC + MFA]]

    subgraph host [Single Docker host]
        caddy[Caddy<br/>TLS · path split · forward-auth]
        o2p[oauth2-proxy<br/>OIDC relying party]
        n8n[(n8n CE<br/>unmodified)]
    end

    user -- HTTPS --> caddy
    caddy -- "editor / rest / push<br/>(gated)" --> o2p
    o2p -. "not authenticated" .-> idp
    idp -. "assertion / token" .-> o2p
    o2p -- "2xx authenticated" --> caddy
    caddy -- forward --> n8n

    ext -- "/webhook /form /mcp<br/>(BYPASS auth)" --> caddy --> n8n
```

One host, one public entry point (Caddy), one auth engine (oauth2-proxy). n8n is on a private compose network — never published to the host, never internet-facing.

---

## 2. Request flow — User → IdP → Gateway → n8n

1. User opens `https://n8n.example.com/`.
2. Caddy matches the gated route, calls `oauth2-proxy /oauth2/auth`.
3. No valid session → oauth2-proxy returns 401 → Caddy redirects the browser to `/oauth2/sign_in`, which bounces to **your IdP**.
4. User authenticates at the IdP (**MFA, conditional access, device policy all enforced here**).
5. IdP redirects back to `/oauth2/callback`; oauth2-proxy validates the token, sets its **session cookie**, redirects to the original URL.
6. Caddy re-runs `/oauth2/auth` → now **2xx** → request is proxied to `n8n:5678`.
7. n8n shows **its own login once** (see [§Double login](#the-double-login-reality)); after that, n8n's session cookie persists for the browser session.

## 3. Webhook flow — External service → n8n

```mermaid
sequenceDiagram
    participant Ext as Slack / GitHub / cron
    participant Caddy
    participant n8n
    Ext->>Caddy: POST https://n8n.example.com/webhook/abc
    Caddy->>Caddy: path matches @public → NO auth
    Caddy->>n8n: proxy /webhook/abc
    n8n-->>Ext: 200 (workflow fired)
```

External callers can't do interactive SSO, so trigger paths **bypass the gate entirely**. They remain protected by n8n's own webhook secrecy (unguessable path IDs) and whatever auth the workflow's trigger node enforces (HMAC signatures, header auth, etc.) — not by the IdP.

---

## 4–7. Traffic classes and the path split

The routing decision *is* the security boundary. Verified live on **2.34.5**:

| Class | Paths | Policy | Why |
|---|---|---|---|
| **Browser / UI** | `/`, `/assets/*` | **GATE** | The editor. Humans, interactive. |
| **REST (editor backend)** | `/rest/*` incl. `/rest/login` | **GATE** | Backs the UI; must sit behind the same session. |
| **WebSocket push** | `/rest/push` | **GATE** | Editor live-updates. Caddy upgrades the WS transparently; the auth check runs on the HTTP upgrade request, which carries the cookie. |
| **Webhook execution** | `/webhook/*`, `/webhook-waiting/*` | **BYPASS** | Slack/GitHub/Stripe/Wait-node callbacks. No browser, no SSO. |
| **Webhook (test)** | `/webhook-test/*` | **BYPASS** | Editor "listen for test event" delivery from outside. |
| **Forms** | `/form/*`, `/form-test/*`, `/form-waiting/*` | **BYPASS** | Form Trigger — meant to be filled by the public. |
| **MCP triggers** | `/mcp/*`, `/mcp-test/*` | **BYPASS** | n8n exposed as an MCP tool server; external MCP clients can't SSO. |
| **Health** | `/healthz`, `/healthz/readiness` | **BYPASS** | Docker/LB probes. `/metrics` too if you enable `N8N_METRICS`. |
| **Credential OAuth callback** | `/rest/oauth2-credential/callback`, `/rest/oauth1-credential/callback` | **BYPASS** | n8n's *own* OAuth (e.g. authorizing a Google Sheets credential). The provider redirect can arrive without the session cookie; gating it can break credential setup. |
| **Public API** | `/api/*` | **BYPASS** | Authenticates with `X-N8N-API-KEY`, not cookies. Gating it blocks your scripts/CI. See [§12](#12-api-authentication-strategy). |
| **SAML broker** | `/auth/*` | **BYPASS** | Keycloak, only in the [`prism-saml/`](prism-saml/README.md) variant. Un-gated because it *is* the login flow — same reason as `/oauth2/*`. n8n serves nothing under `/auth`. |

**Security implication of each bypass:** a bypassed path is **not** behind your IdP — it's exposed to the internet, guarded only by n8n's own mechanism for that path. Webhooks rely on unguessable IDs + trigger-node auth; forms are intentionally public; the API relies on its key. **The editor and all credential/workflow management stay fully gated**, so a bypass never exposes the ability to read credentials or edit workflows. If a webhook needs stronger protection, add auth *in the workflow* (Header Auth / HMAC verification node), not at the proxy.

---

## 8. Gateway comparison

| Gateway | Speaks | Self-hosted | Best when |
|---|---|:---:|---|
| **oauth2-proxy** *(used here)* | OIDC/OAuth2 | ✅ | Your IdP offers OIDC (Keycloak, Authentik, Entra, Okta, Google). Simplest, single binary, purpose-built for path-gated forward-auth. **Use ≥ 7.15.2** — see the version note below. |
| **+ Keycloak broker** *(ships here)* | OIDC **+ SAML** | ✅ | Your IdP is **SAML-only — including CloudKeeper Prism**. Keycloak consumes the SAML assertion and re-issues it as OIDC for oauth2-proxy. Ready to deploy in [`prism-saml/`](prism-saml/README.md); realm config is a single imported JSON file. |
| **Authentik proxy provider** | OIDC **+ SAML** | ✅ | The same job as the Keycloak broker, if Authentik is already your standard. Its outpost consumes SAML upstream and presents forward-auth downstream; callback path becomes `/outpost.goauthentik.io/*`. Not shipped here. |
| **Authelia** | OIDC RP + MFA | ✅ | You want built-in MFA/WebAuthn at the gate itself, or already run it. Clean YAML `access_control` bypass rules. |
| **Traefik ForwardAuth** | delegates | ✅ | Already on Traefik. Adds nothing auth-wise over Caddy; just a different place to declare bypasses. |
| **nginx `auth_request`** | delegates | ✅ | nginx is your standard. Works, but the path-split spreads across more `location` blocks. |
| **Cloudflare Access** | OIDC/SAML | ❌ (SaaS) | You want zero self-hosted auth infra and are fine with Cloudflare in the path. Fastest to stand up; least self-hosted. |

**SAML specifically:** oauth2-proxy does not speak SAML, and never will — it's an OAuth2/OIDC relying party by design. If your IdP only offers SAML (**Prism does**), you need a broker in front of it: [`prism-saml/`](prism-saml/README.md) ships one. Prefer OIDC if your IdP offers both — it's simpler and every provider here supports it.

**oauth2-proxy version — pin ≥ 7.15.2, not 7.11.0.** Three fixes matter for a forward-auth deployment like this one, and the last is exploitable against exactly this topology:

| Version | Fix |
|---|---|
| 7.11.0 | [CVE-2025-54576](https://github.com/advisories/GHSA-7rh7-c77v-6434) (CVSS 9.1) — `skip_auth_routes` regexes matched the **query string** as well as the path, so appending a crafted parameter to a protected URL could satisfy a skip rule and bypass auth entirely |
| 7.13.0 | CVE-2025-64484 — request header smuggling; also normalizes header names when matching headers to strip |
| **7.15.2** | **Critical.** `X-Forwarded-Uri` header spoofing auth bypass, session fixation, health-check user-agent bypass, email validation bypass via multi-`@` claims. Adds `--trusted-proxy-ip`. |

Both compose files here pin **v7.15.3** (current, June 2026). The Caddyfile and the nginx config additionally **strip client-supplied `X-Forwarded-Uri`, `X-Forwarded-Method` and `X-Auth-Request-*` headers** at the edge, so the spoofing class is dead at the door regardless of the auth engine's version.

---

## Already running ALB → nginx → n8n? Use the nginx variant

The default above uses Caddy as a fresh edge. If you **already have an AWS ALB and nginx** in front of n8n (as most production deployments do), don't add Caddy — keep your edge and let **nginx do the auth check** via `auth_request`. Files: [`nginx-alb/`](nginx-alb/).

```
ALB (TLS)  →  your nginx (n8n-sso.conf)  →  oauth2-proxy  (auth check)
                                          →  n8n          (unmodified)
```

- **TLS stays at the ALB.** nginx listens HTTP behind it and trusts `X-Forwarded-Proto`. n8n runs with **`N8N_PROXY_HOPS=2`** (ALB + nginx) — get this wrong and n8n miscomputes client IPs and webhook URLs.
- **ALB target health check must hit `/healthz`** (un-gated), never `/` — the editor now 302-redirects to the IdP, which the ALB would read as unhealthy.
- **ALB supports WebSockets natively**; nginx forwards the `Upgrade`/`Connection` headers for `/rest/push` (in the config). Bump the ALB idle timeout to ≥ 3600s so the editor's live push isn't cut.
- **n8n binds to `127.0.0.1` only** — reachable by nginx, never the internet. Verify with `docker ps` (no `0.0.0.0:5678`).

Deploy: fill `.env`, then — **note the `--env-file` flag, it is not optional** —

```bash
cd examples/sso
docker compose --env-file .env -f nginx-alb/docker-compose.yml up -d
# with Prism (SAML), add the broker:
# docker compose --env-file .env -f nginx-alb/docker-compose.yml -f prism-saml/docker-compose.yml up -d
```

then drop `nginx-alb/n8n-sso.conf` into your nginx and reload. Everything in the [testing matrix](#18-testing-matrix) applies unchanged.

> **Why the flag.** Compose resolves an implicit `.env` next to the **compose file** (`nginx-alb/`), not your shell's cwd, and `env_file:` inside the service does *not* feed `${VAR}` interpolation. Without `--env-file` every variable silently resolves to empty: oauth2-proxy boots with a blank client id and a redirect URL of `https:///oauth2/callback`, and the only symptom is an opaque error at your IdP. Verified — `docker compose config` prints exactly that.

### Even cleaner on AWS (if your IdP speaks OIDC): let the ALB do it

An AWS ALB can authenticate natively with an `authenticate-oidc` listener action — **no oauth2-proxy container at all.** You add ordered listener rules: `authenticate-oidc` then `forward` for the editor paths, and plain `forward` (no auth) for `/webhook/*`, `/api/*`, `/healthz` — the same path split, expressed as ALB rules instead of nginx locations. Fewest moving parts on AWS, but it (a) requires an **OIDC** issuer (issuer URL, client id/secret, and the ALB's `/oauth2/idpresponse` callback registered), and (b) moves the config into your ALB/Terraform rather than a file.

**With Prism, this is not available on its own.** Prism issues SAML to apps, and `authenticate-oidc` cannot consume SAML. Your options are the Keycloak broker ([`prism-saml/`](prism-saml/README.md)) — after which the ALB *could* do `authenticate-oidc` against Keycloak instead of running oauth2-proxy, if you prefer the config in Terraform — or the nginx + oauth2-proxy variant as written. Either way something has to speak SAML to Prism.

---

## 9–10. Deploy

Prereqs: a DNS record, ports 80+443 open, Docker Compose.

```bash
cd examples/sso
cp .env.example .env
openssl rand -base64 32 | tr '+/' '-_'   # → paste as OAUTH2_PROXY_COOKIE_SECRET
$EDITOR .env                     # set N8N_DOMAIN, OIDC_ISSUER_URL, client id/secret

docker compose up -d
docker compose logs -f caddy     # watch the TLS cert issue
```

### IdP configuration (register n8n as an OIDC client)

**Prism users:** skip this section — Prism registers n8n as a **SAML** Custom Application instead, and the fields are different. See [`prism-saml/`](prism-saml/README.md).

In your IdP, create an OIDC/OAuth application:

| Field | Value |
|---|---|
| Redirect / callback URI | `https://n8n.example.com/oauth2/callback` |
| Grant type | Authorization Code |
| Scopes | `openid email profile` (add `groups` to gate on group membership) |
| Client ID / Secret | → `.env` as `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` |

Then restrict *who* gets in, in `oauth2-proxy.cfg`: `email_domains = ["yourcompany.com"]`, or `allowed_groups = ["n8n-users"]` with `oidc_groups_claim = "groups"`.

### DNS
`n8n.example.com` → A/AAAA record to this host's public IP. Caddy needs it resolvable before it can get a cert.

### TLS
Automatic. Caddy fetches and renews a Let's Encrypt cert on first boot (ports 80+443 must reach it). Certs persist in the `caddy_data` volume — **back it up** to avoid re-issuance rate limits on rebuild.

---

## 11. Webhook exceptions — see [§4–7 table](#47-traffic-classes-and-the-path-split). Each bypass and its blast radius is documented there.

## 12. API authentication strategy

`/api/*` uses n8n's own API keys (`X-N8N-API-KEY`), not IdP cookies — so it **bypasses** the gate, in **both** the Caddyfile (`@public`) and the nginx config (`location /api/`). *(Earlier revisions of this example documented the bypass but only implemented it in nginx, so API callers behind Caddy got a 302 to the IdP. Fixed — the two gateways now express the same policy.)* Security: API access is only as strong as key hygiene. Harden by (a) issuing keys narrowly, (b) optionally IP-allowlisting `/api/*` at Caddy to your CI/office ranges, (c) rotating keys. If you have *no* API callers, gate `/api/*` too — move it out of `@public` in the Caddyfile.

## 13. WebSocket handling

n8n's editor push is a WebSocket at **`/rest/push`** (default backend on 2.34.5; `N8N_PUSH_BACKEND` can switch to SSE). It's a **gated** path — Caddy's `reverse_proxy` upgrades it transparently, and `forward_auth` validates the cookie on the HTTP upgrade handshake. No extra config. If push ever fails to connect behind the proxy, confirm the upgrade headers aren't being stripped and that the auth cookie is present on `/rest/push`.

## 14. Upgrade strategy

n8n is **unmodified**, so upgrades don't interact with the gateway at all:

```bash
# bump N8N_TAG in .env, then:
docker compose pull n8n && docker compose up -d n8n
```

The gateway (Caddy, oauth2-proxy) upgrades independently on its own cadence — watch oauth2-proxy security releases. Across an n8n **major** (2.x → 3.x), take a data snapshot first and re-run the [testing matrix](#18-testing-matrix), especially webhook paths (the one thing that would change the routing). Because nothing patches n8n, there's no hook to re-verify on each release — the standing weakness of the "trusted-header" auto-login approach this design deliberately avoids.

## 15. Backup / rollback

| Asset | Backup |
|---|---|
| n8n data (workflows, credentials, **encryption key**) | snapshot the `n8n_data` volume / bind dir — or use `n8n-warden`'s snapshot |
| TLS certs | the `caddy_data` volume |
| Config | `.env`, `Caddyfile`, `oauth2-proxy.cfg` (keep `.env` out of git) |

**Rollback to no-SSO** is instant and non-destructive — the gateway never wrote to n8n's data: `docker compose down`, then run n8n directly on 5678 again. Your workflows and users are untouched.

## 16. Security hardening checklist

- [ ] `OAUTH2_PROXY_COOKIE_SECRET` is a fresh `openssl rand -base64 32 | tr '+/' '-_'`, unique per deploy — without the `tr`, a secret containing `+` or `/` fails to decode and oauth2-proxy refuses to start
- [ ] oauth2-proxy image is **≥ 7.15.2** (ships pinned to v7.15.3 — see [§8](#8-gateway-comparison) for what each fix covers)
- [ ] Access restricted by `email_domains` **or** `allowed_groups` — the shipped config fails closed with a `CHANGE-ME` placeholder domain; if you replaced it with `*` you have re-opened it
- [ ] Edge strips client-supplied `X-Forwarded-Uri` / `X-Forwarded-Method` / `X-Auth-Request-*` (both configs here do)
- [ ] n8n is **not** published to the host (`expose`, not `ports`) — verify `docker ps` shows no `0.0.0.0:5678`
- [ ] `N8N_SECURE_COOKIE=true` and the whole path is HTTPS
- [ ] `N8N_PROXY_HOPS` matches your real proxy count
- [ ] HSTS + security headers present (in the Caddyfile)
- [ ] Webhook trigger nodes that handle sensitive actions add their own auth (HMAC/header) — bypass ≠ open door, but defense-in-depth
- [ ] `/metrics` not exposed publicly (bypassed only if you enable it; restrict to internal)
- [ ] Backups of `n8n_data` **and** the encryption key verified restorable
- [ ] IdP enforces MFA and has n8n users provisioned/deprovisioned in the right group
- [ ] *(prism-saml)* Keycloak ≥ 26.7, `keycloak_db` volume backed up, `realm-n8n.json` (holds a client secret) not committed

## 17. Step-by-step deployment

1. Point DNS `n8n.example.com` at the host; open 80+443.
2. Register the OIDC client in your IdP (callback `https://n8n.example.com/oauth2/callback`).
3. `cp .env.example .env`; fill domain, issuer, client id/secret; generate the cookie secret.
4. `docker compose up -d`; watch `docker compose logs -f caddy` until the cert issues.
5. Browse to `https://n8n.example.com` → you should bounce to the IdP.
6. Authenticate → land on n8n's login → sign in / create the owner once.
7. Run the [testing matrix](#18-testing-matrix).
8. Restrict access (`email_domains`/`allowed_groups`), redeploy oauth2-proxy.
9. Manage per-user n8n accounts and their access with [`n8n-warden`](../../README.md).

## 18. Testing matrix

| # | Test | Expected |
|---|---|---|
| 1 | Unauthenticated UI (`curl -I https://host/`) | 302 → IdP sign-in |
| 2 | Authenticated UI (browser, after IdP) | n8n editor loads |
| 3 | Logout | oauth2-proxy `/oauth2/sign_out` clears gate; n8n logout clears its own cookie (no SLO — see below) |
| 4 | Expired gateway session | next request re-bounces to IdP |
| 5 | Revoked IdP user | IdP refuses; gate denies; user can't reach n8n |
| 6 | Webhook execution (`curl -X POST https://host/webhook/<id>`) | 200, workflow fires — **no** IdP redirect |
| 7 | API request (`curl -H "X-N8N-API-KEY: …" https://host/api/v1/workflows`) | 200 — not gated |
| 8 | WebSocket (open editor, edit a node) | live updates work (`/rest/push` connected) |
| 9 | OAuth credential in a workflow (authorize Google Sheets) | callback completes, credential saved |
| 10 | Container health (`docker inspect -f '{{.State.Health.Status}}' n8n`) | `healthy`; `/healthz` reachable un-gated |
| 11 | n8n upgrade (bump `N8N_TAG`, `up -d`) | comes back healthy; re-run 1, 6, 8 |
| 12 | Header spoofing (`curl -H 'X-Auth-Request-Email: admin@you.com' -H 'X-Forwarded-Uri: /webhook/x' https://host/`) | still 302 → IdP; the injected headers never reach oauth2-proxy or n8n |
| 13 | *(prism-saml)* `curl -s https://host/auth/realms/n8n/.well-known/openid-configuration \| jq .issuer` | `https://host/auth/realms/n8n` |

---

## The double-login reality

n8n CE has **no supported trusted-header / external-identity hook** (confirmed: no such env var exists in the running 2.34.5). So n8n keeps its own login *under* the gate. Real options:

- **Two logins (this design, recommended).** IdP once (SSO'd across your org), then n8n's own login once per browser session. Zero hacks, survives every upgrade. This is what most production deployments actually ship.
- **Shared n8n account behind the gate.** One n8n login for everyone past the IdP. Loses per-user attribution, ownership, and makes n8n's RBAC meaningless — only for a one/two-person instance. Not real SSO; don't confuse the two.
- **Trusted-header auto-login (a known community hack).** Splices an undocumented `EXTERNAL_HOOK_FILES` middleware into n8n to read a proxy header and call n8n's internal `issueCookie()`. Eliminates the second login *but* depends on n8n internals that have already broken once across versions — budget re-verification on every upgrade, and the proxy **must** strip any client-supplied copy of the trust header or you have an auth bypass. This design deliberately avoids it for a stock, upgrade-safe instance. Documented here so you can make the call, not recommended by default.

**Single logout (SLO):** the gateway cookie and n8n's cookie are independent. "Log out everywhere" means hitting both `/oauth2/sign_out` and n8n's logout; there's no SLO wired between them out of the box. The [`prism-saml/`](prism-saml/README.md) variant improves half of this — Keycloak exposes a real `end_session_endpoint`, so `/oauth2/sign_out?rd=<end_session_endpoint>` clears the gateway **and** the IdP session in one hop, and propagates to Prism when `singleLogoutServiceUrl` is set. n8n's own cookie is still its own.

## Limitations vs native Enterprise SAML

| | This gateway | Native Enterprise SAML |
|---|---|---|
| SSO to reach n8n | ✅ | ✅ |
| MFA / conditional access | ✅ (at IdP) | ✅ (at IdP) |
| Central deprovisioning | ✅ (IdP group) | ✅ |
| SAML-only IdP (e.g. Prism) | ✅ via [`prism-saml/`](prism-saml/README.md) broker | ✅ |
| Second (n8n) login | ⚠️ once per session | ❌ none |
| IdP identity → n8n user mapping | ❌ manual (via warden) | ✅ automatic |
| SCIM / auto-provisioning | ❌ | ✅ (Enterprise) |
| Cost | free (OSS) | paid licence |
| n8n modified | never | n/a |

For the identity-mapping gap, pair this with [`n8n-warden`](../../README.md): the gateway controls *who gets in*, warden controls *what each account can see* once inside.

---

## The simplest production setup I'd deploy for a small team

**Exactly this repo's default: Caddy → oauth2-proxy (OIDC) → n8n, accepting the one extra n8n login.** It's three containers, one `.env`, automatic TLS, nothing patched, and it upgrades with a tag bump.

**On CloudKeeper Prism, add the broker: Caddy → oauth2-proxy → Keycloak → Prism → n8n.** Five containers, still nothing patched, still one `.env`. That's the floor, not a choice — Prism speaks SAML to apps and oauth2-proxy speaks OIDC, so something has to translate. Everything else on this page stays as written.

Skip the trusted-header auto-login unless the second click is a genuine dealbreaker — the stock, upgrade-proof instance is worth more than saving one login per session. Manage the n8n accounts behind the gate with `n8n-warden`.

---

## Sources

Version and vulnerability claims on this page, checked 2026-08-17:

- CloudKeeper Prism — [Custom Applications](https://docs.prism.cloudkeeper.com/admin-portal/custom-applications/) (SAML 2.0 only for third-party apps), [Identity Providers](https://docs.prism.cloudkeeper.com/admin-portal/identity-providers/) (OIDC is upstream of Prism), [product page](https://www.cloudkeeper.com/cloudkeeper-prism)
- oauth2-proxy — [releases](https://github.com/oauth2-proxy/oauth2-proxy/releases) (v7.15.3, 2026-06-09), [CHANGELOG](https://github.com/oauth2-proxy/oauth2-proxy/blob/master/CHANGELOG.md), [CVE-2025-54576 advisory](https://github.com/advisories/GHSA-7rh7-c77v-6434)
- n8n — [2.x release notes](https://docs.n8n.io/changelog/release-notes-2.x) (2.34.6 stable, 2.35.2 beta)
- Keycloak — [26.7.0 release](https://www.keycloak.org/2026/07/keycloak-2670-released), [release notes](https://www.keycloak.org/docs/latest/release_notes/index.html)
