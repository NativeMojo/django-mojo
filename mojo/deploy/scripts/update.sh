#!/bin/bash
# Node-side fleet update — the framework half of the django-mojo edge deploy
# plane, shipped inside the django-mojo package (mojo/deploy/scripts/) and
# executed through each project's aws/update.sh shim. The authoritative
# contract is django-mojo docs/django_developer/edge/deploy.md; the shim
# contract is docs/django_developer/deploy/README.md.
#
# THIS FILE IS REPLACED MID-RUN, AND THAT IS FINE: the post_deploy step below
# pip-installs a (possibly newer) django-mojo, which swaps this file for a new
# inode inside site-packages. The bash executing this run keeps its fd on the
# OLD inode, so the in-flight run completes on the code it started with; the
# NEXT run resolves the new copy through the shim's `locate`.
#
# Modes:
#   update.sh --sha <7-40 hex> --framework <version> --deployment <uuid> [--migrate]
#       Deploy mode — what the engine's deploy_node job invokes. Checks out
#       the NAMED commit (never origin/main), installs the PINNED framework
#       version, and reports terminal status via `manage.py deploy_status`.
#       --migrate marks the canary run: locked migration, sanity check, and
#       rollback-with-report on failure.
#   update.sh --manual
#       The hands-on path for one box: origin/main, latest framework, no
#       migration, NO status writes. For "ssh in and fix this node".
#   update.sh
#       A usage error on purpose — a bare muscle-memory run mid-deploy must
#       not race the fleet with untracked state.
#
# Project inputs (exported by the shim before it execs this file):
#   PROJ_PATH    the deployed tree                (default /opt/api)
#   SANITY_URL   the URL sanity_check must probe  (default http://127.0.0.1/api/version)
#
# Structure the engine depends on (do not reorder casually):
#   - The script reports terminal status itself: the `jobman stop` at the end
#     kills the engine running the deploy_node job that shelled us, so no
#     Python after our exit ever runs on this box.
#   - On a --migrate failure we report `failed` BEFORE rolling back — the
#     rollback may reinstall a framework version that predates the
#     deploy_status command, so the report happens while the tool exists.
#   - `jobman stop` stays LAST and is output-redirected: deploy_node captures
#     our stdout through pipes whose read end dies with the engine, and
#     jobman's own echo after killing it would SIGPIPE the stop script
#     between "stop engine" and "stop scheduler".

PROJ_PATH="${PROJ_PATH:-/opt/api}"
cd "$PROJ_PATH" || { echo "FATAL: cannot cd to $PROJ_PATH" >&2; exit 1; }

SANITY_URL="${SANITY_URL:-http://127.0.0.1/api/version}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" | tee -a var/update.log; }

usage() {
    cat >&2 <<'EOF'
usage:
  aws/update.sh --sha <7-40 hex> --framework <version> --deployment <uuid> [--migrate]   (deploy)
  aws/update.sh --manual                                             (hands-on)

A bare invocation is refused: deploys are driven by the fleet orchestrator
(push to the deploy branch, or POST /api/edge/deploy). For a one-box manual
update use --manual.
EOF
}

# ── argument parsing ─────────────────────────────────────────────────────────

MODE=""
SHA=""
FRAMEWORK=""
MIGRATE=0
DEPLOYMENT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --sha)       SHA="${2:-}"; shift 2 || { usage; exit 2; } ;;
        --framework) FRAMEWORK="${2:-}"; shift 2 || { usage; exit 2; } ;;
        --deployment) DEPLOYMENT="${2:-}"; shift 2 || { usage; exit 2; } ;;
        --migrate)   MIGRATE=1; shift ;;
        --manual)    MODE="manual"; shift ;;
        *)           usage; exit 2 ;;
    esac
done

if [ "$MODE" = "manual" ]; then
    if [ -n "$SHA" ] || [ -n "$FRAMEWORK" ] || [ -n "$DEPLOYMENT" ] || [ "$MIGRATE" = "1" ]; then
        usage; exit 2
    fi
elif [ -n "$SHA" ] || [ -n "$FRAMEWORK" ]; then
    MODE="deploy"
    # Mirrors deploy_node's own validation — defense in depth before anything
    # enters a git or pip argv.
    if ! [[ "$SHA" =~ ^[0-9a-f]{7,40}$ ]] || [[ "$SHA" =~ ^0+$ ]]; then
        echo "invalid --sha: ${SHA}" >&2; usage; exit 2
    fi
    if ! [[ "$FRAMEWORK" =~ ^[A-Za-z0-9][A-Za-z0-9._!+-]{0,63}$ ]]; then
        echo "invalid --framework: ${FRAMEWORK}" >&2; usage; exit 2
    fi
    if ! [[ "$DEPLOYMENT" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$ ]]; then
        echo "invalid --deployment: ${DEPLOYMENT}" >&2; usage; exit 2
    fi
else
    usage; exit 2
fi

# ── one run per box ──────────────────────────────────────────────────────────
# Deploy mode WAITS (a chained deploy queues behind a canary rollback instead
# of false-failing); timeout exits non-zero so the still-alive deploy_node
# files an accurate incident. Manual mode fails fast at the prompt. The lock
# fd dies with the process; var/ is gitignored so `git clean -fd` cannot
# unlink the lock file under a live run.

exec 9>>var/update.lock
if [ "$MODE" = "deploy" ]; then
    flock -w 600 9 || { log "another update held the lock for 600s — giving up"; exit 1; }
else
    flock -n 9 || { log "another update is in flight on this box"; exit 1; }
fi

# ── deploy status reporting ──────────────────────────────────────────────────
# Exit 3 from deploy_status means "this deploy was superseded" — stale by
# design, never a script failure. Any other non-zero propagates to the caller.

report_status() {
    # report_status <deploying|failed> <sha> [detail]
    local state="$1" sha="$2" detail="${3:-}" rc=0
    if [ -n "$detail" ]; then
        python3 bin/manage.py deploy_status set "$state" --sha "$sha" --deployment "$DEPLOYMENT" --detail "$detail" || rc=$?
    else
        python3 bin/manage.py deploy_status set "$state" --sha "$sha" --deployment "$DEPLOYMENT" || rc=$?
    fi
    if [ "$rc" = "3" ]; then
        log "deploy_status: deploy superseded — report ignored (tolerated)"
        return 0
    fi
    return "$rc"
}

# ── deploy mode ──────────────────────────────────────────────────────────────

if [ "$MODE" = "deploy" ]; then
    HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
    CURRENT_FRAMEWORK="$(python3 -c 'import mojo; print(mojo.__version__)' 2>/dev/null || echo "")"

    # Short-circuit: a redelivered ghost or duplicate publish becomes a no-op.
    # Prefix match on the sha — the manual deploy endpoint accepts abbreviated
    # shas and HEAD is always full. The framework must match too: a
    # framework-only re-pin still runs.
    case "$HEAD_SHA" in
        "$SHA"*)
            if [ -n "$SHA" ] && [ "$CURRENT_FRAMEWORK" = "$FRAMEWORK" ]; then
                log "already on ${SHA} / django-mojo ${FRAMEWORK} — nothing to do"
                exit 0
            fi
            ;;
    esac

    # Rollback state, captured before anything moves.
    PREV_SHA="$HEAD_SHA"
    PREV_FRAMEWORK="$CURRENT_FRAMEWORK"
    echo "$PREV_SHA" > var/previous_sha
    echo "$PREV_FRAMEWORK" > var/previous_framework

    fail_deploy() {
        # fail_deploy <step that failed>
        local step="$1"
        log "deploy of ${SHA} failed at: ${step}"
        if [ "$MIGRATE" = "1" ]; then
            # Report FIRST — the rollback below may reinstall a framework
            # that has no deploy_status command.
            report_status failed "$SHA" "$step" || true
            if [ -n "$PREV_SHA" ] && [ -n "$PREV_FRAMEWORK" ]; then
                log "rolling back to ${PREV_SHA} / django-mojo ${PREV_FRAMEWORK}"
                if git reset --hard "$PREV_SHA" \
                        && sudo bash ./aws/post_deploy.sh --framework "$PREV_FRAMEWORK"; then
                    python3 bin/manage.py sanity_check --url "$SANITY_URL" \
                        || log "rollback landed but its sanity_check failed"
                else
                    log "ROLLBACK FAILED — this node is in an unknown state"
                    report_status failed "$SHA" "rollback failed" || true
                fi
            else
                log "no previous state recorded — cannot roll back"
                report_status failed "$SHA" "rollback impossible: no previous state" || true
            fi
        fi
        # Fleet (non-migrate) runs neither report nor roll back: exiting
        # non-zero here happens BEFORE jobman stop, so the still-alive
        # deploy_node job files the incident.
        exit 1
    }

    log "UPDATE STARTED deployment=${DEPLOYMENT} sha=${SHA} framework=${FRAMEWORK} migrate=${MIGRATE}"
    git fetch origin                    || fail_deploy "git fetch"
    git reset --hard "$SHA"             || fail_deploy "git reset to ${SHA}"
    git clean -fd                       || fail_deploy "git clean"

    if [ "$MIGRATE" = "1" ]; then
        sudo bash ./aws/post_deploy.sh --framework "$FRAMEWORK" --migrate \
                                        || fail_deploy "post_deploy (migrate)"
        python3 bin/manage.py sanity_check --url "$SANITY_URL" \
                                        || fail_deploy "sanity_check"
        # A hard failure here (not exit 3) exits non-zero before jobman stop,
        # so deploy_node reports it — the release is installed but the
        # orchestrator was never told, which must be loud, not silent.
        report_status deploying "$SHA"  || fail_deploy "deploy_status report"
    else
        sudo bash ./aws/post_deploy.sh --framework "$FRAMEWORK" \
                                        || fail_deploy "post_deploy"
    fi

# ── manual mode ──────────────────────────────────────────────────────────────

else
    log "MANUAL UPDATE STARTED (origin/main, latest framework)"
    git fetch origin                    || { log "manual update failed: git fetch"; exit 1; }
    git reset --hard origin/main        || { log "manual update failed: git reset"; exit 1; }
    git clean -fd                       || { log "manual update failed: git clean"; exit 1; }
    sudo bash ./aws/post_deploy.sh      || { log "manual update failed: post_deploy"; exit 1; }
fi

# ── common tail ──────────────────────────────────────────────────────────────

VERSION="$(grep '^__version__' config/settings/version.py | cut -d '"' -f 2)"
log "system now at: ${VERSION}"
echo "$VERSION" > var/version
if [ "$MODE" = "deploy" ]; then
    echo "$SHA" > var/deploy_sha
    echo "$DEPLOYMENT" > var/deployment_uuid
fi

# LAST, and redirected — see the header. Cron's `jobman start` brings the
# engine back on the new code within a minute.
./bin/jobman stop >> var/update.log 2>&1
