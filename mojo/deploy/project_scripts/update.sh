#!/bin/bash
# Stable project-owned deploy launcher. It deliberately uses git, pip and
# shell only until the candidate release is checked out.

set -euo pipefail

PROJ_PATH="${PROJ_PATH:-/opt/api}"
LOCK_FILE="${PROJ_PATH}/var/update.lock"
ACTIVE_TRANSACTION="${PROJ_PATH}/var/deploy-rollback/active"

usage() {
    echo "usage: update.sh --sha <commit> --framework <version> --deployment <uuid> [--migrate] | --manual" >&2
}

die() {
    echo "FATAL: $*" >&2
    exit 1
}

installed_framework() {
    python3 -m pip show django-mojo 2>/dev/null | sed -n 's/^Version:[[:space:]]*//p' | head -1
}

recover_if_needed() {
    if [ -d "$ACTIVE_TRANSACTION" ]; then
        echo "Recovering interrupted deployment before starting another..."
        sudo bash "$PROJ_PATH/aws/post_deploy.sh" --recover-only
    fi
}

SHA=""
FRAMEWORK=""
DEPLOYMENT=""
MIGRATE=0
MANUAL=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --sha) SHA="${2:-}"; shift 2 ;;
        --framework) FRAMEWORK="${2:-}"; shift 2 ;;
        --deployment) DEPLOYMENT="${2:-}"; shift 2 ;;
        --migrate) MIGRATE=1; shift ;;
        --manual) MANUAL=1; shift ;;
        *) usage; exit 2 ;;
    esac
done

if [ "$MANUAL" = "1" ]; then
    if [ -n "$SHA$FRAMEWORK$DEPLOYMENT" ] || [ "$MIGRATE" != "0" ]; then
        usage
        exit 2
    fi
else
    if [ -z "$SHA" ] || [ -z "$FRAMEWORK" ] || [ -z "$DEPLOYMENT" ]; then
        usage
        exit 2
    fi
    case "$SHA" in *[!0-9a-fA-F]*|"") die "invalid commit SHA" ;; esac
    [ "${#SHA}" -ge 7 ] && [ "${#SHA}" -le 40 ] || die "invalid commit SHA"
    case "$FRAMEWORK" in *[!0-9A-Za-z._!+-]*|"") die "invalid framework version" ;; esac
fi

cd "$PROJ_PATH"
mkdir -p var
exec 9>"$LOCK_FILE"
if [ "$MANUAL" = "1" ]; then
    flock -n 9 || die "another update is running"
else
    flock 9
fi

recover_if_needed

PREVIOUS_SHA="$(git rev-parse HEAD)" || die "cannot read current commit"
PREVIOUS_FRAMEWORK="$(installed_framework)"
[ -n "$PREVIOUS_FRAMEWORK" ] || die "cannot read installed django-mojo version"

git fetch --prune origin || die "git fetch failed"
if [ "$MANUAL" = "1" ]; then
    SHA="$(git rev-parse origin/main)" || die "cannot resolve origin/main"
fi
git cat-file -e "${SHA}^{commit}" 2>/dev/null || die "target commit is unavailable"
git clean -fd
git checkout --force "$SHA"

[ -f ./aws/post_deploy.sh ] || {
    git checkout --force "$PREVIOUS_SHA" >/dev/null 2>&1 || true
    die "candidate has no aws/post_deploy.sh"
}

args=()
[ "$MANUAL" = "1" ] || args+=(--framework "$FRAMEWORK")
[ "$MIGRATE" = "0" ] || args+=(--migrate)

export MOJO_PREVIOUS_SHA="$PREVIOUS_SHA"
export MOJO_PREVIOUS_FRAMEWORK="$PREVIOUS_FRAMEWORK"
if ! sudo -E bash ./aws/post_deploy.sh "${args[@]}"; then
    # post_deploy owns rollback once invoked. Re-check its one mechanical
    # promise so a broken rollback cannot be mistaken for success.
    current="$(git rev-parse HEAD 2>/dev/null || true)"
    [ "$current" = "$PREVIOUS_SHA" ] ||
        die "post-deploy failed and did not restore $PREVIOUS_SHA"
    exit 1
fi

TARGET_SHA="$(git rev-parse HEAD)"
if [ "$MANUAL" = "0" ]; then
    identity="var/deploy_identity.json.tmp.$$"
    printf '{"schema":2,"sha":"%s","deployment":"%s"}\n' "$TARGET_SHA" "$DEPLOYMENT" > "$identity"
    chmod 0644 "$identity"
    mv -f "$identity" var/deploy_identity.json
    rm -f var/deploy_identity.invalid
fi

# One-release bridge for an engine still running the predecessor framework.
# New parents set MOJO_DEPLOY_PARENT_STATUS and own the callback/recycle after
# this process returns. Old parents do not, so the verified candidate uses the
# old callback once and recycles the engine after its job can return.
if [ "$MANUAL" = "0" ] && [ -z "${MOJO_DEPLOY_PARENT_STATUS:-}" ]; then
    if [ "$MIGRATE" = "1" ]; then
        MOJO_DEPLOY_IDENTITY_READY=2 python3 bin/manage.py deploy_status set deploying --sha "$TARGET_SHA" --deployment "$DEPLOYMENT"
    fi
    nohup bash -c 'sleep 2; python3 -m mojo.deploy.jobman stop engine --root "$1" --grace 2; sleep 1; python3 -m mojo.deploy.jobman start engine --root "$1"' deploy-recycle "$PROJ_PATH" >/dev/null 2>&1 &
fi

echo "Deployment ${DEPLOYMENT:-manual} completed at $SHA"
