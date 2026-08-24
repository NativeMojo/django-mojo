#!/bin/bash
# Stable deployment transaction. The framework supplies mechanics; projects
# supply only an optional aws/deploy/<type>.sh lifecycle for unusual nodes.

set -Eeuo pipefail

PROJ_PATH="${PROJ_PATH:-/opt/api}"
LOCK_FILE="${PROJ_PATH}/var/update.lock"
RUNTIME_SECONDS="${MOJO_DEPLOY_RUNTIME_SECONDS:-1800}"
ROLLBACK_SECONDS="${MOJO_DEPLOY_ROLLBACK_SECONDS:-900}"
APP_USER="${APP_USER:-${SUDO_USER:-ec2-user}}"
RUN_UID="$(id -u)"
TRANSACTION_ROOT="/var/lib/django-mojo-deploy"
if [ "$RUN_UID" != "0" ] && [ -n "${MOJO_DEPLOY_STATE_ROOT:-}" ]; then
    TRANSACTION_ROOT="$MOJO_DEPLOY_STATE_ROOT"
fi
ACTIVE="$TRANSACTION_ROOT/active"
ROLLING_BACK=0

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "[$(date '+%H:%M:%S')] FATAL: $*" >&2; exit 1; }
usage() {
    echo "usage: update.sh --sha <commit> --framework <version> --deployment <uuid> [--node-type <type>] [--migrate] | --manual [--node-type <type>]" >&2
}

valid_sha() { [[ "$1" =~ ^[0-9a-fA-F]{7,40}$ ]]; }
valid_version() { [[ "$1" =~ ^[0-9A-Za-z][0-9A-Za-z._!+-]{0,63}$ ]]; }
valid_node_type() { [[ "$1" =~ ^[a-z][a-z0-9_-]{0,31}$ ]]; }
valid_deployment() {
    [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

installed_framework() {
    python3 -m pip show django-mojo 2>/dev/null |
        sed -n 's/^Version:[[:space:]]*//p' | head -1
}

git_project() {
    # The transient unit is root so it can converge host services, but the
    # checkout stays owned by the application account. This also avoids Git's
    # root/foreign-owner safe.directory refusal on ordinary nodes.
    if [ "$RUN_UID" = "0" ] && id "$APP_USER" >/dev/null 2>&1; then
        sudo -H -u "$APP_USER" -- git -C "$PROJ_PATH" "$@"
    else
        git -C "$PROJ_PATH" "$@"
    fi
}

manifest_for_tree() {
    if [ -f aws/deploy/requirements.txt ]; then
        printf '%s\n' "aws/deploy/requirements.txt"
    elif [ -f requirements.txt ]; then
        printf '%s\n' "requirements.txt"
    fi
}

install_manifest() {
    local manifest="${1:-}"
    [ -n "$manifest" ] || return 0
    log "Installing declared dependencies"
    python3 -m pip install -r "$manifest"
}

read_previous_type() {
    python3 - "$PROJ_PATH/var/deploy_identity.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle)
    node_type = value.get("node_type", "api")
    if isinstance(node_type, str):
        print(node_type)
except (OSError, ValueError, TypeError):
    pass
PY
}

ensure_state_root() {
    [ ! -L "$TRANSACTION_ROOT" ] || die "deployment state path is a symlink"
    if [ ! -d "$TRANSACTION_ROOT" ]; then
        mkdir -m 0700 -p -- "$TRANSACTION_ROOT"
    fi
    chmod 0700 "$TRANSACTION_ROOT"
}

write_outcome() {
    local status="$1" detail="${2:-}" outcome_sha="${3:-${SHA:-}}"
    local outcome_deployment="${4:-${DEPLOYMENT:-}}"
    local outcome_type="${5:-${NODE_TYPE:-api}}"
    [ -n "$outcome_deployment" ] || return 0
    mkdir -p "$PROJ_PATH/var"
    local path="$PROJ_PATH/var/deploy_outcome.json.tmp.$$"
    printf '{"schema":1,"sha":"%s","deployment":"%s","node_type":"%s","status":"%s","detail":"%s"}\n' \
        "$outcome_sha" "$outcome_deployment" "$outcome_type" "$status" "$detail" > "$path"
    chmod 0644 "$path"
    mv -f "$path" "$PROJ_PATH/var/deploy_outcome.json"
}

rollback_transaction() {
    local previous_sha previous_framework previous_type candidate_type
    local candidate_sha candidate_deployment
    [ -d "$ACTIVE" ] || return 0
    previous_sha="$(head -1 "$ACTIVE/previous_sha" 2>/dev/null || true)"
    previous_framework="$(head -1 "$ACTIVE/previous_framework" 2>/dev/null || true)"
    previous_type="$(head -1 "$ACTIVE/previous_node_type" 2>/dev/null || true)"
    candidate_type="$(head -1 "$ACTIVE/candidate_node_type" 2>/dev/null || true)"
    candidate_sha="$(head -1 "$ACTIVE/candidate_sha" 2>/dev/null || true)"
    candidate_deployment="$(head -1 "$ACTIVE/deployment" 2>/dev/null || true)"
    valid_sha "$previous_sha" || return 1
    valid_version "$previous_framework" || return 1
    valid_node_type "$previous_type" || return 1
    valid_node_type "$candidate_type" || return 1

    if [ ! -f "$ACTIVE/mutation_started" ]; then
        write_outcome "failed" "pre_mutation" "$candidate_sha" \
            "$candidate_deployment" "$candidate_type"
        rm -rf -- "$ACTIVE"
        return 0
    fi

    log "Rolling back candidate lifecycle"
    if [ -x "$ACTIVE/candidate_post.sh" ]; then
        MOJO_DEPLOY_ROLLBACK=1 bash "$ACTIVE/candidate_post.sh" \
            --rollback-candidate --node-type "$candidate_type" --state "$ACTIVE" || true
    fi

    log "Restoring $previous_sha"
    cd "$PROJ_PATH"
    git_project checkout --force "$previous_sha" || return 1
    if [ -f "$ACTIVE/previous_requirements.txt" ]; then
        python3 -m pip install -r "$ACTIVE/previous_requirements.txt" || return 1
    fi
    python3 -m pip install "django-mojo==$previous_framework" || return 1

    if [ -x "$ACTIVE/previous_post.sh" ]; then
        MOJO_DEPLOY_ROLLBACK=1 bash "$ACTIVE/previous_post.sh" \
            --activate-previous --node-type "$previous_type" --state "$ACTIVE" || return 1
    elif [ "$previous_type" != "code" ]; then
        return 1
    fi
    write_outcome "failed" "rolled_back" "$candidate_sha" \
        "$candidate_deployment" "$candidate_type"
    rm -rf -- "$ACTIVE"
    log "Rollback completed"
}

rollback_and_exit() {
    local status="${1:-1}"
    [ "$ROLLING_BACK" = "0" ] || exit "$status"
    ROLLING_BACK=1
    trap - ERR TERM INT HUP
    if ! rollback_transaction; then
        echo "FATAL: rollback failed; transaction retained at $ACTIVE" >&2
    fi
    exit "$status"
}

SHA=""
FRAMEWORK=""
DEPLOYMENT=""
NODE_TYPE="api"
MIGRATE=0
MANUAL=0
TRANSACTION=0
ORIGINAL_ARGS=("$@")
while [ "$#" -gt 0 ]; do
    case "$1" in
        --sha) SHA="${2:-}"; shift 2 ;;
        --framework) FRAMEWORK="${2:-}"; shift 2 ;;
        --deployment) DEPLOYMENT="${2:-}"; shift 2 ;;
        --node-type) NODE_TYPE="${2:-}"; shift 2 ;;
        --migrate) MIGRATE=1; shift ;;
        --manual) MANUAL=1; shift ;;
        --transaction) TRANSACTION=1; shift ;;
        *) usage; exit 2 ;;
    esac
done

valid_node_type "$NODE_TYPE" || die "invalid node type"
if [ "$NODE_TYPE" != "api" ] && [ "$MIGRATE" = "1" ]; then
    die "only api nodes may migrate"
fi
if [ "$MANUAL" = "1" ]; then
    [ -z "$SHA$FRAMEWORK$DEPLOYMENT" ] && [ "$MIGRATE" = "0" ] || { usage; exit 2; }
else
    valid_sha "$SHA" || die "invalid commit SHA"
    valid_version "$FRAMEWORK" || die "invalid framework version"
    valid_deployment "$DEPLOYMENT" || die "invalid deployment UUID"
fi

# A custom profile may restart the engine service that launched this command.
# Re-enter the complete transaction in its own bounded systemd cgroup first;
# no checkout or package mutation happens before this boundary.
if [ "$TRANSACTION" = "0" ] && [ "${MOJO_DEPLOY_NO_SYSTEMD:-0}" != "1" ]; then
    command -v systemd-run >/dev/null 2>&1 || die "systemd-run is required"
    self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    unit="django-mojo-deploy-${DEPLOYMENT:-manual}"
    unit="${unit//[^A-Za-z0-9_.@-]/-}"
    exec systemd-run --quiet --wait --collect --unit "$unit" \
        --property Type=oneshot \
        --property "RuntimeMaxSec=$RUNTIME_SECONDS" \
        --property "TimeoutStopSec=$ROLLBACK_SECONDS" \
        --setenv="MOJO_DEPLOY_IN_TRANSIENT_UNIT=1" \
        --setenv="MOJO_DEPLOY_PARENT_STATUS=${MOJO_DEPLOY_PARENT_STATUS:-}" \
        --setenv="PROJ_PATH=$PROJ_PATH" \
        --setenv="APP_USER=$APP_USER" \
        --setenv="PROBE_URL=${PROBE_URL:-https://127.0.0.1/api/version}" \
        --setenv="WEB_USER=${WEB_USER:-www}" \
        --setenv="ASGI_WORKERS=${ASGI_WORKERS:-4}" \
        bash "$self" --transaction "${ORIGINAL_ARGS[@]}"
fi

cd "$PROJ_PATH"
mkdir -p var
exec 9>"$LOCK_FILE"
if [ "$MANUAL" = "1" ]; then
    flock -n 9 || die "another update is running"
else
    flock 9
fi

ensure_state_root
for preparing in "$TRANSACTION_ROOT"/preparing.*; do
    [ -d "$preparing" ] || continue
    rm -rf -- "$preparing"
done
if [ -d "$ACTIVE" ]; then
    log "Recovering interrupted deployment"
    rollback_transaction || die "interrupted deployment recovery failed"
fi

PREVIOUS_SHA="$(git_project rev-parse HEAD)" || die "cannot read current commit"
PREVIOUS_FRAMEWORK="$(installed_framework)"
valid_version "$PREVIOUS_FRAMEWORK" || die "cannot read installed django-mojo version"
PREVIOUS_NODE_TYPE="$(read_previous_type)"
PREVIOUS_NODE_TYPE="${PREVIOUS_NODE_TYPE:-api}"
valid_node_type "$PREVIOUS_NODE_TYPE" || die "invalid previous node type"

# Custom adoption is deliberately staged: the profile must already exist in
# the serving checkout before this node is switched to that type.
if [ "$NODE_TYPE" != "api" ] && [ "$NODE_TYPE" != "code" ]; then
    [ -f "aws/deploy/$NODE_TYPE.sh" ] ||
        die "stage aws/deploy/$NODE_TYPE.sh before activating this node type"
fi

PREPARING="$TRANSACTION_ROOT/preparing.$$"
mkdir -m 0700 "$PREPARING"
printf '%s\n' "$PREVIOUS_SHA" > "$PREPARING/previous_sha"
printf '%s\n' "$PREVIOUS_FRAMEWORK" > "$PREPARING/previous_framework"
printf '%s\n' "$PREVIOUS_NODE_TYPE" > "$PREPARING/previous_node_type"
printf '%s\n' "$NODE_TYPE" > "$PREPARING/candidate_node_type"
printf '%s\n' "$SHA" > "$PREPARING/candidate_sha"
printf '%s\n' "${DEPLOYMENT:-manual}" > "$PREPARING/deployment"
printf '%s\n' "$(date +%s)" > "$PREPARING/started_at"
previous_manifest="$(manifest_for_tree)"
if [ -n "$previous_manifest" ]; then
    cp -f -- "$previous_manifest" "$PREPARING/previous_requirements.txt"
fi
previous_post="$(python3 -m mojo.deploy locate post_deploy.sh)" ||
    die "cannot locate previous post-deploy body"
cp -f -- "$previous_post" "$PREPARING/previous_post.sh"
chmod 0700 "$PREPARING/previous_post.sh"
if [ "$PREVIOUS_NODE_TYPE" != "api" ] && [ "$PREVIOUS_NODE_TYPE" != "code" ]; then
    [ -f "aws/deploy/$PREVIOUS_NODE_TYPE.sh" ] || die "previous custom profile is missing"
    cp -f -- "aws/deploy/$PREVIOUS_NODE_TYPE.sh" "$PREPARING/previous_profile.sh"
    chmod 0700 "$PREPARING/previous_profile.sh"
fi
mv -- "$PREPARING" "$ACTIVE"

trap 'rollback_and_exit $?' ERR
trap 'rollback_and_exit 143' TERM HUP
trap 'rollback_and_exit 130' INT

log "Fetching candidate"
git_project fetch --prune origin
if [ "$MANUAL" = "1" ]; then
    SHA="$(git_project rev-parse origin/main)" || die "cannot resolve origin/main"
fi
git_project cat-file -e "${SHA}^{commit}" 2>/dev/null || die "target commit is unavailable"
SHA="$(git_project rev-parse "${SHA}^{commit}")"
printf '%s\n' "$SHA" > "$ACTIVE/candidate_sha"
: > "$ACTIVE/mutation_started"
git_project clean -fd
git_project checkout --force "$SHA"

candidate_manifest="$(manifest_for_tree)"
install_manifest "$candidate_manifest"
if [ -n "$FRAMEWORK" ]; then
    log "Installing django-mojo $FRAMEWORK"
    python3 -m pip install "django-mojo==$FRAMEWORK"
else
    log "Installing newest django-mojo"
    python3 -m pip install --upgrade django-mojo
fi
installed_candidate_framework="$(installed_framework)"
valid_version "$installed_candidate_framework" || die "cannot read candidate django-mojo version"
printf '%s\n' "$installed_candidate_framework" > "$ACTIVE/candidate_framework"

# Resolve only after installing the requested framework. A release replacing
# broken deployment code executes its own post-deploy body, not N-1's.
candidate_post="$(python3 -m mojo.deploy locate post_deploy.sh)" ||
    die "cannot locate candidate post-deploy body"
cp -f -- "$candidate_post" "$ACTIVE/candidate_post.sh"
chmod 0700 "$ACTIVE/candidate_post.sh"

post_args=(--activate --node-type "$NODE_TYPE" --state "$ACTIVE")
[ "$MIGRATE" = "0" ] || post_args+=(--migrate)
candidate_entry="$candidate_post"
if [ -f "$PROJ_PATH/aws/post_deploy.sh" ]; then
    candidate_entry="$PROJ_PATH/aws/post_deploy.sh"
fi
bash "$candidate_entry" "${post_args[@]}"

TARGET_SHA="$(git_project rev-parse HEAD)"
if [ "$MANUAL" = "0" ]; then
    identity="$PROJ_PATH/var/deploy_identity.json.tmp.$$"
    printf '{"schema":3,"sha":"%s","deployment":"%s","node_type":"%s"}\n' \
        "$TARGET_SHA" "$DEPLOYMENT" "$NODE_TYPE" > "$identity"
    chmod 0644 "$identity"
    mv -f "$identity" "$PROJ_PATH/var/deploy_identity.json"
    rm -f "$PROJ_PATH/var/deploy_identity.invalid"
    write_outcome "completed" "" "$TARGET_SHA" "$DEPLOYMENT" "$NODE_TYPE"
fi

trap - ERR TERM INT HUP
rm -rf -- "$ACTIVE"

# One predecessor-generation bridge. New parents own evidence after return;
# an old parent needs the candidate API to report once during adoption.
if [ "$MANUAL" = "0" ] && [ "$NODE_TYPE" = "api" ] \
        && [ -z "${MOJO_DEPLOY_PARENT_STATUS:-}" ]; then
    if [ "$MIGRATE" = "1" ]; then
        MOJO_DEPLOY_IDENTITY_READY=3 python3 bin/manage.py deploy_status set deploying \
            --sha "$TARGET_SHA" --deployment "$DEPLOYMENT"
    fi
fi

log "Deployment ${DEPLOYMENT:-manual} completed at $TARGET_SHA ($NODE_TYPE)"
