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
#   - The script reports terminal status itself: `restart_engine` at the end
#     kills the engine running the deploy_node job that shelled us, so no
#     Python after our exit ever runs on this box.
#   - On a --migrate failure we report `failed` BEFORE rolling back — the
#     rollback may reinstall a framework version that predates the
#     deploy_status command, so the report happens while the tool exists.
#   - `restart_engine` stays LAST and every command in it is output-redirected:
#     deploy_node captures our stdout through pipes whose read end dies with
#     the engine, and anything echoed after killing it would SIGPIPE the tail
#     between "stop engine" and "stop scheduler".
#   - It hands the deploy_node job row off BEFORE the stop (while an engine
#     still exists to do it), stops both components, and starts them again as
#     the ENGINE'S OWN USER — never as the root this script runs as. Cron's
#     every-minute `jobman start` stays the backstop, and is the whole restart
#     whenever the engine user cannot be resolved.

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

# Written straight to the log, never to stdout: everything in the restart tail
# below runs after the job engine has been killed, and stdout is a pipe whose
# read end died with it. `log` tees to stdout.
log_quietly() {
    printf '%s: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> var/update.log 2>/dev/null
}

# ── phase timing ─────────────────────────────────────────────────────────────
#
# Where the seconds went, appended a line at a time to var/deploy/phase_timings
# and carried to the platform in the terminal callback. Both node scripts write
# the same file, and post_deploy.sh reads the PASS from var/deploy/phase_pass
# rather than from argv: a rollback re-enters post_deploy.sh, and giving that
# script a new flag would break the one case that matters — a rollback whose
# pip has already downgraded the framework, leaving the shim to locate an OLDER
# post_deploy.sh that would die on an argument it has never heard of.
#
#   <pass> <name> <value> <unit>      e.g.  deploy post_deploy 41230 ms
#
# THE CLOCK IS PROBED, NOT ASSUMED. `date +%s%3N` is a GNU extension; where it
# is unsupported the `%3N` is echoed literally ("17872374243N"), so the answer
# is checked for SHAPE — all digits — and seconds are used when it is not. A
# second-resolution timing is still worth having. Nothing here may fail a
# deploy: every write is `|| true`, and every arithmetic use is guarded,
# because post_deploy.sh runs under `set -e` where `((X++))` returning 1 is
# fatal.
PHASE_DIR="var/deploy"
PHASE_FILE="${PHASE_DIR}/phase_timings"
PHASE_PASS_FILE="${PHASE_DIR}/phase_pass"
PHASE_PASS="deploy"
PHASE_NAME=""
PHASE_START=""
_phase_probe="$(date +%s%3N 2>/dev/null || true)"
case "$_phase_probe" in
    ''|*[!0-9]*) PHASE_MS=0 ;;
    *)           PHASE_MS=1 ;;
esac

phase_now() {
    if [ "$PHASE_MS" = "1" ]; then
        date +%s%3N 2>/dev/null || true
    else
        date +%s 2>/dev/null || true
    fi
}

phase_reset() { # pass — which half of the run the following phases belong to
    PHASE_PASS="$1"
    mkdir -p "$PHASE_DIR" 2>/dev/null || true
    printf '%s\n' "$1" 2>/dev/null > "$PHASE_PASS_FILE" || true
    return 0
}

phase_truncate() {
    mkdir -p "$PHASE_DIR" 2>/dev/null || true
    : 2>/dev/null > "$PHASE_FILE" || true
    return 0
}

phase_begin() { # name
    PHASE_NAME="$1"
    PHASE_START="$(phase_now)"
    return 0
}

phase_record() { # name start — for a span whose start was kept elsewhere
    local name="$1" start="$2" end="" delta=0 unit="s"
    case "$start" in ''|*[!0-9]*) return 0 ;; esac
    end="$(phase_now)"
    case "$end" in ''|*[!0-9]*) return 0 ;; esac
    if [ "$PHASE_MS" = "1" ]; then unit="ms"; fi
    delta=$(( end - start ))
    [ "$delta" -ge 0 ] 2>/dev/null || delta=0
    # 2>/dev/null FIRST. Redirections apply left to right, so with the append
    # first, a failure to OPEN the file is reported by the shell to a stderr
    # that has not been silenced yet — which is how an unwritable timings file
    # printed "Permission denied" into the middle of every deploy's output
    # despite the `|| true` this line already had.
    printf '%s %s %s %s\n' "$PHASE_PASS" "$name" "$delta" "$unit" \
        2>/dev/null >> "$PHASE_FILE" || true
    return 0
}

phase_end() { # [name]
    phase_record "${1:-$PHASE_NAME}" "$PHASE_START"
    PHASE_START=""
    return 0
}

# --phases is PROBED, never assumed. A rollback can reinstall a framework whose
# deploy_status has no such flag, and argparse would exit 2 — which report_status
# propagates as a node failure on a deploy that actually worked.
PHASE_ARGS=()
PHASE_FLAG_SUPPORTED=""
set_phase_args() {
    PHASE_ARGS=()
    [ -s "$PHASE_FILE" ] || return 0
    if [ -z "$PHASE_FLAG_SUPPORTED" ]; then
        if python3 bin/manage.py deploy_status set --help 2>/dev/null \
                | grep -q -- '--phases'; then
            PHASE_FLAG_SUPPORTED=1
        else
            PHASE_FLAG_SUPPORTED=0
        fi
    fi
    [ "$PHASE_FLAG_SUPPORTED" = "1" ] || return 0
    PHASE_ARGS=(--phases "$PHASE_FILE")
    return 0
}

# ── who owns the job engine ──────────────────────────────────────────────────
#
# This script runs as ROOT (the deploy plane execs it through `sudo -n`), and
# the engine must not. A root-started engine is the first writer of the
# framework logs in var/logs and leaves them root-owned, after which every
# app-user start fails on the log open — permanently, until someone chowns them
# by hand. APP_USER is post_deploy.sh's input and is NOT in this script's
# environment, so the owner is discovered instead, most-authoritative first:
#
#   1. field 6 of the jobs cron entry — the every-minute `jobman start` names
#      the exact account the FLEET intends to run the engine as. It is a
#      deployed statement of intent, identical on every node, and it is what
#      will own the engine sixty seconds from now no matter what this script
#      decides. Ranking anything above it means the restart can disagree with
#      cron, and the tail exists precisely to pre-empt cron, not to fight it.
#   2. $SUDO_USER — merely whoever ran the sudo we are under. On the deploy
#      path it agrees with the cron entry, so it adds nothing; on the
#      documented manual path (`sudo bash aws/update.sh --manual` from a login
#      account) it DISAGREES, and taking it would start the engine as e.g.
#      `ubuntu`: var/logs and var/pids become ubuntu-owned — the same
#      permanent brick the root ban above prevents, which cron cannot even
#      repair because it sees a live pidfile — and the engine, which executes
#      arbitrary queued work, ends up running as an account that typically
#      carries NOPASSWD:ALL. It is kept only as a fallback for a node whose
#      cron entry is missing or unusable.
#   3. the owner of var/pids — where the engine writes its pidfiles. Last
#      resort: it is evidence of what ran, not of what is meant to run.
#
# `root`, an empty answer and GNU stat's `UNKNOWN` all resolve to NOTHING, and
# nothing means SKIP THE RESTART: cron brings the engine back within the
# minute, which is exactly the behaviour this tail replaces. Guessing wrong is
# how a node gets bricked; guessing not at all costs sixty seconds.
CRON_ETC="${CRON_ETC:-/etc/cron.d}"

valid_engine_user() { # candidate -> 0 only for a real, non-root account
    local uid=""
    case "${1:-}" in
        ""|root|UNKNOWN) return 1 ;;
        # A leading dash is an OPTION to `id`, not a name: `-r`, `-n` and `-u`
        # all survive the charset test below and would make `id` answer about
        # the current user (root) instead of about the candidate.
        -*) return 1 ;;
        *[!A-Za-z0-9_.-]*) return 1 ;;
    esac
    case "$1" in
        *[!0-9]*) ;;
        *) return 1 ;;   # all digits: a uid, not a name we were told to use
    esac
    # `--` so a name is never read as a flag, and the uid rather than just the
    # exit status: the root ban above is a STRING test, and a uid-0 alias
    # (`toor`) is a different string with identical power.
    uid="$(id -u -- "$1" 2>/dev/null)" || return 1
    case "$uid" in
        ""|*[!0-9]*) return 1 ;;
    esac
    [ "$uid" -ne 0 ]
}

resolve_engine_user() {
    local candidate=""
    # `$1 !~ /=/` skips the SHELL= and PATH= assignments above the schedule.
    candidate="$(awk '$1 !~ /^#/ && $1 !~ /=/ && NF >= 7 { print $6; exit }' \
        "${CRON_ETC}/3_mojo_jobs" 2>/dev/null)"
    if valid_engine_user "$candidate"; then printf '%s\n' "$candidate"; return 0; fi
    candidate="${SUDO_USER:-}"
    if valid_engine_user "$candidate"; then printf '%s\n' "$candidate"; return 0; fi
    candidate="$(stat -c %U var/pids 2>/dev/null)" \
        || candidate="$(stat -f %Su var/pids 2>/dev/null)"
    if valid_engine_user "$candidate"; then printf '%s\n' "$candidate"; return 0; fi
    return 1
}

run_as_engine_user() { # command...
    if [ "$(id -un 2>/dev/null)" = "$RESTART_USER" ]; then
        "$@"
    else
        sudo -n -u "$RESTART_USER" "$@"
    fi
}

# ── the restart tail ─────────────────────────────────────────────────────────
#
# LAST, and every command redirected. This kills the engine that is running the
# deploy_node job that shelled us, so nothing after it may write to stdout (a
# pipe to that engine), and nothing after it may be Python on this box.
#
# Order matters: hand the job row off FIRST, while there is still an engine to
# do it, then stop, then start. Every call is `|| true` — a node that has
# proven its release must never fail its deploy over the restart, because cron
# is still the backstop.
restart_engine() {
    RESTART_USER="$(resolve_engine_user)"
    if [ -z "$RESTART_USER" ]; then
        log_quietly "jobman: engine user unresolved — leaving the restart to cron"
        return 0
    fi
    if [ "$MODE" = "deploy" ] && [ -n "$DEPLOYMENT" ]; then
        # --manual falls through this same tail with no DEPLOYMENT at all.
        run_as_engine_user python3 bin/manage.py deploy_status handoff \
            --deployment "$DEPLOYMENT" >> var/update.log 2>&1 || true
    fi
    # The fallback catches a rollback-downgraded jobman that argparse-errors on
    # --grace (exit 2) — better a ten-second stop than no stop at all. The
    # scheduler gets the plain stop: it has no deploy job to release, so the
    # full window costs nothing.
    run_as_engine_user ./bin/jobman stop engine --grace 2 >> var/update.log 2>&1 \
        || run_as_engine_user ./bin/jobman stop engine >> var/update.log 2>&1 \
        || true
    run_as_engine_user ./bin/jobman stop scheduler >> var/update.log 2>&1 || true
    run_as_engine_user ./bin/jobman start >> var/update.log 2>&1 || true
    return 0
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

# The timings belong to THIS run. Truncated behind the lock, so a queued deploy
# cannot erase the file the running one is still appending to.
phase_truncate
phase_reset deploy
RUN_START="$(phase_now)"

# ── deploy status reporting ──────────────────────────────────────────────────
# Exit 3 from deploy_status means "this deploy was superseded" — stale by
# design, never a script failure. Any other non-zero propagates to the caller.

report_status() {
    # report_status <deploying|failed> <sha> [detail] [include-evidence]
    local state="$1" sha="$2" detail="${3:-}" evidence="${4:-0}" rc=0
    local args=()
    set_phase_args
    if [ "$state" = "deploying" ]; then
        MOJO_DEPLOY_IDENTITY_READY=2 python3 bin/manage.py deploy_status set \
            "$state" --sha "$sha" --deployment "$DEPLOYMENT" \
            "${PHASE_ARGS[@]}" || rc=$?
    elif [ -n "$detail" ]; then
        args=(deploy_status set "$state" --sha "$sha" \
              --deployment "$DEPLOYMENT" --detail "$detail")
        [ "$evidence" != "1" ] || args+=(--evidence)
        python3 bin/manage.py "${args[@]}" "${PHASE_ARGS[@]}" || rc=$?
    else
        python3 bin/manage.py deploy_status set "$state" --sha "$sha" \
            --deployment "$DEPLOYMENT" "${PHASE_ARGS[@]}" || rc=$?
    fi
    if [ "$rc" = "3" ]; then
        log "deploy_status: deploy superseded — report ignored (tolerated)"
        return 0
    fi
    return "$rc"
}

# The migrating canary stops its own job engine after reporting terminal
# state, so the parent process can never persist its captured subprocess
# output. Keep one private 64 KiB tail for the management command to sanitize
# into durable evidence before rollback starts.
DEPLOY_FAILURE_OUTPUT="var/deploy_failure_output"
rm -f -- "$DEPLOY_FAILURE_OUTPUT"
cleanup_failure_output() { rm -f -- "$DEPLOY_FAILURE_OUTPUT"; }
trap cleanup_failure_output EXIT

run_with_evidence() {
    local rc=0
    rm -f -- "$DEPLOY_FAILURE_OUTPUT"
    (umask 077; : > "$DEPLOY_FAILURE_OUTPUT") || return 1
    "$@" 2>&1 | tee /dev/stderr | tail -c 65536 > "$DEPLOY_FAILURE_OUTPUT"
    rc="${PIPESTATUS[0]}"
    if [ "$rc" = "0" ]; then
        rm -f -- "$DEPLOY_FAILURE_OUTPUT"
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
        # fail_deploy <step that failed> [captured-output]
        local step="$1" captured="${2:-0}"
        log "deploy of ${SHA} failed at: ${step}"
        if [ "$MIGRATE" = "1" ] || [ "$SAME_RELEASE" = "1" ]; then
            # Report FIRST — the rollback below may reinstall a framework
            # that has no deploy_status command.
            report_status failed "$SHA" "$step" "$captured" || true
            rm -f -- "$DEPLOY_FAILURE_OUTPUT"
            invalidate_identity || log "could not publish identity invalidation marker"
            rollback_ok=0
            if [ "$SAME_RELEASE" = "1" ]; then
                rollback_ok=1
            elif [ -n "$PREV_SHA" ] && [ -n "$PREV_FRAMEWORK" ]; then
                log "rolling back to ${PREV_SHA} / django-mojo ${PREV_FRAMEWORK}"
                # Everything from here belongs to the rollback pass, including
                # the phases post_deploy.sh appends when it re-enters.
                phase_reset rollback
                phase_begin rollback
                if git reset --hard "$PREV_SHA" \
                        && run_with_evidence sudo bash ./aws/post_deploy.sh \
                            --framework "$PREV_FRAMEWORK" \
                        && run_with_evidence python3 bin/manage.py sanity_check \
                            --url "$SANITY_URL"; then
                    phase_end
                    rollback_ok=1
                else
                    phase_end
                    log "ROLLBACK FAILED — this node is in an unknown state"
                    report_status failed "$SHA" "rollback failed" 1 || true
                    rm -f -- "$DEPLOY_FAILURE_OUTPUT"
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
            # The replacement engine finalizes this exact terminal UUID and
            # releases any queued successor. Keep the restart last.
            restart_engine
        fi
        # Fleet (non-migrate) runs neither report nor roll back: exiting
        # non-zero here happens BEFORE the restart tail, so the still-alive
        # deploy_node job files the incident.
        exit 1
    }

    fail_control_plane() {
        # The candidate application has not proven broken. Leave it serving
        # and let the still-running parent job file its durable node failure;
        # rolling healthy code back because bookkeeping failed creates a
        # second outage without repairing the control plane.
        local step="$1"
        log "deployment control-plane failure at: ${step}; healthy candidate is not rolled back"
        python3 bin/manage.py deploy_warning deployment_control_plane \
            >/dev/null 2>&1 || true
        exit 1
    }

    log "UPDATE STARTED deployment=${DEPLOYMENT} sha=${SHA} framework=${FRAMEWORK} migrate=${MIGRATE}"
    if [ "$SAME_RELEASE" != "1" ]; then
        invalidate_identity || fail_control_plane "identity invalidation"
        phase_begin git_sync
        git fetch origin                    || fail_deploy "git fetch"
        git reset --hard "$SHA"             || fail_deploy "git reset to ${SHA}"
        git clean -fd                       || fail_deploy "git clean"
        phase_end
        phase_begin post_deploy
        if [ "$MIGRATE" = "1" ]; then
            run_with_evidence sudo bash ./aws/post_deploy.sh \
                --framework "$FRAMEWORK" --migrate \
                                            || fail_deploy "post_deploy (migrate)" 1
        else
            run_with_evidence sudo bash ./aws/post_deploy.sh \
                --framework "$FRAMEWORK" \
                                            || fail_deploy "post_deploy" 1
        fi
        phase_end
    fi

    if [ "$MIGRATE" = "1" ] || [ "$SAME_RELEASE" = "1" ]; then
        phase_begin sanity_check
        run_with_evidence python3 bin/manage.py sanity_check --url "$SANITY_URL" \
                                        || fail_deploy "sanity_check" 1
        phase_end
    fi

    phase_begin identity
    LIVE_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
    if ! [[ "$LIVE_SHA" =~ ^[0-9a-f]{40}$ ]] || [[ "$LIVE_SHA" != "$SHA"* ]]; then
        fail_deploy "identity sha mismatch"
    fi
    publish_identity "$LIVE_SHA" "$DEPLOYMENT" || fail_control_plane "identity publish"
    phase_end

    if [ "$MIGRATE" = "1" ] || [ "$SAME_RELEASE" = "1" ]; then
        # Exit 3 means superseded and is tolerated by report_status. Any other
        # callback failure stops fleet progression but leaves the already
        # healthy, identity-published candidate serving. The callback cannot
        # time itself — it is the thing carrying the timings — so the last
        # entry is the whole run up to this point.
        phase_record total "$RUN_START"
        report_status deploying "$SHA"  || fail_control_plane "deploy_status report"
    fi

# ── manual mode ──────────────────────────────────────────────────────────────

else
    log "MANUAL UPDATE STARTED (origin/main, latest framework)"
    phase_begin git_sync
    git fetch origin                    || { log "manual update failed: git fetch"; exit 1; }
    git reset --hard origin/main        || { log "manual update failed: git reset"; exit 1; }
    git clean -fd                       || { log "manual update failed: git clean"; exit 1; }
    phase_end
    phase_begin post_deploy
    sudo bash ./aws/post_deploy.sh      || { log "manual update failed: post_deploy"; exit 1; }
    phase_end
fi

# ── common tail ──────────────────────────────────────────────────────────────

VERSION="$(grep '^__version__' config/settings/version.py | cut -d '"' -f 2)"
log "system now at: ${VERSION}"
echo "$VERSION" > var/version
# LAST, and redirected — see the header. The restart brings the engine back on
# the new code immediately; cron's every-minute `jobman start` remains the
# backstop for the paths that cannot restart it themselves.
restart_engine
