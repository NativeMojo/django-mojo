#!/bin/bash
# Post-checkout deployment — run by update.sh after every code change, shipped
# inside the django-mojo package (mojo/deploy/scripts/) and executed through
# each project's aws/post_deploy.sh shim. The shim contract, the input table
# and the var/deploy layout are documented in django-mojo
# docs/django_developer/deploy/README.md.
#
# THIS FILE IS REPLACED MID-RUN, AND THAT IS FINE: the pip install below swaps
# this file for a new inode inside site-packages. The bash executing this run
# keeps its fd on the OLD inode, so the in-flight run completes on the code it
# started with; the NEXT run resolves the new copy through the shim's
# `locate`. On success this run snapshots the installed copy to
# ${PROJ_PATH}/var/deploy/post_deploy.sh — the shim's fallback for rolling
# back through a framework version that cannot be located.
#
# Usage: sudo bash post_deploy.sh [--framework <version>] [--migrate]
#
#   --framework <v>  install django-mojo==<v> (the fleet deploy's pinned
#                    version — pinned across nodes, never across time)
#   --migrate        run `manage.py migrate_locked --noinput` (the canary run)
#   (bare)           latest django-mojo, no migration. Bare stays valid: it is
#                    what `update.sh --manual` and provisioning invoke.
#
# Project inputs (exported by the shim; every default matches the skeleton):
#   PROJ_PATH     the deployed tree                     (default /opt/api)
#   PROBE_URL     post-restart health gate              (default http://127.0.0.1/api/version)
#   APP_USER      owns the tree and runs the engines    (default ec2-user)
#   WEB_USER      runs the asgi app behind nginx        (default www)
#   ASGI_WORKERS  uvicorn worker count (@WORKERS@)      (default 4)
#   NGINX_ETC / SYSTEMD_ETC / CRON_ETC                  test seams, prod defaults
#
# THIS SCRIPT FAILS LOUDLY ON PURPOSE. A deploy that half-worked and said
# nothing is worse than one that stops. If you are tempted to add `|| true`
# to something here, the question to answer first is: what should happen if
# this step fails? "Carry on silently" is almost never the answer.

set -euo pipefail

PROJ_PATH="${PROJ_PATH:-/opt/api}"
PROBE_URL="${PROBE_URL:-http://127.0.0.1/api/version}"
APP_USER="${APP_USER:-ec2-user}"
WEB_USER="${WEB_USER:-www}"
ASGI_WORKERS="${ASGI_WORKERS:-4}"
MOJOSEC_MODE="${MOJOSEC_MODE:-enrolled}"
MOJOSEC_DEPLOY_CRITICALITY="${MOJOSEC_DEPLOY_CRITICALITY:-enrolled}"
# Test seams: prod-identical defaults, overridable only by the shell harness.
NGINX_ETC="${NGINX_ETC:-/etc/nginx}"
SYSTEMD_ETC="${SYSTEMD_ETC:-/etc/systemd/system}"
CRON_ETC="${CRON_ETC:-/etc/cron.d}"
LOGROTATE_ETC="${LOGROTATE_ETC:-/etc/logrotate.d}"
cd "$PROJ_PATH"

DEPLOY_DIR="${PROJ_PATH}/var/deploy"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "[$(date '+%H:%M:%S')] FATAL: $*" >&2; exit 1; }

FRAMEWORK=""
MIGRATE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --framework) FRAMEWORK="${2:-}"; shift 2 || die "--framework needs a value" ;;
        --migrate)   MIGRATE=1; shift ;;
        *)           die "unknown argument: $1" ;;
    esac
done

# Copy a file that is expected to exist. A missing source is a real problem
# (someone renamed or deleted it) and should be visible, not shrugged off.
install_file() {
    local src="$1" dest="$2"
    [[ -f "$src" ]] || die "expected file is missing: $src"
    cp -f "$src" "$dest"
}

# ── dependencies ─────────────────────────────────────────────────────────────
#
# Project deps first (exact pins from requirements.txt — regenerated with
# `uv export`, django-mojo deliberately excluded from it), the framework LAST
# so its resolver can lift any of its own dependencies past those pins.
# Fleet deploys pass --framework: the orchestrator resolves the newest
# django-mojo ONCE per deploy and every node installs exactly that — pinned
# across nodes for the seconds a deploy takes, never across time. Bare runs
# install latest, so releases are never missed. Either way a failure is loud.

log "Installing project dependencies..."
pip install -r requirements.txt \
    || die "dependency install failed — refusing to restart with an incomplete environment"

if [ -n "$FRAMEWORK" ]; then
    log "Installing django-mojo==${FRAMEWORK} (fleet-pinned)..."
    pip install "django-mojo==${FRAMEWORK}" \
        || die "django-mojo ${FRAMEWORK} install failed — refusing to deploy on an unknown framework version"
else
    log "Upgrading django-mojo (latest)..."
    pip install --upgrade django-mojo \
        || die "django-mojo upgrade failed — refusing to deploy on an unknown framework version"
fi

# ── migrations ───────────────────────────────────────────────────────────────
#
# Migrations run ONLY when the deploy says so (--migrate — the canary run),
# under the real Postgres advisory lock inside migrate_locked. A per-box flag
# file cannot serialise anything; the advisory lock can — a concurrent
# migrate exits non-zero instead of racing.

if [ "$MIGRATE" = "1" ]; then
    log "Running migrations (locked)..."
    python3 "${PROJ_PATH}/bin/manage.py" migrate_locked --noinput \
        || die "migration failed — the schema is in an unknown state, NOT restarting the app"
else
    log "Skipping migrations (deploy did not request them)."
fi

log "Collecting static files..."
python3 "${PROJ_PATH}/bin/manage.py" collectstatic --noinput \
    || die "collectstatic failed"

# ── render ───────────────────────────────────────────────────────────────────
#
# The JUST-INSTALLED framework's cron/systemd templates, rendered with this
# node's parameters into var/deploy/, plus any project extras from aws/. All
# /etc convergence below installs FROM var/deploy/, and check_node audits
# /etc AGAINST var/deploy/ — so an empty or failed render must stop the
# deploy here, before anything touches /etc.

log "Rendering node templates into var/deploy..."
python3 -m mojo.deploy render --dest "$DEPLOY_DIR" \
        --project-path "$PROJ_PATH" --app-user "$APP_USER" \
        --web-user "$WEB_USER" --workers "$ASGI_WORKERS" \
    || die "template render failed — not converging /etc against an unknown contract"
compgen -G "${DEPLOY_DIR}/cron.d/*" > /dev/null \
    || die "render left ${DEPLOY_DIR}/cron.d empty — refusing to converge cron against nothing"
compgen -G "${DEPLOY_DIR}/systemd/*" > /dev/null \
    || die "render left ${DEPLOY_DIR}/systemd empty — refusing to converge systemd against nothing"

# ── nginx ────────────────────────────────────────────────────────────────────

log "Updating nginx configs..."
install_file "${PROJ_PATH}/aws/nginx/nginx.conf"  "${NGINX_ETC}/nginx.conf"
install_file "${PROJ_PATH}/aws/nginx/asgi.inc"    "${NGINX_ETC}/asgi.inc"
MOJOSEC_DJANGO_EXISTED=0
MOJOSEC_DJANGO_BACKUP="$(mktemp "${NGINX_ETC}/.mojosec-django.XXXXXX")"
if [ -e "${NGINX_ETC}/django.inc" ]; then
    [ ! -L "${NGINX_ETC}/django.inc" ] \
        || die "refusing symlink nginx django.inc"
    [ -f "${NGINX_ETC}/django.inc" ] \
        || die "nginx django.inc is not a regular file"
    cp -p "${NGINX_ETC}/django.inc" "$MOJOSEC_DJANGO_BACKUP"
    MOJOSEC_DJANGO_EXISTED=1
fi
install_file "${PROJ_PATH}/aws/nginx/django.inc"  "${NGINX_ETC}/django.inc"

if compgen -G "${PROJ_PATH}/aws/nginx/sec.d/*.conf" > /dev/null; then
    mkdir -p "${NGINX_ETC}/sec.d"
    cp -f "${PROJ_PATH}/aws/nginx/sec.d/"*.conf "${NGINX_ETC}/sec.d/"
fi

# Repo-owned vhosts, converged on every deploy — the repo's aws/nginx/conf.d/
# IS the set of fleet-served vhosts. `.example` files are templates for
# operators and are deliberately not installed. Other vhosts on a node
# (single-node domains) stay node-managed and are not touched from git;
# retiring a once-shipped name is aws/node_retired.conf's job below.

mkdir -p "${NGINX_ETC}/conf.d"
VHOSTS=0
if compgen -G "${PROJ_PATH}/aws/nginx/conf.d/*.conf" > /dev/null; then
    for vhost in "${PROJ_PATH}/aws/nginx/conf.d/"*.conf; do
        name="$(basename "$vhost")"
        case "$name" in *.example*) continue ;; esac
        cp -f "$vhost" "${NGINX_ETC}/conf.d/${name}"
        VHOSTS=$((VHOSTS+1))
    done
fi
log "  converged ${VHOSTS} vhost(s) from aws/nginx/conf.d"

# MojoSec root assets come from the installed package, never from var/deploy
# or the application-writable project tree. `observe` reports only; all ban
# policy remains central. best_effort makes an unenrolled old node a warning,
# while required makes a configured fleet fail closed at deploy time.
log "Converging MojoSec (${MOJOSEC_MODE}, ${MOJOSEC_DEPLOY_CRITICALITY})..."
restore_mojosec_django() {
    if [ "$MOJOSEC_DJANGO_EXISTED" = "1" ]; then
        cp -p "$MOJOSEC_DJANGO_BACKUP" "${NGINX_ETC}/django.inc"
    else
        rm -f -- "${NGINX_ETC}/django.inc"
    fi
    rm -f -- "$MOJOSEC_DJANGO_BACKUP"
}

# Distinguish an old package that truly lacks the module from any safety or
# convergence failure inside a present module. Only exact exit 3 means absent.
if python3 -I -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("mojo.deploy.mojosec") else 3)'; then
    MOJOSEC_MODULE_AVAILABLE=1
else
    module_rc=$?
    [ "$module_rc" = "3" ] || {
        restore_mojosec_django
        die "cannot preflight MojoSec deployment module"
    }
    MOJOSEC_MODULE_AVAILABLE=0
fi

if [ "$MOJOSEC_MODULE_AVAILABLE" = "1" ]; then
    if ! python3 -I -m mojo.deploy.mojosec converge \
        --mode "$MOJOSEC_MODE" \
        --criticality "$MOJOSEC_DEPLOY_CRITICALITY"; then
        restore_mojosec_django
        nginx -t || die "MojoSec failed and the exact prior django.inc is invalid"
        systemctl reload nginx \
            || die "cannot reload exact prior nginx graph after MojoSec failure"
        die "MojoSec deployment failed"
    fi
else
    # A downgrade can replace the package while this script keeps running from
    # its old inode. Enrolled lifecycle cannot be guessed when enrollment
    # exists; only an explicitly off or genuinely unenrolled node is retired.
    fallback_mode="$MOJOSEC_MODE"
    if [ "$fallback_mode" = "enrolled" ]; then
        [ ! -e /etc/mojosec/enrollment.json ] \
            || { restore_mojosec_django; die "old package cannot resolve enrolled MojoSec lifecycle"; }
        fallback_mode=off
    fi
    [ "$fallback_mode" = "off" ] \
        || { restore_mojosec_django; die "old package cannot deploy MojoSec observe mode"; }
    log "MojoSec module absent; applying transactional pre-feature off cleanup"
    fallback_dir="$(mktemp -d "${NGINX_ETC}/.mojosec-off.XXXXXX")"
    prior_active=0
    prior_enabled=0
    systemctl is-active --quiet mojosec.service && prior_active=1
    systemctl is-enabled --quiet mojosec.service && prior_enabled=1
    managed=("${NGINX_ETC}/conf.d/00_mojosec.conf" "${LOGROTATE_ETC}/mojosec")
    index=0
    for path in "${managed[@]}"; do
        [ ! -L "$path" ] || { restore_mojosec_django; die "refusing symlink during MojoSec cleanup: $path"; }
        if [ -e "$path" ]; then
            [ -f "$path" ] && [ "$(stat -c %u "$path")" = "0" ] \
                || { restore_mojosec_django; die "refusing unsafe MojoSec cleanup target: $path"; }
            cp -p "$path" "$fallback_dir/$index"
        else
            : > "$fallback_dir/$index.absent"
        fi
        index=$((index+1))
    done
    if [ -e "${SYSTEMD_ETC}/mojosec.service" ] || \
            [ "$prior_active" = "1" ] || [ "$prior_enabled" = "1" ]; then
        systemctl disable --now mojosec.service \
            || { restore_mojosec_django; die "cannot stop pre-feature MojoSec service"; }
    fi
    rm -f -- "${managed[@]}"
    fallback_ok=1
    nginx -t || fallback_ok=0
    [ "$fallback_ok" = "0" ] || systemctl reload nginx || fallback_ok=0
    if [ "$fallback_ok" = "0" ]; then
        index=0
        for path in "${managed[@]}"; do
            if [ -f "$fallback_dir/$index" ]; then
                cp -p "$fallback_dir/$index" "$path"
            else
                rm -f -- "$path"
            fi
            index=$((index+1))
        done
        restore_mojosec_django
        nginx -t || die "pre-feature cleanup rollback restored an invalid graph"
        systemctl reload nginx || die "cannot reload pre-feature cleanup rollback"
        [ "$prior_enabled" = "0" ] || systemctl enable mojosec.service
        [ "$prior_active" = "0" ] || systemctl start mojosec.service
        die "pre-feature MojoSec off cleanup failed and was rolled back"
    fi
    rm -f -- "$fallback_dir/0" "$fallback_dir/0.absent" \
              "$fallback_dir/1" "$fallback_dir/1.absent"
    rmdir "$fallback_dir"
fi
rm -f -- "$MOJOSEC_DJANGO_BACKUP"

# ── retired names ────────────────────────────────────────────────────────────
#
# Names this project ONCE shipped into cron.d/conf.d and has since retired,
# declared in aws/node_retired.conf (lines `cron.d/<name>` / `conf.d/<name>`,
# `#` comments). An explicit list, never a discovery sweep, for the files the
# structural cron sweep below cannot catch: entries that never mention
# PROJ_PATH, and conf.d vhosts (where a mentions-the-project rule would
# delete live node-managed domains). Removals are logged, never silent. The
# file lives OUTSIDE aws/cron.d/ so the cron convergence glob can never copy
# the list itself into /etc.

RETIRED_LIST="${PROJ_PATH}/aws/node_retired.conf"
if [[ -f "$RETIRED_LIST" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="${line//[[:space:]]/}"
        [[ -n "$line" ]] || continue
        case "$line" in
            cron.d/*) target="${CRON_ETC}/${line#cron.d/}" ;;
            conf.d/*) target="${NGINX_ETC}/conf.d/${line#conf.d/}" ;;
            *) log "  WARN: node_retired.conf line not understood: ${line}"; continue ;;
        esac
        case "${line#*/}" in
            ""|*/*|.|..) log "  WARN: refusing suspicious retired name: ${line}"; continue ;;
        esac
        if [[ -f "$target" ]]; then
            log "  retiring declared name: ${line}"
            rm -f "$target"
        fi
    done < "$RETIRED_LIST"
fi

# Gate the reload on the config test, and treat a failed test as fatal. nginx
# keeps serving the old config either way, but continuing the deploy would
# restart the app behind a web server running configuration that no longer
# matches the code that was just installed.
nginx -t || die "nginx config test failed — not reloading, and not restarting the app"
systemctl reload nginx

# ── systemd ──────────────────────────────────────────────────────────────────
#
# From the RENDERED set, timers as well as services. Copying only *.service
# is why config-sync.timer was never installed on any box despite the unit
# existing in the repo.

log "Updating systemd units..."
UNIT_SRC="${DEPLOY_DIR}/systemd"
if compgen -G "${UNIT_SRC}/*.service" > /dev/null; then
    cp -f "${UNIT_SRC}/"*.service "${SYSTEMD_ETC}/"
fi
if compgen -G "${UNIT_SRC}/*.timer" > /dev/null; then
    cp -f "${UNIT_SRC}/"*.timer "${SYSTEMD_ETC}/"
fi
systemctl daemon-reload

# Enable any timer we ship. Enabling is idempotent, and a timer that is
# present but never enabled does nothing while looking installed.
for unit in "${UNIT_SRC}/"*.timer; do
    [[ -e "$unit" ]] || continue
    name="$(basename "$unit")"
    systemctl enable --now "$name" || die "could not enable $name"
    log "  enabled $name"
done

# ── cron ─────────────────────────────────────────────────────────────────────
#
# The RENDERED var/deploy/cron.d/ IS the set, not merely a subset that gets
# added to. The sweep DISCOVERS what to retire instead of naming it: a
# hardcoded list of legacy filenames cannot survive contact with a real node —
# fleets have carried `1mojocron`, `2certbot` and `3mojo_jogs` (yes, a typo),
# none of which any guess would have matched, each firing alongside its
# replacement forever. The rule is structural: a file in cron.d that invokes
# PROJ_PATH is ours; if it is ours and the rendered set no longer ships that
# filename, it is stale and goes. Files that never mention PROJ_PATH
# (0hourly, distro entries) are not ours and are left alone (retire those via
# aws/node_retired.conf above). Removals are logged, never silent.

log "Updating cron entries..."
if compgen -G "${DEPLOY_DIR}/cron.d/*" > /dev/null; then
    cp -f "${DEPLOY_DIR}/cron.d/"* "${CRON_ETC}/"
fi
for installed in "${CRON_ETC}"/*; do
    [[ -f "$installed" ]] || continue
    name="$(basename "$installed")"
    # Ours? (mentions the project path)  Still shipped? (rendered set has it)
    grep -q "${PROJ_PATH}" "$installed" 2>/dev/null || continue
    [[ -f "${DEPLOY_DIR}/cron.d/${name}" ]] && continue
    log "  retiring stale project cron: ${name}"
    rm -f "$installed"
done

# The root-run manage.py steps above can be the FIRST writer of a framework
# log file (edge/github/jobs on a fresh install), leaving it root-owned in
# the web-group directory — which then blocks the web app and the app-user
# engine from appending. Re-assert the standard ownership (provisioning
# establishes the full APP_USER:WEB_USER setgid model).
if [[ -d "${PROJ_PATH}/var/logs" ]]; then
    chown -R "${APP_USER}:${WEB_USER}" "${PROJ_PATH}/var/logs" 2>/dev/null \
        || log "WARN: could not normalize var/logs ownership (non-fatal off-node)"
fi

# ── restart ──────────────────────────────────────────────────────────────────

log "Restarting mojo-asgi..."
systemctl restart mojo-asgi || die "mojo-asgi failed to restart"

# Snapshot the installed copy of this script next to the rendered contract.
# This is the shim's rollback self-heal: when a rollback reinstalls a
# framework so old that `locate` cannot resolve post_deploy.sh, the shim
# falls back to this snapshot. cmp first: when this run IS the snapshot
# (fallback mode), copying a file onto itself is not progress.
snapshot_self() {
    local snap="${DEPLOY_DIR}/post_deploy.sh" self="${BASH_SOURCE[0]}"
    if [[ -f "$self" ]] && ! cmp -s "$self" "$snap" 2>/dev/null; then
        cp -f "$self" "$snap" 2>/dev/null \
            || log "WARN: could not snapshot post_deploy.sh into var/deploy"
    fi
}

# A restart that returns 0 only means systemd accepted the unit. Confirm the
# app is actually answering before calling the deploy done — through
# PROBE_URL, which the shim points at whatever vhost proxies straight to the
# asgi socket (a port-80 server that 301s everything would false-pass
# `curl -f`).
for _ in $(seq 1 15); do
    if curl -fsS -o /dev/null --max-time 2 "$PROBE_URL" 2>/dev/null; then
        snapshot_self
        log "Post-deploy complete — app responding."
        exit 0
    fi
    sleep 2
done
die "app did not answer ${PROBE_URL} within 30s of restart"
