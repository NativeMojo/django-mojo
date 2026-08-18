#!/bin/bash
# mojo-deploy-contract: 2
# ^ the argv contract this script speaks. It must equal
#   mojo.apps.edge.services.deploy.DEPLOY_CONTRACT: the deploy plane READS this
#   line before it execs the script and refuses one that declares an older
#   contract. Bump both together, or not at all.
#
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
#   update.sh --contract
#       Print the argv contract version above and exit 0, touching nothing.
#       For an operator or a checker asking "what does this box's script
#       speak"; the deploy plane never uses it (it reads the marker instead).
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

usage() {
    cat >&2 <<'EOF'
usage:
  aws/update.sh --sha <7-40 hex> --framework <version> --deployment <uuid> [--migrate]   (deploy)
  aws/update.sh --manual                                             (hands-on)
  aws/update.sh --contract                                           (print argv contract)

A bare invocation is refused: deploys are driven by the fleet orchestrator
(push to the deploy branch, or POST /api/edge/deploy). For a one-box manual
update use --manual.
EOF
}

# ── contract probe ───────────────────────────────────────────────────────────
# Answered BEFORE the cd, and before anything else: "which contract does this
# box's script speak" has to be answerable on a box whose PROJ_PATH does not
# exist yet, and answering it must touch nothing — no cd, no lock, no git, no
# status write. Combined with any other flag it is a usage error, so it can
# never become a half-run deploy. The integer below and the
# `mojo-deploy-contract` marker at the top of this file are one number; the
# harness asserts they agree.

for arg in "$@"; do
    if [ "$arg" = "--contract" ]; then
        [ "$#" -eq 1 ] || { usage; exit 2; }
        echo "2"
        exit 0
    fi
done

PROJ_PATH="${PROJ_PATH:-/opt/api}"
cd "$PROJ_PATH" || { echo "FATAL: cannot cd to $PROJ_PATH" >&2; exit 1; }

SANITY_URL="${SANITY_URL:-http://127.0.0.1/api/version}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" | tee -a var/update.log; }

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
    if [ "$state" = "deploying" ]; then
        MOJO_DEPLOY_IDENTITY_READY=2 python3 bin/manage.py deploy_status set \
            "$state" --sha "$sha" --deployment "$DEPLOYMENT" || rc=$?
    elif [ -n "$detail" ]; then
        python3 bin/manage.py deploy_status set "$state" --sha "$sha" \
            --deployment "$DEPLOYMENT" --detail "$detail" || rc=$?
    else
        python3 bin/manage.py deploy_status set "$state" --sha "$sha" --deployment "$DEPLOYMENT" || rc=$?
    fi
    if [ "$rc" = "3" ]; then
        log "deploy_status: deploy superseded — report ignored (tolerated)"
        return 0
    fi
    return "$rc"
}

# ── atomic deployment identity ──────────────────────────────────────────────

PREV_IDENTITY_SHA=""
PREV_IDENTITY_UUID=""

parse_identity() { # canonical-json
    local value="$1"
    local pattern='^\{"schema":2,"sha":"([0-9a-f]{40})","deployment":"([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"\}$'
    [[ "$value" =~ $pattern ]] || return 1
    PREV_IDENTITY_SHA="${BASH_REMATCH[1]}"
    PREV_IDENTITY_UUID="${BASH_REMATCH[2]}"
}

write_identity_file() { # sha uuid destination
    local sha="$1" deployment="$2" destination="$3"
    local temp="var/.deploy_identity.$$.tmp"
    (umask 022; printf '{"schema":2,"sha":"%s","deployment":"%s"}\n' \
        "$sha" "$deployment" > "$temp") || return 1
    chmod 0644 "$temp" || { rm -f "$temp"; return 1; }
    mv -f "$temp" "$destination" || { rm -f "$temp"; return 1; }
}

snapshot_previous_identity() {
    local value="" sha="" deployment=""
    rm -f var/previous_deploy_identity.json
    if [ -e var/deploy_identity.json ]; then
        value="$(head -c 4097 var/deploy_identity.json 2>/dev/null)"
        [ "${#value}" -le 4096 ] && parse_identity "$value" || return 0
    elif [ ! -e var/deploy_identity.invalid ]; then
        sha="$(head -c 129 var/deploy_sha 2>/dev/null | tr -d '\r\n')"
        deployment="$(head -c 129 var/deployment_uuid 2>/dev/null | tr -d '\r\n')"
        if [[ "$sha" =~ ^[0-9a-f]{40}$ ]] && \
                [[ "$deployment" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
            PREV_IDENTITY_SHA="$sha"
            PREV_IDENTITY_UUID="$deployment"
        fi
    fi
    if [ -n "$PREV_IDENTITY_SHA" ] && [ -n "$PREV_IDENTITY_UUID" ]; then
        write_identity_file "$PREV_IDENTITY_SHA" "$PREV_IDENTITY_UUID" \
            var/previous_deploy_identity.json || return 1
    fi
}

invalidate_identity() {
    local temp="var/.deploy_identity_invalid.$$.tmp"
    (umask 022; printf 'invalid\n' > "$temp") || return 1
    chmod 0644 "$temp" || { rm -f "$temp"; return 1; }
    mv -f "$temp" var/deploy_identity.invalid \
        || { rm -f "$temp"; return 1; }
    rm -f var/deploy_identity.json var/deploy_sha var/deployment_uuid
}

publish_identity() { # full-sha uuid
    write_identity_file "$1" "$2" var/deploy_identity.json || return 1
    rm -f var/deploy_identity.invalid var/deploy_sha var/deployment_uuid
}

# ── deploy mode ──────────────────────────────────────────────────────────────

if [ "$MODE" = "deploy" ]; then
    HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
    CURRENT_FRAMEWORK="$(python3 -c 'import mojo; print(mojo.__version__)' 2>/dev/null || echo "")"

    # A same-code delivery skips fetch/install, but not deployment identity.
    # A fresh UUID is a fresh attempt and must still prove itself, report the
    # callback (including a single-runner non-migrate delivery), and reach the
    # normal self-stop tail.
    SAME_RELEASE=0
    case "$HEAD_SHA" in
        "$SHA"*)
            if [ -n "$SHA" ] && [ "$CURRENT_FRAMEWORK" = "$FRAMEWORK" ]; then
                SAME_RELEASE=1
                log "already on ${SHA} / django-mojo ${FRAMEWORK} — refreshing deployment identity"
            fi
            ;;
    esac

    # Rollback state, captured before anything moves.
    PREV_SHA="$HEAD_SHA"
    PREV_FRAMEWORK="$CURRENT_FRAMEWORK"
    echo "$PREV_SHA" > var/previous_sha
    echo "$PREV_FRAMEWORK" > var/previous_framework
    snapshot_previous_identity || { log "could not snapshot prior deployment identity"; exit 1; }
    if [ "$SAME_RELEASE" = "1" ] && [ "$PREV_IDENTITY_SHA" = "$HEAD_SHA" ] \
            && [ "$PREV_IDENTITY_UUID" = "$DEPLOYMENT" ]; then
        log "deployment ${DEPLOYMENT} is already proven — nothing to do"
        exit 0
    fi

    fail_deploy() {
        # fail_deploy <step that failed>
        local step="$1"
        log "deploy of ${SHA} failed at: ${step}"
        if [ "$MIGRATE" = "1" ] || [ "$SAME_RELEASE" = "1" ]; then
            # Report FIRST — the rollback below may reinstall a framework
            # that has no deploy_status command.
            report_status failed "$SHA" "$step" || true
            invalidate_identity || log "could not publish identity invalidation marker"
            rollback_ok=0
            if [ "$SAME_RELEASE" = "1" ]; then
                rollback_ok=1
            elif [ -n "$PREV_SHA" ] && [ -n "$PREV_FRAMEWORK" ]; then
                log "rolling back to ${PREV_SHA} / django-mojo ${PREV_FRAMEWORK}"
                if git reset --hard "$PREV_SHA" \
                        && sudo bash ./aws/post_deploy.sh --framework "$PREV_FRAMEWORK" \
                        && python3 bin/manage.py sanity_check --url "$SANITY_URL"; then
                    rollback_ok=1
                else
                    log "ROLLBACK FAILED — this node is in an unknown state"
                    report_status failed "$SHA" "rollback failed" || true
                fi
            else
                log "no previous state recorded — cannot roll back"
                report_status failed "$SHA" "rollback impossible: no previous state" || true
            fi
            if [ "$rollback_ok" = "1" ] && [ -n "$PREV_IDENTITY_SHA" ] \
                    && [ -n "$PREV_IDENTITY_UUID" ]; then
                publish_identity "$PREV_IDENTITY_SHA" "$PREV_IDENTITY_UUID" \
                    || log "rollback succeeded but prior identity restore failed"
            fi
            # The new engine finalizes this exact terminal UUID and releases
            # any queued successor. Keep the self-stop as the last command.
            ./bin/jobman stop >> var/update.log 2>&1
        fi
        # Fleet (non-migrate) runs neither report nor roll back: exiting
        # non-zero here happens BEFORE jobman stop, so the still-alive
        # deploy_node job files the incident.
        exit 1
    }

    log "UPDATE STARTED deployment=${DEPLOYMENT} sha=${SHA} framework=${FRAMEWORK} migrate=${MIGRATE}"
    if [ "$SAME_RELEASE" != "1" ]; then
        invalidate_identity || fail_deploy "identity invalidation"
        git fetch origin                    || fail_deploy "git fetch"
        git reset --hard "$SHA"             || fail_deploy "git reset to ${SHA}"
        git clean -fd                       || fail_deploy "git clean"
        if [ "$MIGRATE" = "1" ]; then
            sudo bash ./aws/post_deploy.sh --framework "$FRAMEWORK" --migrate \
                                            || fail_deploy "post_deploy (migrate)"
        else
            sudo bash ./aws/post_deploy.sh --framework "$FRAMEWORK" \
                                            || fail_deploy "post_deploy"
        fi
    fi

    if [ "$MIGRATE" = "1" ] || [ "$SAME_RELEASE" = "1" ]; then
        python3 bin/manage.py sanity_check --url "$SANITY_URL" \
                                        || fail_deploy "sanity_check"
    fi

    LIVE_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
    if ! [[ "$LIVE_SHA" =~ ^[0-9a-f]{40}$ ]] || [[ "$LIVE_SHA" != "$SHA"* ]]; then
        fail_deploy "identity sha mismatch"
    fi
    publish_identity "$LIVE_SHA" "$DEPLOYMENT" || fail_deploy "identity publish"

    if [ "$MIGRATE" = "1" ] || [ "$SAME_RELEASE" = "1" ]; then
        # A hard failure here (not exit 3) rolls back and leaves no candidate
        # proof. v2 tells the command that the atomic identity is now readable.
        report_status deploying "$SHA"  || fail_deploy "deploy_status report"
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
# LAST, and redirected — see the header. Cron's `jobman start` brings the
# engine back on the new code within a minute.
./bin/jobman stop >> var/update.log 2>&1
