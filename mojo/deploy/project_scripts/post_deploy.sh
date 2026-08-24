#!/bin/bash
# Stable project-owned post-checkout transaction.
#
# The only release gates are:
#   1. nginx accepts the installed configuration;
#   2. the restarted candidate API answers the configured URL with HTTP 200.

set -euo pipefail

PROJ_PATH="${PROJ_PATH:-/opt/api}"
PROBE_URL="${PROBE_URL:-https://127.0.0.1/api/version}"
APP_USER="${APP_USER:-ec2-user}"
WEB_USER="${WEB_USER:-www}"
ASGI_WORKERS="${ASGI_WORKERS:-4}"
NGINX_ETC="${NGINX_ETC:-/etc/nginx}"
SYSTEMD_ETC="${SYSTEMD_ETC:-/etc/systemd/system}"
CRON_ETC="${CRON_ETC:-/etc/cron.d}"
TRANSACTION_ROOT="${PROJ_PATH}/var/deploy-rollback"
ACTIVE="${TRANSACTION_ROOT}/active"
ROLLING_BACK=0

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "[$(date '+%H:%M:%S')] FATAL: $*" >&2; exit 1; }

record_file() {
    local destination="$1" key
    key="$(printf '%s' "$destination" | cksum | awk '{print $1}')"
    printf '%s\t%s\n' "$key" "$destination" >> "$ACTIVE/files"
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        cp -a -- "$destination" "$ACTIVE/$key"
    else
        : > "$ACTIVE/$key.absent"
    fi
}

restore_files() {
    local key destination
    [ -f "$ACTIVE/files" ] || return 0
    while IFS=$'\t' read -r key destination; do
        [ -n "$key" ] && [ -n "$destination" ] || continue
        if [ -f "$ACTIVE/$key.absent" ]; then
            rm -f -- "$destination"
        else
            mkdir -p "$(dirname "$destination")"
            rm -rf -- "$destination"
            cp -a -- "$ACTIVE/$key" "$destination"
        fi
    done < "$ACTIVE/files"
}

probe_api() {
    local code
    code="$(curl -ksS --max-time 20 -o /dev/null -w '%{http_code}' "$PROBE_URL")" || return 1
    [ "$code" = "200" ]
}

restore_previous_release() {
    local previous_sha previous_framework
    [ -d "$ACTIVE" ] || return 0
    previous_sha="$(cat "$ACTIVE/previous_sha")"
    previous_framework="$(cat "$ACTIVE/previous_framework")"
    log "Rolling back to $previous_sha"
    cd "$PROJ_PATH"
    git checkout --force "$previous_sha" || return 1
    if [ -f requirements.txt ]; then
        pip install -r requirements.txt || return 1
    fi
    pip install "django-mojo==$previous_framework" || return 1
    restore_files || return 1
    systemctl daemon-reload || return 1
    nginx -t || return 1
    systemctl restart mojo-asgi.service || return 1
    systemctl reload nginx || return 1
    probe_api || return 1
    rm -rf -- "$ACTIVE" || return 1
}

rollback() {
    local status="${1:-1}"
    [ "$ROLLING_BACK" = "0" ] || exit "$status"
    ROLLING_BACK=1
    trap - ERR TERM INT HUP
    if restore_previous_release; then
        log "Rollback completed"
    else
        echo "FATAL: rollback failed; transaction retained at $ACTIVE" >&2
    fi
    exit "$status"
}

FRAMEWORK=""
MIGRATE=0
RECOVER_ONLY=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --framework) FRAMEWORK="${2:-}"; shift 2 ;;
        --migrate) MIGRATE=1; shift ;;
        --recover-only) RECOVER_ONLY=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done

cd "$PROJ_PATH"
if [ "$RECOVER_ONLY" = "1" ]; then
    [ -d "$ACTIVE" ] || exit 0
    restore_previous_release
    exit 0
fi

[ ! -d "$ACTIVE" ] || die "an interrupted deployment must be recovered first"
PREVIOUS_SHA="${MOJO_PREVIOUS_SHA:-$(head -c 64 var/previous_sha 2>/dev/null || true)}"
PREVIOUS_FRAMEWORK="${MOJO_PREVIOUS_FRAMEWORK:-$(head -c 64 var/previous_framework 2>/dev/null || true)}"
[ -n "$PREVIOUS_SHA" ] || die "missing previous commit"
[ -n "$PREVIOUS_FRAMEWORK" ] || die "missing previous framework"

mkdir -p "$TRANSACTION_ROOT"
mkdir "$ACTIVE"
printf '%s\n' "$PREVIOUS_SHA" > "$ACTIVE/previous_sha"
printf '%s\n' "$PREVIOUS_FRAMEWORK" > "$ACTIVE/previous_framework"
: > "$ACTIVE/files"

record_file "$NGINX_ETC/nginx.conf"
record_file "$NGINX_ETC/django.inc"
for source in "$PROJ_PATH"/aws/nginx/conf.d/*.conf; do
    [ -f "$source" ] || continue
    record_file "$NGINX_ETC/conf.d/$(basename "$source")"
done

trap 'rollback $?' ERR
trap 'rollback 143' TERM HUP
trap 'rollback 130' INT

log "Installing candidate dependencies"
if [ -f requirements.txt ]; then
    pip install -r requirements.txt
fi
if [ -n "$FRAMEWORK" ]; then
    pip install "django-mojo==$FRAMEWORK"
else
    pip install --upgrade django-mojo
fi

if [ "$MIGRATE" = "1" ]; then
    python3 bin/manage.py migrate_locked --noinput
fi
python3 bin/manage.py collectstatic --noinput

python3 -m mojo.deploy render --dest "$PROJ_PATH/var/deploy" --project-path "$PROJ_PATH" --app-user "$APP_USER" --web-user "$WEB_USER" --workers "$ASGI_WORKERS"

# Rendering has not changed the host. Snapshot every destination it produced
# now, immediately before the copy loops mutate those destinations.
for source in "$PROJ_PATH"/var/deploy/systemd/*; do
    [ -f "$source" ] || continue
    record_file "$SYSTEMD_ETC/$(basename "$source")"
done
for source in "$PROJ_PATH"/var/deploy/cron.d/*; do
    [ -f "$source" ] || continue
    record_file "$CRON_ETC/$(basename "$source")"
done

install -D -m 0644 "$PROJ_PATH/aws/nginx/nginx.conf" "$NGINX_ETC/nginx.conf"
install -D -m 0644 "$PROJ_PATH/aws/nginx/django.inc" "$NGINX_ETC/django.inc"
for source in "$PROJ_PATH"/aws/nginx/conf.d/*.conf; do
    [ -f "$source" ] || continue
    install -D -m 0644 "$source" "$NGINX_ETC/conf.d/$(basename "$source")"
done
for source in "$PROJ_PATH"/var/deploy/systemd/*; do
    [ -f "$source" ] || continue
    install -D -m 0644 "$source" "$SYSTEMD_ETC/$(basename "$source")"
done
for source in "$PROJ_PATH"/var/deploy/cron.d/*; do
    [ -f "$source" ] || continue
    install -D -m 0644 "$source" "$CRON_ETC/$(basename "$source")"
done

# These are the two deliberate release checks. A redirect is not a live API.
nginx -t
systemctl daemon-reload
systemctl restart mojo-asgi.service
systemctl reload nginx
probe_api

trap - ERR TERM INT HUP
rm -rf -- "$ACTIVE"
log "Candidate API returned HTTP 200; deployment complete"
