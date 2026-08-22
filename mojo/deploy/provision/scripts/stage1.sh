#!/bin/bash
# stage 1 — everything a launched node does between "EC2 says running" and
# "the portal answers".
#
# Stage 0 (the instance's user data, built by
# mojo/deploy/provision/nodes.py::stage0_user_data) is deliberately tiny: it
# sets the hostname, makes swap, writes var/bootstrap.conf, downloads THIS
# script from the config bucket and execs it. Everything of any size lives
# here, where it can be changed without replacing an instance — user data
# cannot be edited on a running box, and ec2_bootstrap.sh alone is 20KB
# against EC2's 16KB user-data ceiling.
#
# THE ORDER OF THE STEPS BELOW IS THE POINT, and each one is load-bearing:
#
#   1. untar the application tree FIRST — ec2_bootstrap.sh and ec2_deploy.sh
#      are files inside it, so nothing else can run until it is on disk.
#   2. ec2_bootstrap.sh — the OS: users, packages, nginx, certbot, and an
#      UNPINNED `pip install django-mojo`.
#   3. pin django-mojo to the version of the CLI that provisioned this
#      environment — AFTER bootstrap, precisely so it overwrites bootstrap's
#      unpinned install. Reversed, the unpinned install wins and the node runs
#      a different framework release than the one that built it.
#   4. render the JUST-INSTALLED framework's cron/systemd templates into
#      var/deploy. A fresh node has no previous post-deploy render to inherit.
#   5. ec2_deploy.sh — the project: nginx vhosts, systemd units, var/ layout.
#      Note it does an unconditional `cp -f` of the shipped nginx config, so
#      anything written into /etc/nginx before this point is lost here.
#   6. var/profile = "prod" — BEFORE config_sync. settings/helper.py defaults
#      to `local` unless VAR_ROOT/profile exists, so a node without this boots
#      the local profile and silently ignores every endpoint just provisioned.
#   7. the CloudWatch agent — its config comes down from the config bucket
#      already substituted with this environment's three log-group names.
#      A failed install WARNS and continues: logging is not worth failing a
#      bootstrap over, and a re-run picks it up.
#   8. config_sync LAST. It publishes var/django.conf and may restart
#      mojo-asgi when the root-sealed request role permits it — which must not
#      happen until var/profile exists, or the app comes up on settings.local.
#
# Idempotent: every step is safe to re-run, and a resumed bootstrap is a plain
# re-execution of this file.
#
# @DJANGO_MOJO_VERSION@ is substituted by the provisioning CLI before upload
# (mojo/deploy/provision/storage.py::stage1_script). An unsubstituted copy
# refuses to run rather than installing something arbitrary.

set -euo pipefail

PROJ_PATH="${PROJ_PATH:-/opt/api}"
BOOTSTRAP_CONF="${BOOTSTRAP_CONF:-${PROJ_PATH}/var/bootstrap.conf}"
STAGE1_LOG="${STAGE1_LOG:-/var/log/mojo-stage1.log}"
CW_AGENT_ETC="${CW_AGENT_ETC:-/opt/aws/amazon-cloudwatch-agent/etc}"
APP_USER="${APP_USER:-ec2-user}"
WEB_USER="${WEB_USER:-www}"
ASGI_WORKERS="${ASGI_WORKERS:-4}"

DJANGO_MOJO_VERSION="@DJANGO_MOJO_VERSION@"

exec >> "$STAGE1_LOG" 2>&1

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
warn() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN: $*"; }
die()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] FATAL: $*"; exit 1; }

trap 'die "stage1 failed at line $LINENO — see $STAGE1_LOG"' ERR

log "stage1 starting (django-mojo ${DJANGO_MOJO_VERSION})"

case "$DJANGO_MOJO_VERSION" in
    *@*) die "this stage1.sh was never substituted with a django-mojo version — \
it was copied out of the package rather than published by \`provision apply\`" ;;
esac

# ── the config the CLI left for us ───────────────────────────────────────────
# bootstrap.conf is stage 0's output: region, config bucket, config prefix, and
# the two config_sync settings. No credentials — the node reads S3 with its
# instance role.

[ -f "$BOOTSTRAP_CONF" ] || die "$BOOTSTRAP_CONF is missing — stage 0 did not run"

conf_value() { # key
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$BOOTSTRAP_CONF" \
        | tail -n 1 | tr -d "\"'" | tr -d '\r'
}

AWS_REGION="$(conf_value AWS_REGION)"
AWS_CONFIG_BUCKET="$(conf_value AWS_CONFIG_BUCKET)"
BOOTSTRAP_PREFIX="${BOOTSTRAP_PREFIX:-bootstrap}"

[ -n "$AWS_CONFIG_BUCKET" ] || die "AWS_CONFIG_BUCKET is not set in $BOOTSTRAP_CONF"

s3_get() { # object-name destination
    log "fetching s3://${AWS_CONFIG_BUCKET}/${BOOTSTRAP_PREFIX}/$1"
    aws s3 cp --region "$AWS_REGION" \
        "s3://${AWS_CONFIG_BUCKET}/${BOOTSTRAP_PREFIX}/$1" "$2"
}

# ── 1. the application tree ──────────────────────────────────────────────────
# A `git archive HEAD` tarball, not a clone: the node needs no deploy key to
# reach GitHub before it has one, and the tree is exactly the commit the
# operator provisioned from.

log "installing the application tree into $PROJ_PATH"
mkdir -p "$PROJ_PATH"
s3_get "app.tar.gz" "${PROJ_PATH}/var/app.tar.gz"
tar -xzf "${PROJ_PATH}/var/app.tar.gz" -C "$PROJ_PATH"
rm -f "${PROJ_PATH}/var/app.tar.gz"

[ -f "${PROJ_PATH}/aws/ec2_bootstrap.sh" ] || \
    die "the archive has no aws/ec2_bootstrap.sh — is this a django-mojo project?"

# The commit that tarball came from. A tarball carries no history, so this is
# the only record of which commit this node runs — `provision configure` reads
# it to wire /opt/api to origin at exactly this sha, and the deploy plane is
# `git fetch && git reset --hard` from there. Best-effort: an estate whose
# payload predates app.sha still boots, it just stays unwired until the next
# `apply` republishes the payload.
if s3_get "app.sha" "${PROJ_PATH}/var/app_sha"; then
    log "provisioned from commit $(cat "${PROJ_PATH}/var/app_sha")"
else
    rm -f "${PROJ_PATH}/var/app_sha"
    warn 'no app.sha in the boot payload — this node cannot name its own commit, so "provision configure" will not wire it to origin. Re-run "provision apply" to republish the payload.'
fi

# ── 2. the OS ────────────────────────────────────────────────────────────────

log "running ec2_bootstrap.sh (users, packages, nginx, certbot)"
bash "${PROJ_PATH}/aws/ec2_bootstrap.sh"

# ── 3. the version pin ───────────────────────────────────────────────────────
# AFTER bootstrap on purpose — bootstrap installs django-mojo unpinned, and
# this is what makes the node run the same release as the CLI that built it.

log "pinning django-mojo==${DJANGO_MOJO_VERSION}"
# A freshly published release can lag pip's Simple-index caches by minutes
# (post_deploy.sh's framework resolution block tells the whole story), so the
# pin converges with bounded retries whose retries bypass every pip cache —
# no feature-detected flags, any pip version. A fresh node has no installed
# framework to fall back on, so exhaustion stays fatal here; re-running
# provisioning is the recovery.
DJANGO_MOJO_RETRIES="${DJANGO_MOJO_RETRIES:-6}"
DJANGO_MOJO_RETRY_DELAY="${DJANGO_MOJO_RETRY_DELAY:-30}"
pin_attempt=1
pin_args=()
until pip install "${pin_args[@]}" --upgrade "django-mojo==${DJANGO_MOJO_VERSION}"; do
    if [ "$pin_attempt" -ge "$DJANGO_MOJO_RETRIES" ]; then
        log "django-mojo==${DJANGO_MOJO_VERSION} did not resolve after ${DJANGO_MOJO_RETRIES} attempts"
        exit 1
    fi
    pin_attempt=$((pin_attempt+1))
    pin_args=(--no-cache-dir)
    log "django-mojo==${DJANGO_MOJO_VERSION} not resolvable yet; retry ${pin_attempt}/${DJANGO_MOJO_RETRIES} in ${DJANGO_MOJO_RETRY_DELAY}s"
    sleep "$DJANGO_MOJO_RETRY_DELAY"
done

# ── 4. the installed node contract ───────────────────────────────────────────

# ec2_deploy.sh converges systemd from this rendered contract. Render with the
# JUST-PINNED package rather than relying on a project checkout to carry copies
# of framework units: that is what lets a fresh node receive a packaging fix
# from the same django-mojo version that provisioned it.

log "rendering the installed node contract into ${PROJ_PATH}/var/deploy"
python3 -m mojo.deploy render --dest "${PROJ_PATH}/var/deploy" \
    --project-path "$PROJ_PATH" --app-user "$APP_USER" \
    --web-user "$WEB_USER" --workers "$ASGI_WORKERS" || \
    die "the installed django-mojo templates could not be rendered"

[ -f "${PROJ_PATH}/var/deploy/systemd/mojo-asgi.service" ] || \
    die "the rendered node contract has no mojo-asgi.service"

# ── 5. the project ───────────────────────────────────────────────────────────

log "running ec2_deploy.sh (nginx vhosts, systemd units, var layout)"
bash "${PROJ_PATH}/aws/ec2_deploy.sh"

# ── 6. the profile ───────────────────────────────────────────────────────────
# Written after ec2_deploy.sh (its var/ ownership sweep would otherwise be the
# last word) and before config_sync (whose restart would otherwise boot the
# app on settings.local).

log "selecting the prod settings profile"
echo prod > "${PROJ_PATH}/var/profile"
chown "${APP_USER}:${WEB_USER}" "${PROJ_PATH}/var/profile"
chmod 640 "${PROJ_PATH}/var/profile"

# ── 7. logs off the box ──────────────────────────────────────────────────────
# The log groups, their retention and the node role's scoped logs:* grant are
# all created by the provisioning run (provision/observability.py and
# provision/identity.py). This only points the agent at them.

log "configuring the CloudWatch agent"
if command -v amazon-cloudwatch-agent-ctl >/dev/null 2>&1; then
    log "  amazon-cloudwatch-agent is already installed"
elif dnf install -y amazon-cloudwatch-agent; then
    log "  installed amazon-cloudwatch-agent"
else
    warn "could not install amazon-cloudwatch-agent — continuing without it; \
re-run stage1 once the node can reach the package repositories"
fi

if command -v amazon-cloudwatch-agent-ctl >/dev/null 2>&1; then
    mkdir -p "$CW_AGENT_ETC"
    if s3_get "cloudwatch-agent.json" "${CW_AGENT_ETC}/amazon-cloudwatch-agent.json"; then
        amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 \
            -c "file:${CW_AGENT_ETC}/amazon-cloudwatch-agent.json" -s || \
            warn "the agent refused its configuration — check $CW_AGENT_ETC"
        systemctl enable --now amazon-cloudwatch-agent || \
            warn "could not enable amazon-cloudwatch-agent"
    else
        warn "no cloudwatch-agent.json published — the agent stays unconfigured"
    fi
fi

# ── 8. the application config ────────────────────────────────────────────────
# LAST. This installs var/django.conf from the config bucket and, with
# CONFIG_SYNC_RESTART=true, restarts mojo-asgi only when the root-sealed
# request-service role permits it. Any restart is only correct once var/profile
# above says prod.
#
# BEST-EFFORT on first boot, deliberately. On a fresh account the node always
# boots before the operator's `configure` step has published django.conf, so
# a hard failure here would fail cloud-init on EVERY first boot and block the
# convergence that would have fixed it. `configure` (and config-sync.timer)
# is the authoritative convergence; this sync is just the fast path when the
# config already exists.

log "syncing django.conf with role-aware service activation"
python3 -m mojo.deploy.config_sync --config "$BOOTSTRAP_CONF" || \
    warn "django.conf is not published yet — expected on first boot; the configure step converges this node"

log "stage1 complete"
