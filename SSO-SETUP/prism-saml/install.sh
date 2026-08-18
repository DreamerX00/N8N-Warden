#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  n8n + CloudKeeper Prism SSO — end-to-end installer
#
#      curl -fsSL https://raw.githubusercontent.com/DreamerX00/N8N-Warden/main/SSO-SETUP/prism-saml/install.sh | sudo bash
#
#  Takes a host that already runs n8n under Docker Compose with nginx on the
#  system, and turns it into:
#
#      ALB ──▶ nginx (container) ──▶ oauth2-proxy ──▶ Keycloak ──▶ Prism (SAML)
#                                 └─▶ n8n (unmodified, same data)
#
#  It backs up first, keeps your existing n8n volume, moves nginx into Compose,
#  asks for every value it needs (explaining what each one is and where to find
#  it), renders the whole stack, brings it up, and smoke-tests the result.
#
#  Nothing is destroyed: the old n8n container is stopped, never removed, and
#  its data volume is reused in place after a full tar backup. Rollback
#  instructions are printed at the end and written to the install directory.
#
#  Non-interactive (CI / re-runs): set ASSUME_YES=1 and supply the same values
#  as environment variables — see PROMPTS below or run with --help.
# ─────────────────────────────────────────────────────────────────────────────
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/DreamerX00/N8N-Warden.git}"
REPO_REF="${REPO_REF:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/n8n-sso}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-n8n-sso}"

# Defaults overridable by env (all are prompted for when interactive).
N8N_DOMAIN="${N8N_DOMAIN:-}"
PUBLIC_SCHEME_EXPLICIT="${PUBLIC_SCHEME:+1}" # did the caller pin it?
PUBLIC_SCHEME="${PUBLIC_SCHEME:-https}"     # http only for local testing
# Left empty on purpose: anything pre-seeded here would win over the value
# detected from your running deployment and silently mask it. The fallback for
# each lives in its ask() call instead.
HTTP_PORT="${HTTP_PORT:-}"                  # host port the nginx container binds
BEHIND_ALB="${BEHIND_ALB:-}"                # yes|no → N8N_PROXY_HOPS 2|1
PRISM_SSO_URL="${PRISM_SSO_URL:-}"
SP_ENTITY_ID="${SP_ENTITY_ID:-}"      # must equal the client ID Prism has for n8n
PRISM_SLO_URL="${PRISM_SLO_URL:-}"
PRISM_SIGNING_CERT="${PRISM_SIGNING_CERT:-}"
ALLOWED_EMAIL_DOMAIN="${ALLOWED_EMAIL_DOMAIN:-}"
ALLOWED_GROUP="${ALLOWED_GROUP:-}"
GENERIC_TIMEZONE="${GENERIC_TIMEZONE:-}"
N8N_TAG="${N8N_TAG:-}"                      # blank → keep whatever tag is running
EXTRA_HOST_IP="${EXTRA_HOST_IP:-}"          # see "hairpin" note in phase 6
ASSUME_YES="${ASSUME_YES:-}"
SKIP_BACKUP="${SKIP_BACKUP:-}"

# Pinned, for the reasons documented in ../README.md §8.
IMG_OAUTH2_PROXY="quay.io/oauth2-proxy/oauth2-proxy:v7.15.3"
IMG_KEYCLOAK="quay.io/keycloak/keycloak:26.7"
IMG_POSTGRES="postgres:17-alpine"
IMG_NGINX="nginx:1.29-alpine"
DEFAULT_N8N_TAG="2.34.6"

# ── output ───────────────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; X=$'\033[0m'
else B=""; DIM=""; R=""; G=""; Y=""; C=""; X=""; fi

STEP=0
step()  { STEP=$((STEP+1)); printf '\n%s━━ %d/9  %s%s\n' "$B$C" "$STEP" "$*" "$X"; }
say()   { printf '   %s\n' "$*"; }
info()  { printf '   %s%s%s\n' "$DIM" "$*" "$X"; }
ok()    { printf '   %s✔%s %s\n' "$G" "$X" "$*"; }
warn()  { printf '   %s!%s %s\n' "$Y" "$X" "$*"; }
die()   { printf '\n%s✘ %s%s\n\n' "$R" "$*" "$X" >&2; exit 1; }
on_err() {
  local line="$1"
  printf '\n%s✘ install failed at line %s%s\n' "$R$B" "$line" "$X" >&2
  if [ -f "$INSTALL_DIR/ROLLBACK.md" ]; then
    printf '  The cutover had already started — undo it with %s%s/ROLLBACK.md%s\n' "$B" "$INSTALL_DIR" "$X" >&2
  else
    printf '  Nothing was changed on this host yet.%s\n' "$X" >&2
    [ -d "${BACKUP:-}" ] && printf '  Backups (if any) are in %s\n' "$BACKUP" >&2
  fi
  exit 1
}
trap 'on_err $LINENO' ERR

banner() {
  printf '%s' "$B$C"
  cat <<'EOF'

   ┌──────────────────────────────────────────────────────────┐
   │  n8n  ×  CloudKeeper Prism SSO                           │
   │  SAML single sign-on for self-hosted n8n Community Ed.   │
   └──────────────────────────────────────────────────────────┘
EOF
  printf '%s\n' "$X"
}

# ── prompting ────────────────────────────────────────────────────────────────
# Everything this script can work out for itself is worked out and offered as a
# pre-filled, editable answer: press Enter to take it, or edit it in place.
TTY_OK=""
[ -r /dev/tty ] && [ -c /dev/tty ] && TTY_OK=1

# ask VAR "Label" "what it is / where to get it" "detected-or-default" ["source"]
ask() {
  local __var="$1" label="$2" help="$3" def="${4:-}" src="${5:-}" cur ans
  cur="$(eval printf '%s' "\"\${$__var}\"")"
  # A value passed in the environment wins as the pre-fill, but is still editable.
  [ -n "$cur" ] && { def="$cur"; src="${src:-from environment}"; }
  if [ -n "$ASSUME_YES" ] || [ -z "$TTY_OK" ]; then
    [ -n "$def" ] || die "$__var has no value and there is no terminal to ask on.
   $help"
    eval "$__var=\$def"; info "$label: $def"; return
  fi
  printf '\n%s%s%s' "$B" "$label" "$X"
  [ -n "$src" ] && printf ' %s(%s — Enter to accept, or edit)%s' "$G" "$src" "$X"
  printf '\n%s%s%s\n' "$DIM" "$help" "$X"
  # read -e pre-loads the line editor with the detected value, so the user sees
  # it on the input line and can edit it rather than retype it.
  ans=""
  read -e -i "$def" -r -p "   ▸ " ans </dev/tty || true
  [ -n "$ans" ] || ans="$def"
  [ -n "$ans" ] || die "$label is required."
  eval "$__var=\$ans"
}

confirm() {  # confirm "question" [default:y|n]
  local prompt="$1" def="${2:-n}" ans hint="[y/N]"
  [ "$def" = y ] && hint="[Y/n]"
  [ -n "$ASSUME_YES" ] && return 0          # that is what ASSUME_YES means
  [ -z "$TTY_OK" ] && { [ "$def" = y ]; return; }
  printf '\n%s%s%s %s: ' "$B$Y" "$prompt" "$X" "$hint"
  IFS= read -r ans </dev/tty || true
  [ -n "$ans" ] || ans="$def"
  case "$ans" in y|Y|yes|YES) return 0;; *) return 1;; esac
}

# Read one KEY=value out of a previous .env, for re-runs.
prev() { [ -f "$INSTALL_DIR/.env" ] && sed -n "s/^$1=//p" "$INSTALL_DIR/.env" | head -1 || true; }
# Read one env var off a running container.
cenv() { docker inspect -f "{{range .Config.Env}}{{println .}}{{end}}" "$1" 2>/dev/null | sed -n "s/^$2=//p" | head -1 || true; }
# First non-empty argument. Always succeeds — it is used inside $( ), where a
# non-zero status would trip `set -e`.
first() { for a in "$@"; do [ -n "$a" ] && { printf '%s' "$a"; return 0; }; done; return 0; }

randpw() { head -c 32 /dev/urandom | base64 | tr -d '\n=+/' | head -c 32; }

# ─────────────────────────────────────────────────────────────────────────────
[ "${1:-}" = "--help" ] && { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

banner

# ── 1. preflight ─────────────────────────────────────────────────────────────
step "Preflight"
[ "$(id -u)" -eq 0 ] || die "run as root: curl -fsSL <url> | sudo bash"
for c in docker curl tar; do command -v "$c" >/dev/null || die "missing required command: $c"; done
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 plugin not found (need 'docker compose', not 'docker-compose')."
command -v git >/dev/null || die "missing required command: git"
ok "root, docker $(docker version --format '{{.Server.Version}}' 2>/dev/null), compose $(docker compose version --short)"

if [ "${HTTP_PORT:-80}" = "80" ] && ! command -v systemctl >/dev/null; then
  warn "no systemd — if something else holds port 80 you must stop it yourself"
fi

# ── 2. discover the existing deployment ──────────────────────────────────────
step "Discovering the current n8n deployment"

N8N_CONTAINER="$(docker ps -a --format '{{.Names}}\t{{.Image}}' | awk -F'\t' 'tolower($2) ~ /n8nio\/n8n/ {print $1; exit}')"
N8N_DATA_SOURCE=""; N8N_DATA_KIND=""; OLD_COMPOSE_FILES=""; RUNNING_TAG=""

if [ -n "$N8N_CONTAINER" ]; then
  ok "found n8n container: $N8N_CONTAINER"
  RUNNING_TAG="$(docker inspect -f '{{.Config.Image}}' "$N8N_CONTAINER" | sed 's/.*://')"
  read -r N8N_DATA_KIND N8N_DATA_SOURCE <<<"$(
    docker inspect -f '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Type}} {{if eq .Type "volume"}}{{.Name}}{{else}}{{.Source}}{{end}}{{end}}{{end}}' "$N8N_CONTAINER"
  )" || true
  OLD_COMPOSE_FILES="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$N8N_CONTAINER" 2>/dev/null || true)"
  [ -n "$N8N_DATA_SOURCE" ] || die "could not find the /home/node/.n8n mount on $N8N_CONTAINER — refusing to guess where your workflows live."
  ok "n8n data: $N8N_DATA_KIND → $N8N_DATA_SOURCE"
  say "running image tag: ${RUNNING_TAG:-unknown}"
  [ -n "$OLD_COMPOSE_FILES" ] && say "current compose file: $OLD_COMPOSE_FILES"
  # Everything n8n was already told about itself — the most reliable source of
  # truth for domain, scheme, proxy hops and timezone.
  DET_HOST="$(cenv "$N8N_CONTAINER" N8N_HOST)"
  DET_PROTO="$(cenv "$N8N_CONTAINER" N8N_PROTOCOL)"
  DET_HOPS="$(cenv "$N8N_CONTAINER" N8N_PROXY_HOPS)"
  DET_TZ="$(cenv "$N8N_CONTAINER" GENERIC_TIMEZONE)"
  DET_WEBHOOK="$(cenv "$N8N_CONTAINER" WEBHOOK_URL)"
  [ -z "$DET_HOST" ] && [ -n "$DET_WEBHOOK" ] && DET_HOST="$(printf '%s' "$DET_WEBHOOK" | sed -E 's#^[a-z]+://##; s#[:/].*##')"
  [ -n "$DET_HOST" ] && ok "n8n already knows its hostname: $DET_HOST"
  # Anything else the operator had set on n8n is their choice, not ours to
  # silently change. Container env minus image env = what was set at run time;
  # we keep all of it except the handful of vars the new compose block manages.
  # Getting this wrong once cost a production instance: an added
  # DB_SQLITE_POOL_SIZE broke insights writes, which leaked until n8n hit the
  # 2 GB V8 heap limit and aborted every ~35 minutes.
  N8N_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$N8N_CONTAINER" 2>/dev/null || true)"
  N8N_CARRY="$(comm -23 \
      <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$N8N_CONTAINER" 2>/dev/null | sort -u) \
      <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$N8N_IMAGE_REF"   2>/dev/null | sort -u) \
    | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' \
    | grep -vE '^(N8N_HOST|N8N_PROTOCOL|N8N_PORT|N8N_EDITOR_BASE_URL|WEBHOOK_URL|N8N_SECURE_COOKIE|N8N_PROXY_HOPS|GENERIC_TIMEZONE|TZ)=' \
    | sed 's/^/      - /')" || true
  [ -n "$N8N_CARRY" ] && ok "carrying over $(printf '%s\n' "$N8N_CARRY" | grep -c .) existing n8n setting(s)"
else
  warn "no existing n8n container found — a fresh one will be created with a new volume"
  N8N_DATA_KIND="volume"; N8N_DATA_SOURCE="${COMPOSE_PROJECT}_n8n_data"
  DET_HOST=""; DET_PROTO=""; DET_HOPS=""; DET_TZ=""; N8N_CARRY=""
fi

# How is n8n reachable *today*? If it publishes a host port, your load balancer
# almost certainly targets that port, and it will have to be repointed at nginx
# after the cutover — the new stack deliberately publishes nothing but nginx.
OLD_PUBLISHED=""
if [ -n "$N8N_CONTAINER" ]; then
  OLD_PUBLISHED="$(docker port "$N8N_CONTAINER" 2>/dev/null | awk -F: '{print $NF}' | sort -u | tr '\n' ' ' | sed 's/ $//')"
  if [ -n "$OLD_PUBLISHED" ]; then
    warn "n8n currently publishes host port(s): $OLD_PUBLISHED"
    say  "  after the cutover it publishes NOTHING — only nginx is exposed."
    say  "  Anything pointing at those ports (an ALB target group, a bookmark)"
    say  "  must be repointed, and they should be closed in your security group:"
    say  "  a caller that can still reach n8n directly skips SSO completely."
  else
    ok "n8n publishes no host port (already only reachable through a proxy)"
  fi
fi

HOST_NGINX="no"; DET_NGINX_DOMAIN=""; DET_NGINX_PORT=""; NGINX_PROXIES_N8N=""
if command -v systemctl >/dev/null && systemctl is-enabled nginx >/dev/null 2>&1; then
  HOST_NGINX="yes"
  # A running nginx that proxies nothing is the stock default site. Worth saying
  # out loud, because "we are about to stop nginx" reads alarming otherwise.
  if [ -d /etc/nginx ] && grep -rqE 'proxy_pass[^;]*(5678|n8n)' /etc/nginx 2>/dev/null; then
    NGINX_PROXIES_N8N=1
    ok "host nginx detected ($(systemctl is-active nginx)) and it proxies to n8n — its config is backed up, then it is stopped and disabled"
  else
    ok "host nginx detected ($(systemctl is-active nginx)) but nothing in it proxies to n8n"
    say "  looks like the stock default site — stopping it only frees port 80."
  fi
else
  info "no host nginx under systemd — nothing to migrate away from"
fi
if [ -d /etc/nginx ]; then
  DET_NGINX_DOMAIN="$(grep -rhE '^\s*server_name\s' /etc/nginx 2>/dev/null | tr -d ';' | awk '{for(i=2;i<=NF;i++) print $i}' \
                      | grep -E '^[a-z0-9.-]+\.[a-z]{2,}$' | head -1 || true)"
  DET_NGINX_PORT="$(grep -rhE '^\s*listen\s+[0-9]+' /etc/nginx 2>/dev/null | awk '{print $2}' | tr -d ';' | grep -E '^[0-9]+$' | grep -v 443 | head -1 || true)"
  [ -n "$DET_NGINX_DOMAIN" ] && say "nginx serves: $DET_NGINX_DOMAIN${DET_NGINX_PORT:+ on port $DET_NGINX_PORT}"
fi

# System timezone, as the last fallback for n8n's schedules.
DET_SYS_TZ="$( { timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || true; } | head -1 )"

# A previous run of this installer wins over everything — re-runs should be
# Enter-Enter-Enter.
PREV_DOMAIN="$(prev N8N_DOMAIN)"; PREV_URL="$(prev PUBLIC_URL)"
PREV_PORT="$(prev HTTP_PORT)"; PREV_HOPS="$(prev N8N_PROXY_HOPS)"
PREV_TZ="$(prev GENERIC_TIMEZONE)"; PREV_SSO="$(prev PRISM_SSO_URL)"
PREV_SLO="$(prev PRISM_SLO_URL)"; PREV_MAIL="$(prev ALLOWED_EMAIL_DOMAIN)"
PREV_TAG="$(prev N8N_TAG)"; PREV_ENTITY="$(prev SP_ENTITY_ID)"
# Re-runs must not silently switch scheme: take it from the previous PUBLIC_URL
# unless this run explicitly pinned one.
if [ -z "$PUBLIC_SCHEME_EXPLICIT" ] && [ -n "$PREV_URL" ]; then
  case "$PREV_URL" in http://*) PUBLIC_SCHEME=http ;; https://*) PUBLIC_SCHEME=https ;; esac
fi
[ -n "$PREV_DOMAIN" ] && ok "found a previous install in $INSTALL_DIR — its answers are pre-filled"

[ -n "$N8N_TAG" ] || N8N_TAG=""   # ask() fills it below

# ── 3. collect configuration ─────────────────────────────────────────────────
step "Configuration"
say "Values found on this machine are pre-filled — press Enter to accept each,"
say "or edit it in place (Ctrl-U clears the line first). Nothing is applied"
say "until you confirm at the review step."

ask N8N_DOMAIN "Public hostname" \
"The hostname your users type, and the one your ALB serves — not the EC2 private
   DNS. Example: n8n.example.com. It must already resolve to your ALB." \
  "$(first "$PREV_DOMAIN" "$DET_HOST" "$DET_NGINX_DOMAIN")" \
  "$( [ -n "$PREV_DOMAIN" ] && echo 'previous install' || { [ -n "$DET_HOST" ] && echo 'from the running n8n container' || { [ -n "$DET_NGINX_DOMAIN" ] && echo 'from your nginx config'; }; } )"

ask N8N_TAG "n8n version to run" \
"The image tag. Keeping the tag you already run is the safe choice — SSO does
   not require an upgrade. Current 2.x stable is ${DEFAULT_N8N_TAG}." \
  "$(first "$PREV_TAG" "$RUNNING_TAG" "$DEFAULT_N8N_TAG")" \
  "$( [ -n "$RUNNING_TAG" ] && echo 'the tag running now' )"

DEF_HOPS="$(first "$PREV_HOPS" "$DET_HOPS" 2)"
[ "$DEF_HOPS" = 2 ] && DEF_ALB=yes || DEF_ALB=no
ask BEHIND_ALB "Is an AWS ALB (or any load balancer) in front of this host?" \
"'yes' if TLS terminates at an ALB that forwards to this box — the normal EC2
   setup. Sets N8N_PROXY_HOPS: 2 for ALB+nginx, 1 for nginx alone. Wrong value
   makes n8n log the wrong client IPs and hand out wrong webhook URLs." \
  "$DEF_ALB" "$( [ -n "$DET_HOPS" ] && echo "n8n currently runs with N8N_PROXY_HOPS=$DET_HOPS" )"

case "$BEHIND_ALB" in y|Y|yes|YES|true|1) PROXY_HOPS=2 ;; *) PROXY_HOPS=1 ;; esac

ask HTTP_PORT "Host port for nginx" \
"The port the nginx container binds on this host — where your ALB target group
   sends traffic. 80 unless you deliberately run it elsewhere." \
  "$(first "$PREV_PORT" "$DET_NGINX_PORT" 80)" \
  "$( [ -n "$DET_NGINX_PORT" ] && echo 'the port host nginx listens on' )"

PUBLIC_URL="${PUBLIC_SCHEME}://${N8N_DOMAIN}"
[ "$PUBLIC_SCHEME" = "http" ] && [ "$HTTP_PORT" != "80" ] && PUBLIC_URL="${PUBLIC_URL}:${HTTP_PORT}"

printf '\n%s┌─ Create the Prism application first ─────────────────────────────┐%s\n' "$B$C" "$X"
cat <<EOF
   In the Prism admin portal: Applications → Custom Applications → Create.
   Prism issues SAML 2.0 to third-party apps, so these are the values it wants:

     Application Name : n8n
     Client ID        : ${PUBLIC_URL}/auth/realms/n8n
                        (cannot be changed later — copy it exactly)
     ACS URL          : ${PUBLIC_URL}/auth/realms/n8n/broker/prism/endpoint
     Name ID Format   : Persistent
     Single Logout URL: ${PUBLIC_URL}/auth/realms/n8n/broker/prism/endpoint
     Relay State      : ${PUBLIC_URL}/

   Save it, then come back with the metadata URL (easiest — it carries the SSO
   URL and the certificate), or the SSO URL and certificate separately.

   If Prism is itself Keycloak, it may hand you a "Login URL" ending in
   /protocol/saml/clients/<name>. That is the IdP-INITIATED entry point and is
   NOT the SSO URL to use here; the right one is the realm's SingleSignOnService,
   /realms/<realm>/protocol/saml, which the metadata gives you automatically.
EOF
printf '%s└──────────────────────────────────────────────────────────────────┘%s\n' "$B$C" "$X"

# Prism hands you IdP metadata as well as a separate SSO URL and certificate.
# The metadata contains both, so taking it saves transcribing them — but note
# it is read HERE, at install time, and the values are written into the realm.
# Keycloak does not resolve the SSO URL from metadata at login time (verified on
# 26.7.1: with a metadata URL configured, the AuthnRequest still goes to the
# stored singleSignOnServiceUrl), so this is an input convenience, not a
# runtime dependency.
PRISM_META="${PRISM_META:-}"
META_SSO=""; META_SLO=""; META_CERT=""
if [ -z "$PRISM_SIGNING_CERT" ] && [ -z "$PREV_SSO" ]; then
  ask PRISM_META "Prism IdP metadata — URL or file path" \
"The easiest route: Prism's IdP Metadata for this application, either as a URL
   or a downloaded .xml file. The SSO URL and the signing certificate are both
   inside it, so supplying this means you do not have to transcribe either.
   Enter '-' to provide the SSO URL and certificate separately instead." "-"
fi
if [ -n "$PRISM_META" ] && [ "$PRISM_META" != "-" ]; then
  META_FILE="$INSTALL_DIR/.prism-metadata.xml"
  mkdir -p "$INSTALL_DIR"
  case "$PRISM_META" in
    http://*|https://*) curl -fsSL "$PRISM_META" -o "$META_FILE" || die "could not fetch $PRISM_META" ;;
    *) [ -f "$PRISM_META" ] || die "metadata file not found: $PRISM_META"; cp "$PRISM_META" "$META_FILE" ;;
  esac
  eval "$(python3 - "$META_FILE" <<'PY'
import sys, re, shlex, xml.etree.ElementTree as ET
MD="{urn:oasis:names:tc:SAML:2.0:metadata}"; DS="{http://www.w3.org/2000/09/xmldsig#}"
try:
    x=ET.fromstring(open(sys.argv[1],'rb').read())
except Exception as e:
    print("die=%s" % shlex.quote("metadata is not valid XML: %s" % e)); raise SystemExit
d=x if x.tag==MD+"IDPSSODescriptor" else x.find(".//"+MD+"IDPSSODescriptor")
if d is None:
    print("die=%s" % shlex.quote("no IDPSSODescriptor in that document — is it IdP metadata?")); raise SystemExit
cert=""
for kd in d.findall(MD+"KeyDescriptor"):
    if kd.get("use","signing")!="signing": continue
    c=kd.find(".//"+DS+"X509Certificate")
    if c is not None and c.text: cert=re.sub(r'\s+','',c.text); break
sso=""
for b in ("urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
          "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"):
    for s in d.findall(MD+"SingleSignOnService"):
        if s.get("Binding")==b: sso=s.get("Location") or ""; break
    if sso: break
slo=""
for s in d.findall(MD+"SingleLogoutService"):
    slo=s.get("Location") or ""; break
for k,v in (("META_SSO",sso),("META_SLO",slo),("META_CERT",cert)):
    print("%s=%s" % (k, shlex.quote(v)))
PY
  )"
  [ -n "${die:-}" ] && die "$die"
  [ -n "$META_CERT" ] || die "that metadata contains no signing certificate — Keycloak cannot verify Prism's assertions without one"
  [ -n "$META_SSO" ]  || die "that metadata contains no SingleSignOnService location"
  ok "metadata parsed: SSO URL and a ${#META_CERT}-char signing certificate$( [ -n "$META_SLO" ] && echo ', plus a logout URL')"
  PRISM_SIGNING_CERT="$META_CERT"
  [ -z "$PRISM_SLO_URL" ] && PRISM_SLO_URL="$META_SLO"
  case "$PRISM_META" in http://*|https://*) PRISM_META_URL="$PRISM_META" ;; esac
fi
PRISM_META_URL="${PRISM_META_URL:-}"

ask SP_ENTITY_ID "SP entity ID — must match the client ID registered in Prism" \
"The identifier this Keycloak puts in the AuthnRequest's Issuer. Prism matches it
   against the SAML client it has for n8n; if they differ, Prism rejects the login
   before showing a page. Suggested value below — but if whoever configured Prism
   used something else (a short name like 'automata', say), enter THEIR value." \
  "$(first "$PREV_ENTITY" "${PUBLIC_URL}/auth/realms/n8n")" \
  "$( [ -n "$PREV_ENTITY" ] && echo 'previous install' || echo 'derived from your hostname' )"

ask PRISM_SSO_URL "Prism SSO URL" \
"From the Custom Application you created in Prism — the endpoint Keycloak POSTs
   its SAML AuthnRequest to. Required either way: Keycloak stores this, it does
   not look it up in metadata at login time." \
  "$(first "$PREV_SSO" "$META_SSO")" \
  "$( [ -n "$META_SSO" ] && echo 'read from the metadata you supplied' || { [ -n "$PREV_SSO" ] && echo 'previous install'; } )"

ask PRISM_SLO_URL "Prism Single Logout URL (optional)" \
"Prism's logout endpoint, if it shows one — lets 'log out' end the Prism session
   too. Leave as '-' to skip; you can add it later in the Keycloak console." \
  "$(first "$PREV_SLO" "-")" "$( [ -n "$PREV_SLO" ] && echo 'previous install' )"
[ "$PRISM_SLO_URL" = "-" ] && PRISM_SLO_URL=""

CERT_CACHE="$INSTALL_DIR/keycloak/prism-cert.b64"
if [ -z "$PRISM_SIGNING_CERT" ] && [ -s "$CERT_CACHE" ] && confirm "Reuse the Prism certificate from the previous install?" y; then
  PRISM_SIGNING_CERT="$(cat "$CERT_CACHE")"
  ok "certificate reused (${#PRISM_SIGNING_CERT} base64 chars)"
fi
if [ -z "$PRISM_SIGNING_CERT" ]; then
  # Pre-fill with the most likely download location if exactly one .crt/.pem/.cer
  # is lying around where people usually scp them.
  DET_CERT="$(ls -1t /root/*.crt /root/*.pem /root/*.cer /home/*/*.crt /home/*/*.pem /home/*/*.cer /tmp/*.crt /tmp/*.pem 2>/dev/null | head -1 || true)"
  ask PRISM_CERT_INPUT "Path to Prism's X.509 signing certificate" \
"Download it from the same Prism application page and scp it to this host, then
   give the path here (e.g. /root/prism.crt). Keycloak uses it to verify that an
   assertion really came from Prism — without it, anyone could forge a login.
   PEM or bare base64 both work." \
    "$DET_CERT" "$( [ -n "$DET_CERT" ] && echo 'certificate file found on this host' )"
  [ -f "$PRISM_CERT_INPUT" ] || die "certificate file not found: $PRISM_CERT_INPUT"
  # Accept PEM, DER-in-PEM-wrapper, or a bare base64 blob; emit one clean line.
  PRISM_SIGNING_CERT="$(grep -v -- '-----' "$PRISM_CERT_INPUT" | tr -d ' \t\r\n')"
  [ ${#PRISM_SIGNING_CERT} -ge 100 ] || die "that file does not look like a certificate (only ${#PRISM_SIGNING_CERT} base64 characters in it)"
  ok "certificate read (${#PRISM_SIGNING_CERT} base64 chars)"
fi

ask ALLOWED_EMAIL_DOMAIN "Email domain allowed to reach n8n" \
"Only users whose Prism email ends in this domain get past the gate — the last
   line of defence if a Prism account outside your org ever exists. To gate on a
   Prism *group* instead, enter '-' and set allowed_groups in
   ${INSTALL_DIR}/oauth2-proxy.cfg afterwards." \
  "$(first "$PREV_MAIL" "${N8N_DOMAIN#*.}")" "derived from your hostname"

ask GENERIC_TIMEZONE "Timezone for n8n schedules" \
"IANA name, e.g. Asia/Kolkata or UTC. Cron and Schedule nodes fire against it." \
  "$(first "$PREV_TZ" "$DET_TZ" "$DET_SYS_TZ" UTC)" \
  "$( [ -n "$DET_TZ" ] && echo 'from the running n8n container' || { [ -n "$DET_SYS_TZ" ] && echo 'this host'\''s timezone'; } )"

# Secrets: never asked for — generated on the first run, then REUSED on every
# later one. Regenerating them would be quietly catastrophic:
#   • Postgres only ever applies POSTGRES_PASSWORD when it initialises an empty
#     data directory. A new KC_DB_PASSWORD against the existing keycloak_db
#     volume means Keycloak can never connect again ("password authentication
#     failed for user keycloak") and crash-loops forever.
#   • The realm is imported once; a new OIDC client secret would no longer match
#     the one stored in that realm, and every login would fail at the token step.
#
# NOT plain `openssl rand -base64 32` for the cookie: that alphabet includes '+'
# and '/', which oauth2-proxy cannot URL-base64-decode, so it treats the string
# as 44 raw bytes and dies with "cookie_secret must be 16, 24, or 32 bytes".
# A 32-char alphanumeric string is unambiguous however it is read.
SECRETS_REUSED=""
COOKIE_SECRET="$(first "$(prev OAUTH2_PROXY_COOKIE_SECRET)" "$(randpw)")"
OIDC_CLIENT_SECRET="$(first "$(prev OIDC_CLIENT_SECRET)" "$(randpw)")"
KC_DB_PASSWORD="$(first "$(prev KC_DB_PASSWORD)" "$(randpw)")"
KC_ADMIN_PASSWORD="$(first "$(prev KC_ADMIN_PASSWORD)" "$(randpw)")"
[ -n "$(prev KC_DB_PASSWORD)" ] && SECRETS_REUSED=1

# The one unrecoverable combination: a Keycloak database that already exists,
# with no .env left to tell us its password.
KC_VOLUME="${COMPOSE_PROJECT}_keycloak_db"
if [ -z "$SECRETS_REUSED" ] && docker volume inspect "$KC_VOLUME" >/dev/null 2>&1; then
  warn "the Keycloak database volume '$KC_VOLUME' already exists, but there is no"
  warn ".env here holding its password — Keycloak will not be able to log in to it."
  if confirm "Delete that volume and let Keycloak rebuild the realm from scratch? (n8n data is NOT touched)"; then
    docker volume rm "$KC_VOLUME" >/dev/null && ok "removed $KC_VOLUME"
  else
    die "cannot continue: restore the old .env into $INSTALL_DIR, or allow the volume to be removed."
  fi
fi

printf '\n%s─ Review ─────────────────────────────────────────────────────────%s\n' "$B" "$X"
cat <<EOF
   Public URL        : ${PUBLIC_URL}
   nginx binds       : host port ${HTTP_PORT}  (moved into Compose)
   n8n image         : docker.n8n.io/n8nio/n8n:${N8N_TAG}
   n8n data          : ${N8N_DATA_KIND} ${N8N_DATA_SOURCE}  (reused, backed up first)
   N8N_PROXY_HOPS    : ${PROXY_HOPS}
   SP entity ID      : ${SP_ENTITY_ID}
   Prism SSO URL     : ${PRISM_SSO_URL}
   Prism SLO URL     : ${PRISM_SLO_URL:-（none）}
   Allowed email dom : ${ALLOWED_EMAIL_DOMAIN}
   Install directory : ${INSTALL_DIR}
   Secrets           : $([ -n "$SECRETS_REUSED" ] && echo "reused from the previous install (must not change)" || echo "generated (cookie, OIDC client, Keycloak admin + DB)")
EOF
printf '%s──────────────────────────────────────────────────────────────────%s\n' "$B" "$X"
confirm "Proceed? This stops host nginx and the current n8n container." || die "aborted by user — nothing was changed."

# ── 4. fetch the repo ────────────────────────────────────────────────────────
step "Fetching n8n-warden ($REPO_REF)"
mkdir -p "$INSTALL_DIR"
SRC="$INSTALL_DIR/src"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch --depth 1 origin "$REPO_REF" -q && git -C "$SRC" reset --hard -q "origin/$REPO_REF"
  ok "updated $SRC"
else
  rm -rf "$SRC"; git clone --depth 1 --branch "$REPO_REF" -q "$REPO_URL" "$SRC"
  ok "cloned into $SRC"
fi
SSO="$SRC/SSO-SETUP"
for f in "$SSO/oauth2-proxy.cfg" "$SSO/nginx-alb/n8n-sso.conf" "$SSO/prism-saml/realm-n8n.json"; do
  [ -f "$f" ] || die "repo is missing $f — wrong ref?"
done

# ── 5. back up ───────────────────────────────────────────────────────────────
step "Backing up"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$INSTALL_DIR/backups/$STAMP"
mkdir -p "$BACKUP"
if [ -n "$SKIP_BACKUP" ]; then
  warn "SKIP_BACKUP set — no backup taken"
elif [ -n "$N8N_CONTAINER" ]; then
  say "archiving n8n data (workflows, credentials, encryption key)…"
  if [ "$N8N_DATA_KIND" = "volume" ]; then
    docker run --rm -v "$N8N_DATA_SOURCE:/data:ro" -v "$BACKUP:/backup" "$IMG_NGINX" \
      tar czf /backup/n8n-data.tgz -C /data . 2>/dev/null
  else
    tar czf "$BACKUP/n8n-data.tgz" -C "$N8N_DATA_SOURCE" .
  fi
  ok "$(du -h "$BACKUP/n8n-data.tgz" | cut -f1) → $BACKUP/n8n-data.tgz"
  docker inspect "$N8N_CONTAINER" > "$BACKUP/n8n-container.json" 2>/dev/null || true
  [ -n "$OLD_COMPOSE_FILES" ] && [ -f "${OLD_COMPOSE_FILES%%,*}" ] && cp "${OLD_COMPOSE_FILES%%,*}" "$BACKUP/old-docker-compose.yml" || true
fi
[ -d /etc/nginx ] && { tar czf "$BACKUP/etc-nginx.tgz" -C /etc nginx 2>/dev/null || true; ok "host nginx config → $BACKUP/etc-nginx.tgz"; }
chmod -R go-rwx "$BACKUP"

# ── 6. render the stack ──────────────────────────────────────────────────────
step "Rendering the stack into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/nginx" "$INSTALL_DIR/keycloak"
# If a config file was deleted while its container was still running, Docker will
# have recreated the bind-mount path as an empty DIRECTORY. Clear those, or the
# renders below fail with a baffling "Is a directory".
for f in "$INSTALL_DIR/nginx/n8n-sso.conf" "$INSTALL_DIR/keycloak/realm-n8n.json" "$INSTALL_DIR/oauth2-proxy.cfg"; do
  [ -d "$f" ] && { rmdir "$f" 2>/dev/null || rm -rf "$f"; }
done

# nginx: same security boundary as the repo's reviewed config, retargeted at
# compose service names and made the default server (the ALB sets Host).
sed -e 's#^    set \$n8n_upstream .*#    set $n8n_upstream      "http://n8n:5678";#' \
    -e 's#^    set \$oauth2_upstream .*#    set $oauth2_upstream   "http://oauth2-proxy:4180";#' \
    -e 's#^    set \$keycloak_upstream .*#    set $keycloak_upstream "http://keycloak:8080";#' \
    -e 's#^    listen 80;#    listen 80 default_server;#' \
    -e "s#^    server_name .*#    server_name ${N8N_DOMAIN};#" \
    "$SSO/nginx-alb/n8n-sso.conf" > "$INSTALL_DIR/nginx/n8n-sso.conf"
for u in 'http://n8n:5678' 'http://oauth2-proxy:4180' 'http://keycloak:8080'; do
  grep -q "$u" "$INSTALL_DIR/nginx/n8n-sso.conf" || die "nginx template did not patch cleanly (missing $u)"
done
ok "nginx/n8n-sso.conf"

# oauth2-proxy: repo config + this deployment's allow-list and cookie policy.
cp "$SSO/oauth2-proxy.cfg" "$INSTALL_DIR/oauth2-proxy.cfg"
if [ "$ALLOWED_EMAIL_DOMAIN" = "-" ]; then
  sed -i 's#^email_domains = .*#email_domains = ["*"]   # gated by allowed_groups below instead#' "$INSTALL_DIR/oauth2-proxy.cfg"
  warn "email_domains left open — set allowed_groups in $INSTALL_DIR/oauth2-proxy.cfg before going live"
else
  sed -i "s#^email_domains = .*#email_domains = [\"${ALLOWED_EMAIL_DOMAIN}\"]#" "$INSTALL_DIR/oauth2-proxy.cfg"
fi
[ "$PUBLIC_SCHEME" = "https" ] || sed -i 's#^cookie_secure = true#cookie_secure = false#' "$INSTALL_DIR/oauth2-proxy.cfg"
grep -q "^email_domains" "$INSTALL_DIR/oauth2-proxy.cfg" || die "oauth2-proxy.cfg did not patch cleanly"
ok "oauth2-proxy.cfg (email_domains = ${ALLOWED_EMAIL_DOMAIN})"

# Keycloak realm: fill the five REPLACE-ME values. Keycloak does no env
# substitution inside realm imports, so the values must land in the file.
python3 - "$SSO/prism-saml/realm-n8n.json" "$INSTALL_DIR/keycloak/realm-n8n.json" <<PY
import json, sys
src, dst = sys.argv[1], sys.argv[2]
d = json.load(open(src))
c = d["clients"][0]
c["secret"] = """${OIDC_CLIENT_SECRET}"""
c["redirectUris"] = ["""${PUBLIC_URL}/oauth2/callback"""]
cfg = d["identityProviders"][0]["config"]
cfg["entityId"] = """${SP_ENTITY_ID}"""
cfg["singleSignOnServiceUrl"] = """${PRISM_SSO_URL}"""
cfg["signingCertificate"] = """${PRISM_SIGNING_CERT}"""
meta_url = """${PRISM_META_URL}"""
if meta_url:
    # Belt and braces: the certificate above is already extracted and stored, so
    # login works regardless. This additionally lets Keycloak re-read the
    # descriptor so a rotated signing key is picked up without a redeploy.
    cfg["metadataDescriptorUrl"] = meta_url
    cfg["useMetadataDescriptorUrl"] = "true"
slo = """${PRISM_SLO_URL}"""
if slo: cfg["singleLogoutServiceUrl"] = slo
else: cfg.pop("singleLogoutServiceUrl", None)
json.dump(d, open(dst, "w"), indent=2)
leftover = [l for l in open(dst) if "REPLACE-ME" in l]
sys.exit("realm still contains REPLACE-ME: %s" % leftover if leftover else 0)
PY
ok "keycloak/realm-n8n.json"

cat > "$INSTALL_DIR/.env" <<EOF
# Generated by install.sh on $(date -Is). Contains secrets — chmod 600.
N8N_DOMAIN=${N8N_DOMAIN}
PUBLIC_URL=${PUBLIC_URL}
HTTP_PORT=${HTTP_PORT}
N8N_TAG=${N8N_TAG}
N8N_PROXY_HOPS=${PROXY_HOPS}
GENERIC_TIMEZONE=${GENERIC_TIMEZONE}
N8N_SECURE_COOKIE=$([ "$PUBLIC_SCHEME" = https ] && echo true || echo false)

OIDC_ISSUER_URL=${PUBLIC_URL}/auth/realms/n8n
OIDC_CLIENT_ID=n8n-gateway
OIDC_CLIENT_SECRET=${OIDC_CLIENT_SECRET}
OAUTH2_PROXY_COOKIE_SECRET=${COOKIE_SECRET}

KC_DB_PASSWORD=${KC_DB_PASSWORD}
KC_ADMIN_USER=admin
KC_ADMIN_PASSWORD=${KC_ADMIN_PASSWORD}

# Not read by compose — kept so a re-run of install.sh can pre-fill your answers.
PRISM_SSO_URL=${PRISM_SSO_URL}
PRISM_SLO_URL=${PRISM_SLO_URL}
ALLOWED_EMAIL_DOMAIN=${ALLOWED_EMAIL_DOMAIN}
SP_ENTITY_ID=${SP_ENTITY_ID}
EOF
chmod 600 "$INSTALL_DIR/.env"
printf '%s' "$PRISM_SIGNING_CERT" > "$INSTALL_DIR/keycloak/prism-cert.b64"
chmod 600 "$INSTALL_DIR/keycloak/prism-cert.b64"
ok ".env (chmod 600) + cached Prism certificate"

# n8n's data mount: reuse whatever the old deployment used, verbatim.
if [ "$N8N_DATA_KIND" = "volume" ]; then
  N8N_VOLUME_BLOCK=$'volumes:\n  keycloak_db:\n  n8n_data:\n    external: true\n    name: '"$N8N_DATA_SOURCE"
  N8N_MOUNT="      - n8n_data:/home/node/.n8n"
else
  N8N_VOLUME_BLOCK=$'volumes:\n  keycloak_db:'
  N8N_MOUNT="      - ${N8N_DATA_SOURCE}:/home/node/.n8n"
fi
EXTRA_HOSTS_BLOCK=""
[ -n "$EXTRA_HOST_IP" ] && EXTRA_HOSTS_BLOCK=$'    extra_hosts:\n      - "'"${N8N_DOMAIN}:${EXTRA_HOST_IP}"'"'

cat > "$INSTALL_DIR/docker-compose.yml" <<EOF
# Generated by install.sh — n8n behind CloudKeeper Prism SSO.
# Edit and re-run 'docker compose up -d' from $INSTALL_DIR.
#
#   ALB ─▶ nginx ─▶ oauth2-proxy ─▶ keycloak ─▶ Prism (SAML)
#                └▶ n8n
#
# Only nginx publishes a port. Everything else is reachable solely on the
# private compose network — n8n is never internet-facing.

services:
  nginx:
    image: ${IMG_NGINX}
    container_name: ${COMPOSE_PROJECT}-nginx
    restart: unless-stopped
    ports:
      - "${HTTP_PORT}:80"
    volumes:
      - ./nginx/n8n-sso.conf:/etc/nginx/conf.d/default.conf:ro
      - nginx_logs:/var/log/nginx
    # Intentionally NOT waiting for the others to be healthy. nginx resolves its
    # upstreams per request, so it can and should come up first: oauth2-proxy
    # fetches its OIDC issuer through nginx, and would never start if nginx were
    # waiting on oauth2-proxy.
    depends_on:
      keycloak:
        condition: service_started
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- --header='Host: ${N8N_DOMAIN}' http://127.0.0.1/healthz >/dev/null || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 20s

  oauth2-proxy:
    image: ${IMG_OAUTH2_PROXY}
    container_name: ${COMPOSE_PROJECT}-oauth2-proxy
    restart: unless-stopped
    command: --config=/oauth2-proxy.cfg
    volumes:
      - ./oauth2-proxy.cfg:/oauth2-proxy.cfg:ro
    environment:
      OAUTH2_PROXY_CLIENT_ID: \${OIDC_CLIENT_ID}
      OAUTH2_PROXY_CLIENT_SECRET: \${OIDC_CLIENT_SECRET}
      OAUTH2_PROXY_COOKIE_SECRET: \${OAUTH2_PROXY_COOKIE_SECRET}
      OAUTH2_PROXY_OIDC_ISSUER_URL: \${OIDC_ISSUER_URL}
      OAUTH2_PROXY_REDIRECT_URL: \${PUBLIC_URL}/oauth2/callback
      OAUTH2_PROXY_COOKIE_DOMAINS: \${N8N_DOMAIN}
      OAUTH2_PROXY_WHITELIST_DOMAINS: \${N8N_DOMAIN}
      # Front channel public, back channel private.
      #
      # Left to itself, oauth2-proxy discovers every endpoint from the PUBLIC
      # issuer URL and then calls them over the public name — so it could not
      # start until it could reach your own ALB from inside this host and loop
      # back in. That hairpin is slow at best and, on a private/split-horizon
      # DNS setup, impossible; oauth2-proxy then crash-loops on
      # "failed to discover OIDC configuration" with the whole editor 500ing.
      #
      # So: the browser-facing login URL stays public (it must — the user's
      # browser follows it), while token, userinfo and JWKS are fetched over the
      # compose network. The issuer stays the public string, which is what
      # Keycloak stamps into its tokens, so validation is unchanged.
      OAUTH2_PROXY_SKIP_OIDC_DISCOVERY: "true"
      OAUTH2_PROXY_LOGIN_URL: \${PUBLIC_URL}/auth/realms/n8n/protocol/openid-connect/auth
      OAUTH2_PROXY_REDEEM_URL: http://keycloak:8080/auth/realms/n8n/protocol/openid-connect/token
      OAUTH2_PROXY_PROFILE_URL: http://keycloak:8080/auth/realms/n8n/protocol/openid-connect/userinfo
      OAUTH2_PROXY_OIDC_JWKS_URL: http://keycloak:8080/auth/realms/n8n/protocol/openid-connect/certs
${EXTRA_HOSTS_BLOCK}
    depends_on:
      keycloak:
        condition: service_healthy
    expose:
      - "4180"

  keycloak:
    image: ${IMG_KEYCLOAK}
    container_name: ${COMPOSE_PROJECT}-keycloak
    restart: unless-stopped
    command: start --import-realm
    environment:
      KC_DB: postgres
      KC_DB_URL: jdbc:postgresql://keycloak-db:5432/keycloak
      KC_DB_USERNAME: keycloak
      KC_DB_PASSWORD: \${KC_DB_PASSWORD}
      KC_HOSTNAME: \${PUBLIC_URL}/auth
      # Keycloak dropped the /auth context path in v17 (the Quarkus rewrite);
      # this build option puts it back. We need SOME prefix so Keycloak can share
      # one hostname with n8n — no second DNS record, no second certificate — and
      # /auth is the conventional one. n8n serves nothing under it.
      #
      # CAUTION: http-relative-path is a BUILD-TIME option. It applies here only
      # because the command above is `start` WITHOUT --optimized, so Keycloak
      # re-augments itself at boot (that is most of the first-boot minute). Add
      # --optimized without running `kc.sh build` first and Keycloak silently
      # serves at / again — every URL below, the realm entityId, and the ACS URL
      # you gave Prism all break at once.
      KC_HTTP_RELATIVE_PATH: /auth
      KC_HTTP_ENABLED: "true"
      KC_PROXY_HEADERS: xforwarded
      KC_HEALTH_ENABLED: "true"
      KC_BOOTSTRAP_ADMIN_USERNAME: \${KC_ADMIN_USER}
      KC_BOOTSTRAP_ADMIN_PASSWORD: \${KC_ADMIN_PASSWORD}
    volumes:
      - ./keycloak/realm-n8n.json:/opt/keycloak/data/import/realm-n8n.json:ro
    depends_on:
      keycloak-db:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "exec 3<>/dev/tcp/127.0.0.1/9000; printf 'GET /auth/health/ready HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n' >&3; grep -q '\"status\": \"UP\"' <&3"]
      interval: 15s
      timeout: 5s
      retries: 20
      start_period: 90s
    expose:
      - "8080"

  keycloak-db:
    image: ${IMG_POSTGRES}
    container_name: ${COMPOSE_PROJECT}-keycloak-db
    restart: unless-stopped
    environment:
      POSTGRES_DB: keycloak
      POSTGRES_USER: keycloak
      POSTGRES_PASSWORD: \${KC_DB_PASSWORD}
    volumes:
      - keycloak_db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U keycloak -d keycloak"]
      interval: 10s
      timeout: 5s
      retries: 10

  n8n:
    image: docker.n8n.io/n8nio/n8n:\${N8N_TAG}
    container_name: ${COMPOSE_PROJECT}-n8n
    restart: unless-stopped
    environment:
      - N8N_HOST=\${N8N_DOMAIN}
      - N8N_PROTOCOL=$(first "$DET_PROTO" "$PUBLIC_SCHEME" http)
      - N8N_PORT=5678
      - N8N_EDITOR_BASE_URL=\${PUBLIC_URL}
      - WEBHOOK_URL=\${PUBLIC_URL}
      - N8N_SECURE_COOKIE=\${N8N_SECURE_COOKIE}
      - N8N_PROXY_HOPS=\${N8N_PROXY_HOPS}
      - GENERIC_TIMEZONE=\${GENERIC_TIMEZONE}
      - TZ=\${GENERIC_TIMEZONE}
${N8N_CARRY}
    volumes:
${N8N_MOUNT}
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:5678/healthz || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 60s
    expose:
      - "5678"

${N8N_VOLUME_BLOCK}
  nginx_logs:
EOF
docker compose -f "$INSTALL_DIR/docker-compose.yml" --env-file "$INSTALL_DIR/.env" config -q \
  || die "generated compose file is invalid — see $INSTALL_DIR/docker-compose.yml"
ok "docker-compose.yml (validated)"

# ── 7. cut over ──────────────────────────────────────────────────────────────
step "Cutting over"
if [ "$HOST_NGINX" = "yes" ] && [ "$HTTP_PORT" = "80" ]; then
  systemctl stop nginx || true
  systemctl disable nginx >/dev/null 2>&1 || true
  ok "host nginx stopped and disabled (config backed up; re-enable with: systemctl enable --now nginx)"
fi
if [ -n "$N8N_CONTAINER" ] && [ "$N8N_CONTAINER" != "${COMPOSE_PROJECT}-n8n" ]; then
  docker stop "$N8N_CONTAINER" >/dev/null && ok "stopped old container '$N8N_CONTAINER' (kept, not removed — its data volume is reused)"
fi

cd "$INSTALL_DIR"
say "starting the stack (first Keycloak boot imports the realm; ~1-2 min)…"
docker compose -p "$COMPOSE_PROJECT" up -d --remove-orphans
for i in $(seq 1 60); do
  st="$(docker inspect -f '{{.State.Health.Status}}' "${COMPOSE_PROJECT}-keycloak" 2>/dev/null || echo starting)"
  [ "$st" = healthy ] && break
  [ "$st" = unhealthy ] && die "Keycloak failed to become healthy — docker compose -p $COMPOSE_PROJECT logs keycloak"
  sleep 5
done
[ "$st" = healthy ] || die "Keycloak did not become healthy in time — docker compose -p $COMPOSE_PROJECT logs keycloak"
ok "Keycloak is up"

H=(-H "Host: ${N8N_DOMAIN}")
[ "$PUBLIC_SCHEME" = https ] && H+=(-H "X-Forwarded-Proto: https")
BASE="http://127.0.0.1:${HTTP_PORT}"
DISCOVERY="$BASE/auth/realms/n8n/.well-known/openid-configuration"

# Keycloak answers /health/ready UP roughly 12 seconds BEFORE it finishes
# importing the realm — so "healthy" does not mean "realm usable", and anything
# that touches /auth/realms/n8n in that window gets a 404/503. The realm's own
# discovery document is the only honest readiness signal.
say "waiting for the realm import to land…"
ISSUER_SEEN=""
for i in $(seq 1 40); do
  ISSUER_SEEN="$(curl -s "${H[@]}" "$DISCOVERY" 2>/dev/null | sed -n 's/.*"issuer":"\([^"]*\)".*/\1/p' || true)"
  [ -n "$ISSUER_SEEN" ] && break
  sleep 3
done
[ -n "$ISSUER_SEEN" ] && ok "realm 'n8n' imported and serving discovery" \
  || warn "realm discovery still not answering — check: docker compose -p $COMPOSE_PROJECT logs keycloak"

# oauth2-proxy fetches the OIDC discovery document over the PUBLIC url, through
# nginx — so it can only finish starting once nginx and Keycloak are both live.
# Until then the gated routes answer 500 (fail-closed, which is the right way to
# fail). Wait for the gate to actually answer before judging anything.
say "waiting for the gate (oauth2-proxy fetches its issuer through nginx)…"
GATE=""
for i in $(seq 1 40); do
  GATE="$(curl -s -o /dev/null -w '%{http_code}' "${H[@]}" "$BASE/" || true)"
  [ "$GATE" = 302 ] && break
  sleep 3
done
if [ "$GATE" = 302 ]; then
  ok "gate is live"
else
  warn "the gate is still answering $GATE after 2 minutes"
  warn "almost always this means ${PUBLIC_URL} is not reachable from this host."
  warn "oauth2-proxy's last words:"
  docker logs "${COMPOSE_PROJECT}-oauth2-proxy" 2>&1 | tail -5 | sed 's/^/     /'
  warn "fix: make ${N8N_DOMAIN} resolve here (ALB hairpin or /etc/hosts), or re-run"
  warn "with EXTRA_HOST_IP=<ip that serves ${N8N_DOMAIN}>"
fi

# ── 8. verify ────────────────────────────────────────────────────────────────
step "Verifying"
PASS=0; FAIL=0
check() { # check "name" "expected" "actual"
  if [ "$2" = "$3" ]; then ok "$1"; PASS=$((PASS+1));
  else warn "$1 — expected $2, got $3"; FAIL=$((FAIL+1)); fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "${H[@]}" "$@" || true; }

check "health endpoint is un-gated"        200 "$(code "$BASE/healthz")"
check "editor is gated (302 → sign-in)"    302 "$(code "$BASE/")"
check "webhook path bypasses the gate"     404 "$(code -X POST "$BASE/webhook/does-not-exist")"
check "API path bypasses the gate"         401 "$(code "$BASE/api/v1/workflows")"
check "spoofed identity header is ignored" 302 "$(code -H 'X-Auth-Request-Email: attacker@evil.test' -H 'X-Forwarded-Uri: /healthz' "$BASE/")"

LOC="$(curl -s -o /dev/null -w '%{redirect_url}' "${H[@]}" "$BASE/" || true)"
case "$LOC" in *"/oauth2/sign_in"*) ok "gate redirects to oauth2-proxy sign-in"; PASS=$((PASS+1));;
  *) warn "gate redirect looks wrong: $LOC"; FAIL=$((FAIL+1));; esac

# `|| true` on every probe: pipefail plus a failed curl would otherwise abort the
# install at the last step, after the stack is already up and fine.
ISS="$(curl -s "${H[@]}" "$DISCOVERY" 2>/dev/null | sed -n 's/.*"issuer":"\([^"]*\)".*/\1/p' || true)"
check "OIDC issuer is the public URL" "${PUBLIC_URL}/auth/realms/n8n" "$ISS"

# Deliberately does NOT follow the redirect: the Location is an absolute public
# URL, and whether this host can resolve that name is a separate question from
# whether the realm is wired correctly. The hop itself is the proof — a realm
# that fell back to Keycloak's own login form would render a page, not bounce to
# the broker.
SAMLLOC="$(curl -s -o /dev/null -w '%{redirect_url}' "${H[@]}" \
  "$BASE/auth/realms/n8n/protocol/openid-connect/auth?client_id=n8n-gateway&response_type=code&scope=openid&redirect_uri=${PUBLIC_URL}/oauth2/callback&state=t" 2>/dev/null || true)"
case "$SAMLLOC" in
  *"/broker/prism/login"*) ok "login goes straight to the Prism broker, no Keycloak login page"; PASS=$((PASS+1));;
  *) warn "expected a redirect to /broker/prism/login, got: ${SAMLLOC:-nothing}"; FAIL=$((FAIL+1));;
esac
if grep -q "$PRISM_SSO_URL" "$INSTALL_DIR/keycloak/realm-n8n.json"; then
  ok "realm posts its SAML AuthnRequest to $PRISM_SSO_URL"; PASS=$((PASS+1))
else
  warn "realm does not reference $PRISM_SSO_URL"; FAIL=$((FAIL+1))
fi

if [ -n "$N8N_CONTAINER" ]; then
  WF="$(docker exec "${COMPOSE_PROJECT}-n8n" sh -c 'ls /home/node/.n8n/ 2>/dev/null | tr "\n" " "' || true)"
  case "$WF" in *database.sqlite*|*config*) ok "existing n8n data present in the new container ($WF)"; PASS=$((PASS+1));;
    *) warn "could not confirm n8n data carried over: $WF"; FAIL=$((FAIL+1));; esac
fi

# ── 9. done ──────────────────────────────────────────────────────────────────
cat > "$INSTALL_DIR/ROLLBACK.md" <<EOF
# Rollback — undo the SSO cutover

Nothing was deleted. To go back to plain n8n + host nginx:

    docker compose -p ${COMPOSE_PROJECT} -f ${INSTALL_DIR}/docker-compose.yml down
    docker start ${N8N_CONTAINER:-<your old n8n container>}
    systemctl enable --now nginx

Your n8n data was never moved: it still lives in ${N8N_DATA_KIND} \`${N8N_DATA_SOURCE}\`.

## Backups taken $STAMP

    ${BACKUP}/n8n-data.tgz      full copy of /home/node/.n8n (workflows, credentials, encryption key)
    ${BACKUP}/etc-nginx.tgz     /etc/nginx as it was
    ${BACKUP}/n8n-container.json  the old container's full definition

Restore n8n data into a volume:

    docker run --rm -v ${N8N_DATA_SOURCE}:/data -v ${BACKUP}:/b alpine \\
      sh -c 'rm -rf /data/* && tar xzf /b/n8n-data.tgz -C /data'

## Keycloak admin console

    ${PUBLIC_URL}/auth/admin/   user: admin   password: see ${INSTALL_DIR}/.env
EOF

step "Done"
if [ "$FAIL" -eq 0 ]; then
  printf '   %s%s✔ all %d checks passed%s\n' "$G" "$B" "$PASS" "$X"
else
  printf '   %s%d passed, %d need attention%s\n' "$Y" "$PASS" "$FAIL" "$X"
fi
cat <<EOF

   ${B}Open ${PUBLIC_URL}/${X}
   You should land on Prism, authenticate there, then see n8n's own login.
   (n8n CE keeps its own login under the gate — see the README's note on the
   double login. Create/manage those accounts with n8n-warden.)

   ${B}Files${X}
     ${INSTALL_DIR}/.env                 secrets — chmod 600, back this up
     ${INSTALL_DIR}/docker-compose.yml   the stack
     ${INSTALL_DIR}/nginx/n8n-sso.conf   the path split (auth boundary)
     ${INSTALL_DIR}/ROLLBACK.md          how to undo all of this
     ${BACKUP}/                          pre-install backups

   ${B}Everyday commands${X}  (run from ${INSTALL_DIR})
     docker compose -p ${COMPOSE_PROJECT} ps
     docker compose -p ${COMPOSE_PROJECT} logs -f keycloak
     docker compose -p ${COMPOSE_PROJECT} restart nginx

   ${B}Give Prism this, or the first login fails${X}
     Prism requires signed AuthnRequests, so it needs the certificate this
     Keycloak signs them with. Hand your Prism admin the SP descriptor:

       ${PUBLIC_URL}/auth/realms/n8n/broker/prism/endpoint/descriptor

     They import it against the n8n application (or, equivalently, turn off
     "client signature required" for it). Until then Prism rejects the request
     before anyone sees a login page.

   ${B}Before you call it live${X}
     • Point the ALB target group at host port ${HTTP_PORT}, health check path /healthz$(
       [ -n "$OLD_PUBLISHED" ] && printf '\n       ↳ it currently targets %s — until you change it, the target is\n         unhealthy and the site is down. Do this now.' "$OLD_PUBLISHED")$(
       [ -n "$OLD_PUBLISHED" ] && printf '\n     • Close %s in the security group. n8n no longer listens there, but\n       leaving it open invites a direct route that skips SSO entirely.' "$OLD_PUBLISHED")
     • ALB idle timeout ≥ 3600s so the editor's WebSocket push isn't cut
     • Confirm ${PUBLIC_URL} resolves from *this host* too — oauth2-proxy fetches
       the issuer over the public URL. If it can't, re-run with
       EXTRA_HOST_IP=<alb-or-nginx-ip>.
     • Keycloak admin console: ${PUBLIC_URL}/auth/admin/ (credentials in .env)

EOF
