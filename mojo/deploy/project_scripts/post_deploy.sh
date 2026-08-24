#!/bin/bash
# Typed activation body called by update.sh inside the transient deployment
# unit. There are only three paths: api, code, and one project profile.

set -Eeuo pipefail

PROJ_PATH="${PROJ_PATH:-/opt/api}"
PROBE_URL="${PROBE_URL:-https://127.0.0.1/api/version}"
PROBE_SECONDS="${PROBE_SECONDS:-30}"
APP_USER="${APP_USER:-ec2-user}"
WEB_USER="${WEB_USER:-www}"
ASGI_WORKERS="${ASGI_WORKERS:-4}"
NGINX_ETC="${NGINX_ETC:-/etc/nginx}"
SYSTEMD_ETC="${SYSTEMD_ETC:-/etc/systemd/system}"
CRON_ETC="${CRON_ETC:-/etc/cron.d}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "[$(date '+%H:%M:%S')] FATAL: $*" >&2; exit 1; }
valid_node_type() { [[ "$1" =~ ^[a-z][a-z0-9_-]{0,31}$ ]]; }
safe_name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$ ]]; }

ACTION=""
NODE_TYPE="api"
STATE=""
MIGRATE=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --activate) ACTION="activate"; shift ;;
        --rollback-candidate) ACTION="rollback-candidate"; shift ;;
        --activate-previous) ACTION="activate-previous"; shift ;;
        --node-type) NODE_TYPE="${2:-}"; shift 2 ;;
        --state) STATE="${2:-}"; shift 2 ;;
        --migrate) MIGRATE=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done

[ -n "$ACTION" ] || die "missing activation action"
valid_node_type "$NODE_TYPE" || die "invalid node type"
[ -n "$STATE" ] && [ -d "$STATE" ] || die "missing deployment transaction state"
if [ "$NODE_TYPE" != "api" ] && [ "$MIGRATE" = "1" ]; then
    die "only api nodes may migrate"
fi

FILES="$STATE/files"
TOUCHED_UNITS="$STATE/touched_units"
ENABLED_UNITS="$STATE/enabled_units"
ACTIVE_UNITS="$STATE/active_units"
touch "$FILES" "$TOUCHED_UNITS" "$ENABLED_UNITS" "$ACTIVE_UNITS"

set_phase() { printf '%s\n' "$1" > "$STATE/phase"; }

safe_destination() {
    local destination="$1" parent leaf
    parent="${destination%/*}"
    leaf="${destination##*/}"
    safe_name "$leaf" || return 1
    case "$destination" in
        "$NGINX_ETC/nginx.conf"|"$NGINX_ETC/asgi.inc"|"$NGINX_ETC/django.inc") return 0 ;;
    esac
    case "$parent" in
        "$NGINX_ETC/conf.d"|"$NGINX_ETC/sec.d"|"$SYSTEMD_ETC"|"$CRON_ETC") return 0 ;;
    esac
    return 1
}

record_file() {
    local destination="$1" key
    safe_destination "$destination" || die "unsupported deployment destination: $destination"
    grep -Fq "$(printf '\t%s' "$destination")" "$FILES" 2>/dev/null && return 0
    key="$(printf '%s' "$destination" | cksum | awk '{print $1}')"
    printf '%s\t%s\n' "$key" "$destination" >> "$FILES"
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        cp -a -- "$destination" "$STATE/$key"
    else
        : > "$STATE/$key.absent"
    fi
}

restore_files() {
    local key destination
    while IFS=$'\t' read -r key destination; do
        [ -n "$key" ] && [ -n "$destination" ] || continue
        case "$key" in *[!0-9]*|"") return 1 ;; esac
        safe_destination "$destination" || return 1
        if [ -f "$STATE/$key.absent" ]; then
            rm -f -- "$destination"
        elif [ -e "$STATE/$key" ] || [ -L "$STATE/$key" ]; then
            mkdir -p -- "$(dirname "$destination")"
            rm -rf -- "$destination"
            cp -a -- "$STATE/$key" "$destination"
        else
            return 1
        fi
    done < "$FILES"
}

record_unit() {
    local unit="$1"
    safe_name "$unit" || die "invalid systemd unit name"
    grep -Fxq "$unit" "$TOUCHED_UNITS" 2>/dev/null && return 0
    printf '%s\n' "$unit" >> "$TOUCHED_UNITS"
    systemctl is-enabled "$unit" >/dev/null 2>&1 && printf '%s\n' "$unit" >> "$ENABLED_UNITS" || true
    systemctl is-active "$unit" >/dev/null 2>&1 && printf '%s\n' "$unit" >> "$ACTIVE_UNITS" || true
}

restore_unit_states() {
    local unit
    [ -s "$TOUCHED_UNITS" ] || return 0
    systemctl daemon-reload
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        if grep -Fxq "$unit" "$ENABLED_UNITS"; then
            systemctl enable "$unit"
        else
            systemctl disable "$unit" >/dev/null 2>&1 || true
        fi
        if grep -Fxq "$unit" "$ACTIVE_UNITS"; then
            systemctl start "$unit"
        else
            systemctl stop "$unit" >/dev/null 2>&1 || true
        fi
    done < "$TOUCHED_UNITS"
}

install_one() {
    local source="$1" destination="$2"
    [ -f "$source" ] || return 0
    record_file "$destination"
    install -D -m 0644 "$source" "$destination"
}

install_directory() {
    local source_dir="$1" destination_dir="$2" source name
    [ -d "$source_dir" ] || return 0
    for source in "$source_dir"/*; do
        [ -f "$source" ] || continue
        name="$(basename "$source")"
        case "$name" in *.example) continue ;; esac
        safe_name "$name" || die "invalid deployed file name"
        install_one "$source" "$destination_dir/$name"
    done
}

record_rendered_units() {
    local source unit
    for source in "$PROJ_PATH"/var/deploy/systemd/*; do
        [ -f "$source" ] || continue
        unit="$(basename "$source")"
        case "$unit" in *.service|*.timer) ;; *) continue ;; esac
        record_unit "$unit"
    done
    record_unit "mojo-asgi.service"
    record_unit "nginx.service"
}

remove_retired() {
    local line kind name destination
    [ -f "$PROJ_PATH/aws/node_retired.conf" ] || return 0
    while IFS= read -r line; do
        line="${line%%#*}"
        line="${line#${line%%[![:space:]]*}}"
        line="${line%${line##*[![:space:]]}}"
        [ -n "$line" ] || continue
        kind="${line%%/*}"
        name="${line#*/}"
        [ "$name" != "$line" ] && safe_name "$name" || continue
        case "$kind" in
            conf.d) destination="$NGINX_ETC/conf.d/$name" ;;
            sec.d) destination="$NGINX_ETC/sec.d/$name" ;;
            systemd) destination="$SYSTEMD_ETC/$name"; record_unit "$name" ;;
            cron.d) destination="$CRON_ETC/$name" ;;
            *) continue ;;
        esac
        record_file "$destination"
        rm -f -- "$destination"
    done < "$PROJ_PATH/aws/node_retired.conf"
}

probe_api() {
    local started now code
    started="$(date +%s)"
    while :; do
        code="$(curl -ksS --max-time 10 -o /dev/null -w '%{http_code}' "$PROBE_URL" 2>/dev/null || true)"
        [ "$code" = "200" ] && return 0
        now="$(date +%s)"
        [ $((now - started)) -lt "$PROBE_SECONDS" ] || return 1
        sleep 1
    done
}

profile_environment() {
    export PROJ_PATH
    export MOJO_DEPLOY_CANDIDATE_SHA="$(head -1 "$STATE/candidate_sha" 2>/dev/null || true)"
    export MOJO_DEPLOY_PREVIOUS_SHA="$(head -1 "$STATE/previous_sha" 2>/dev/null || true)"
    export MOJO_DEPLOY_PREVIOUS_FRAMEWORK="$(head -1 "$STATE/previous_framework" 2>/dev/null || true)"
    export MOJO_DEPLOY_CANDIDATE_FRAMEWORK="$(head -1 "$STATE/candidate_framework" 2>/dev/null || true)"
    export MOJO_DEPLOY_NODE_TYPE="$NODE_TYPE"
    export MOJO_DEPLOY_DEPLOYMENT="$(head -1 "$STATE/deployment" 2>/dev/null || true)"
    export MOJO_DEPLOY_STARTED_AT="$(head -1 "$STATE/started_at" 2>/dev/null || true)"
    export MOJO_DEPLOY_ROLLBACK="${MOJO_DEPLOY_ROLLBACK:-0}"
}

candidate_profile() {
    printf '%s\n' "$PROJ_PATH/aws/deploy/$NODE_TYPE.sh"
}

run_candidate_profile() {
    local phase="$1" profile
    profile="$(candidate_profile)"
    [ -f "$profile" ] || die "missing custom deploy profile: aws/deploy/$NODE_TYPE.sh"
    bash -n "$profile" || die "custom deploy profile does not parse"
    profile_environment
    bash "$profile" "$phase"
}

run_previous_profile() {
    local phase="$1" profile="$STATE/previous_profile.sh"
    [ -f "$profile" ] || die "saved previous custom deploy profile is missing"
    bash -n "$profile" || die "saved previous custom deploy profile does not parse"
    profile_environment
    MOJO_DEPLOY_ROLLBACK=1 bash "$profile" "$phase"
}

activate_api() {
    local source timer
    cd "$PROJ_PATH"
    set_phase django_check
    log "Checking candidate Django"
    python3 bin/manage.py check
    if [ "$MIGRATE" = "1" ]; then
        set_phase migration
        log "Running migrations"
        python3 bin/manage.py migrate_locked --noinput
    fi
    set_phase static_collection
    python3 bin/manage.py collectstatic --noinput

    set_phase configuration
    log "Rendering API configuration"
    python3 -m mojo.deploy render --dest "$PROJ_PATH/var/deploy" \
        --project-path "$PROJ_PATH" --app-user "$APP_USER" \
        --web-user "$WEB_USER" --workers "$ASGI_WORKERS"
    record_rendered_units

    [ -f "$PROJ_PATH/aws/nginx/nginx.conf" ] || die "missing aws/nginx/nginx.conf"
    [ -f "$PROJ_PATH/aws/nginx/django.inc" ] || die "missing aws/nginx/django.inc"
    install_one "$PROJ_PATH/aws/nginx/nginx.conf" "$NGINX_ETC/nginx.conf"
    install_one "$PROJ_PATH/aws/nginx/asgi.inc" "$NGINX_ETC/asgi.inc"
    install_one "$PROJ_PATH/aws/nginx/django.inc" "$NGINX_ETC/django.inc"
    install_directory "$PROJ_PATH/aws/nginx/conf.d" "$NGINX_ETC/conf.d"
    install_directory "$PROJ_PATH/aws/nginx/sec.d" "$NGINX_ETC/sec.d"
    install_directory "$PROJ_PATH/var/deploy/systemd" "$SYSTEMD_ETC"
    install_directory "$PROJ_PATH/var/deploy/cron.d" "$CRON_ETC"
    remove_retired

    set_phase nginx_check
    log "Checking nginx configuration"
    nginx -t
    systemctl daemon-reload
    for source in "$PROJ_PATH"/var/deploy/systemd/*.timer; do
        [ -f "$source" ] || continue
        timer="$(basename "$source")"
        systemctl enable --now "$timer"
    done
    set_phase api_restart
    log "Restarting API"
    systemctl restart mojo-asgi.service
    systemctl reload nginx
    set_phase api_probe
    log "Probing API"
    probe_api || die "candidate API did not return HTTP 200"
}

activate_previous_api() {
    log "Restoring previous API configuration"
    restore_files
    restore_unit_states
    nginx -t
    systemctl daemon-reload
    systemctl restart mojo-asgi.service
    systemctl reload nginx
    probe_api || die "previous API did not return HTTP 200"
}

rollback_candidate() {
    if [ "$NODE_TYPE" = "api" ]; then
        restore_files
        restore_unit_states
    elif [ "$NODE_TYPE" != "code" ]; then
        run_candidate_profile restart
    fi
}

activate_previous() {
    if [ "$NODE_TYPE" = "api" ]; then
        activate_previous_api
    elif [ "$NODE_TYPE" = "code" ]; then
        log "Previous code-only node restored; activation belongs to its supervisor"
    else
        log "Restarting previous $NODE_TYPE profile"
        run_previous_profile restart
        run_previous_profile probe
    fi
}

cd "$PROJ_PATH"
case "$ACTION" in
    activate)
        if [ "$NODE_TYPE" = "api" ]; then
            activate_api
        elif [ "$NODE_TYPE" = "code" ]; then
            log "Code-only deployment complete; no host activation requested"
        else
            set_phase custom_preflight
            log "Preflighting $NODE_TYPE profile"
            run_candidate_profile preflight
            set_phase custom_restart
            log "Restarting $NODE_TYPE profile"
            run_candidate_profile restart
            set_phase custom_probe
            log "Probing $NODE_TYPE profile"
            run_candidate_profile probe
        fi
        ;;
    rollback-candidate) rollback_candidate ;;
    activate-previous) activate_previous ;;
esac
