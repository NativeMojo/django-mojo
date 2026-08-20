#!/bin/bash
# Harness for mojo/deploy/scripts/update.sh (maestro item 1612; harness ported
# from mverify_api #1582 / django-mojo-skeleton #1572).
#
# Runs the REAL packaged script in a throwaway PROJ_PATH with every external
# command stubbed on PATH — each stub appends its argv to a call log, and
# per-command control files script exit codes. The properties under test are
# mostly ORDERINGS (release failure reports BEFORE rollback, the engine restart
# LAST) and ABSENCES (fleet runs never touch deploy_status, a short-circuit fetches
# nothing). Packaged delta under test: every sanity_check carries
# --url "$SANITY_URL" — the skeleton default when the shim exports nothing,
# the shim's override otherwise.
#
# macOS has no flock(1); the harness installs an fcntl-based shim only when
# the real util is absent. Same semantics: the lock lives on the inherited
# fd's open file description and survives the shim's exit.
set -u

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PROJ="$TMP/proj"
STUB="$TMP/stubs"
CTL="$TMP/ctl"
export CALLLOG="$TMP/calls.log"
export STUBCTL="$CTL"

REAL_PYTHON3="$(command -v python3)"
REAL_MV="$(command -v mv)"
export REAL_MV

PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

assert_eq() { # actual expected label
    if [ "$1" = "$2" ]; then ok "$3"; else fail "$3 (got: $1, want: $2)"; fi
}
assert_in_log() { # pattern label
    if grep -q -e "$1" "$CALLLOG" 2>/dev/null; then ok "$2"; else fail "$2 (no '$1' in log)"; fi
}
assert_not_in_log() { # pattern label
    if grep -q -e "$1" "$CALLLOG" 2>/dev/null; then fail "$2 ('$1' present)"; else ok "$2"; fi
}
assert_last_cmd() { # pattern label
    local last
    last="$(grep "^CMD" "$CALLLOG" | tail -1)"
    case "$last" in
        *"$1"*) ok "$2" ;;
        *) fail "$2 (last command was: ${last:-none})" ;;
    esac
}
assert_order() { # first_pattern second_pattern label
    local a b
    a="$(grep -n "$1" "$CALLLOG" | head -1 | cut -d: -f1)"
    b="$(grep -n "$2" "$CALLLOG" | head -1 | cut -d: -f1)"
    if [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]; then
        ok "$3"
    else
        fail "$3 ('$1' at ${a:-none}, '$2' at ${b:-none})"
    fi
}
manifest_value() { # key file
    sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p" "$2"
}

# ── stubs ────────────────────────────────────────────────────────────────────

setup_env() {
    rm -rf "$PROJ" "$STUB" "$CTL"
    mkdir -p "$PROJ/var" "$PROJ/bin" "$PROJ/aws" "$PROJ/config/settings" "$STUB" "$CTL"
    : > "$CALLLOG"

    # The shim contract copies the located packaged script into place; the
    # harness plays the shim.
    cp "$REPO/mojo/deploy/scripts/update.sh" "$PROJ/aws/update.sh"
    echo '__version__ = "9.9.9-test"' > "$PROJ/config/settings/version.py"
    : > "$PROJ/bin/manage.py"

    # git: rev-parse prints the controlled HEAD; mutating verbs honor exit ctls.
    cat > "$STUB/git" <<'EOF'
#!/bin/bash
echo "CMD git $*" >> "$CALLLOG"
case "${1:-}" in
    rev-parse) cat "$STUBCTL/head.txt" 2>/dev/null || echo "" ;;
    fetch|reset|clean)
        ctl="$STUBCTL/git_${1}.exit"
        [ -f "$ctl" ] && exit "$(cat "$ctl")"
        if [ "$1" = "reset" ] && [ "${3:-}" != "origin/main" ]; then
            echo "${3:-}" > "$STUBCTL/head.txt"
        fi ;;
esac
exit 0
EOF

    # python3: the framework-version probe, deploy_status and sanity_check.
    cat > "$STUB/python3" <<'EOF'
#!/bin/bash
echo "CMD python3 $*" >> "$CALLLOG"
case "$*" in
    *"import mojo"*) cat "$STUBCTL/framework.txt" 2>/dev/null || echo "0.0.0"; exit 0 ;;
    *"deploy_status set --help"*)
        # The probe update.sh runs before it dares pass --phases. A rollback
        # can restore a deploy_status that has no such flag.
        [ -f "$STUBCTL/deploy_status.no_phases" ] || echo "  --phases PHASES"
        exit 0 ;;
    *deploy_status*)
        echo "ENV identity_ready=${MOJO_DEPLOY_IDENTITY_READY:-}" >> "$CALLLOG"
        if [[ "$*" == *"deploy_status set deploying"* ]] && \
                [ -f "$STUBCTL/deploy_status.fail_deploying" ]; then
            exit 1
        fi
        ctl="$STUBCTL/deploy_status.exit"
        [ -f "$ctl" ] && exit "$(cat "$ctl")" ;;
    *sanity_check*)
        ctl="$STUBCTL/sanity_check.exit"
        [ -f "$ctl" ] && exit "$(cat "$ctl")" ;;
esac
exit 0
EOF

    # sudo: the post_deploy boundary — never executes, records the argv.
    # fail_first: fail exactly one call (the deploy), succeed after (rollback).
    # sleep: hold the flock long enough for a concurrent run to queue.
    cat > "$STUB/sudo" <<'EOF'
#!/bin/bash
echo "CMD sudo $*" >> "$CALLLOG"
# `sudo -n -u <user> <cmd>` is the restart tail dropping root. It is a real
# privilege change on a node, so the harness performs the drop by running the
# command — the tail's own stubs (jobman, python3) then record what ran. The
# post_deploy boundary below is the one that must never execute.
if [ "${1:-}" = "-n" ] && [ "${2:-}" = "-u" ] && [ -n "${3:-}" ]; then
    if [ -f "$STUBCTL/sudo_u.fail" ]; then exit 1; fi
    shift 3
    exec "$@"
fi
[ -f "$STUBCTL/sudo.sleep" ] && sleep "$(cat "$STUBCTL/sudo.sleep")"
if [ -f "$STUBCTL/sudo.fail_first" ] && [ ! -f "$STUBCTL/.sudo_failed_once" ]; then
    touch "$STUBCTL/.sudo_failed_once"
    echo "FATAL: staged post_deploy sentinel" >&2
    exit 1
fi
if [ -f "$STUBCTL/sudo.fail_second" ]; then
    count_file="$STUBCTL/.sudo_count"
    count=0
    [ -f "$count_file" ] && count="$(cat "$count_file")"
    count=$((count+1))
    echo "$count" > "$count_file"
    [ "$count" -eq 2 ] && exit 1
fi
[ -f "$STUBCTL/sudo.exit" ] && exit "$(cat "$STUBCTL/sudo.exit")"
exit 0
EOF

    cat > "$STUB/mv" <<'EOF'
#!/bin/bash
echo "CMD mv $*" >> "$CALLLOG"
dest="${@: -1}"
if [[ "$dest" == */deploy_identity.json ]] && \
        [[ "$dest" != */previous_deploy_identity.json ]] && \
        [ -f "$STUBCTL/mv_identity.fail_once" ] && \
        [ ! -f "$STUBCTL/.mv_identity_failed" ]; then
    touch "$STUBCTL/.mv_identity_failed"
    exit 1
fi
exec "$REAL_MV" "$@"
EOF

    cat > "$PROJ/bin/jobman" <<'EOF'
#!/bin/bash
echo "CMD jobman $*" >> "$CALLLOG"
# A rollback can restore a jobman that predates --grace; argparse exits 2.
case "$*" in
    *--grace*) [ -f "$STUBCTL/jobman.no_grace" ] && exit 2 ;;
esac
exit 0
EOF

    # id: the uid seam. `id -un` is who the tail thinks it is (root on a real
    # node, because the deploy plane execs this script through sudo); `id -u
    # <name>` is whether that account exists at all.
    cat > "$STUB/id" <<'EOF'
#!/bin/bash
case "${1:-}" in
    -un) cat "$STUBCTL/whoami.txt" 2>/dev/null || echo root; exit 0 ;;
    -u)
        [ -n "${2:-}" ] || exit 1
        grep -qxF "${2}" "$STUBCTL/known_users.txt" 2>/dev/null
        exit $? ;;
esac
exit 1
EOF

    # stat: GNU (-c) or BSD (-f) depending on the fixture, so both branches of
    # the last resolution step are reachable.
    cat > "$STUB/stat" <<'EOF'
#!/bin/bash
case "${1:-}" in
    -c) [ -f "$STUBCTL/stat_bsd" ] && exit 1 ;;
    -f) [ -f "$STUBCTL/stat_bsd" ] || exit 1 ;;
    *)  exit 1 ;;
esac
[ -s "$STUBCTL/pids_owner.txt" ] || exit 1
cat "$STUBCTL/pids_owner.txt"
EOF

    chmod +x "$STUB/git" "$STUB/python3" "$STUB/sudo" "$STUB/mv" \
             "$STUB/id" "$STUB/stat" "$PROJ/bin/jobman"

    # flock shim only where the real util is missing (macOS): fcntl.flock on
    # the inherited fd — the lock outlives the shim on the shared OFD. A bash
    # wrapper execs python by absolute path: a shebang pointing at python3
    # fails when python3 is itself a script (macOS execve refuses
    # script-as-interpreter), which is exactly what pyenv/uv shims are.
    if ! command -v flock >/dev/null 2>&1; then
        cat > "$STUB/flock_impl.py" <<'EOF'
import fcntl, sys, time
args = sys.argv[1:]
timeout = None
nonblock = False
if args[0] == "-w":
    timeout = float(args[1])
    fd = int(args[2])
elif args[0] == "-n":
    nonblock = True
    fd = int(args[1])
else:
    fd = int(args[0])
deadline = time.time() + (timeout or 0)
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except OSError:
        if nonblock or (timeout is not None and time.time() >= deadline):
            sys.exit(1)
        time.sleep(0.1)
EOF
        cat > "$STUB/flock" <<EOF
#!/bin/bash
exec "$REAL_PYTHON3" "$STUB/flock_impl.py" "\$@"
EOF
        chmod +x "$STUB/flock"
    fi

    # Deterministic defaults: an old HEAD, a matching installed framework.
    echo "1111111111111111111111111111111111111111" > "$CTL/head.txt"
    echo "1.5.0" > "$CTL/framework.txt"

    # ...and a node shaped like production: this script running as root under
    # sudo, an app user that exists, and a jobs cron entry naming it.
    mkdir -p "$CTL/cron.d"
    echo "root" > "$CTL/whoami.txt"
    echo "$APP_USER" > "$CTL/sudo_user.txt"
    printf '%s\n' "$APP_USER" > "$CTL/known_users.txt"
    echo "$APP_USER" > "$CTL/pids_owner.txt"
    write_jobs_cron "$APP_USER"
}

write_jobs_cron() { # user
    cat > "$CTL/cron.d/3_mojo_jobs" <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
* * * * * $1 $PROJ/bin/jobman start >> $PROJ/var/logs/jobman.log 2>&1
EOF
}

run_update() { # args...
    local args=("$@") joined=" $* "
    if [[ "$joined" == *" --sha "* ]] && [[ "$joined" != *" --deployment "* ]]; then
        args+=(--deployment "$DEPLOYMENT_UUID")
    fi
    ( cd "$TMP" && PROJ_PATH="$PROJ" PATH="$STUB:$PATH" \
        CRON_ETC="$CTL/cron.d" \
        SUDO_USER="$(cat "$CTL/sudo_user.txt" 2>/dev/null)" \
        bash "$PROJ/aws/update.sh" "${args[@]}" )
}

run_update_with_url() { # url args...
    local url="$1"; shift
    local args=("$@") joined=" $* "
    if [[ "$joined" == *" --sha "* ]] && [[ "$joined" != *" --deployment "* ]]; then
        args+=(--deployment "$DEPLOYMENT_UUID")
    fi
    ( cd "$TMP" && SANITY_URL="$url" PROJ_PATH="$PROJ" PATH="$STUB:$PATH" \
        CRON_ETC="$CTL/cron.d" \
        SUDO_USER="$(cat "$CTL/sudo_user.txt" 2>/dev/null)" \
        bash "$PROJ/aws/update.sh" "${args[@]}" )
}

APP_USER="mojo-app"
SHA_NEW="2222222222222222222222222222222222222222"
DEPLOYMENT_UUID="12345678-1234-4123-8123-123456789abc"
PREVIOUS_UUID="87654321-4321-4321-8321-cba987654321"
DEFAULT_URL="http://127.0.0.1/api/version"
SHIM_URL="http://127.0.0.1:8080/api/version"

# ── tests ────────────────────────────────────────────────────────────────────

echo "update.sh: bare invocation is a usage error, nothing executed"
setup_env
run_update >/dev/null 2>&1
assert_eq "$?" 2 "bare invocation exits 2"
assert_eq "$(wc -l < "$CALLLOG" | tr -d ' ')" 0 "no command ran"

echo "update.sh: invalid arguments refused before any action"
setup_env
run_update --sha "main" --framework "1.5.0" >/dev/null 2>&1
assert_eq "$?" 2 "branch name as --sha exits 2"
run_update --sha "0000000000000000000000000000000000000000" --framework "1.5.0" >/dev/null 2>&1
assert_eq "$?" 2 "zero sha exits 2"
run_update --sha "$SHA_NEW" --framework "1.5; rm -rf /" >/dev/null 2>&1
assert_eq "$?" 2 "shell metacharacters in --framework exit 2"
run_update --sha "$SHA_NEW" --framework "1.5.0" --deployment "not-a-uuid" >/dev/null 2>&1
assert_eq "$?" 2 "invalid deployment UUID exits 2"
assert_eq "$(wc -l < "$CALLLOG" | tr -d ' ')" 0 "no command ran for any refused argv"

echo "update.sh: short-circuit when already on target (sha prefix + framework)"
setup_env
echo "deadbeef111111111111111111111111111111ff" > "$CTL/head.txt"
printf '{"schema":2,"sha":"%s","deployment":"%s"}\n' \
    "deadbeef111111111111111111111111111111ff" "$PREVIOUS_UUID" \
    > "$PROJ/var/deploy_identity.json"
run_update --sha "deadbeef" --framework "1.5.0" >/dev/null 2>&1
assert_eq "$?" 0 "short-circuit exits 0"
assert_not_in_log "CMD git fetch" "no fetch on a short-circuit"
assert_not_in_log "CMD sudo bash ./aws/post_deploy.sh" \
    "no post_deploy on a short-circuit"
assert_in_log "deploy_status set deploying" \
    "same-SHA with a fresh UUID still reports terminal intent"
assert_in_log "ENV identity_ready=2" \
    "same-SHA callback carries the fixed v2 identity-ready signal"
assert_eq "$(manifest_value deployment "$PROJ/var/deploy_identity.json")" \
    "$DEPLOYMENT_UUID" "same-SHA publishes the fresh deployment UUID"
assert_last_cmd "jobman start" \
    "same-SHA still reaches the normal restart tail"

echo "update.sh: same SHA/framework/UUID is a true duplicate"
setup_env
echo "deadbeef111111111111111111111111111111ff" > "$CTL/head.txt"
printf '{"schema":2,"sha":"%s","deployment":"%s"}\n' \
    "deadbeef111111111111111111111111111111ff" "$DEPLOYMENT_UUID" \
    > "$PROJ/var/deploy_identity.json"
run_update --sha "deadbeef" --framework "1.5.0" >/dev/null 2>&1
assert_eq "$?" 0 "same-attempt duplicate exits 0"
assert_not_in_log "CMD git fetch" "same-attempt duplicate fetches nothing"
assert_not_in_log "deploy_status" "same-attempt duplicate does not callback twice"
assert_not_in_log "jobman stop" "same-attempt duplicate does not restart the engine"

echo "update.sh: deploy-mode flock waits; --manual fails fast on a held lock"
setup_env
echo "3" > "$CTL/sudo.sleep"
run_update --sha "$SHA_NEW" --framework "1.6.0" >/dev/null 2>&1 &
first_pid=$!
sleep 1
rm -f "$CTL/sudo.sleep"
start=$(date +%s)
run_update --sha "$SHA_NEW" --framework "1.6.0" >/dev/null 2>&1
second_rc=$?
elapsed=$(( $(date +%s) - start ))
wait "$first_pid"
assert_eq "$second_rc" 0 "queued deploy runs after the lock releases"
if [ "$elapsed" -ge 1 ]; then ok "second deploy actually waited (${elapsed}s)"; else fail "second deploy did not wait"; fi
setup_env
echo "3" > "$CTL/sudo.sleep"
run_update --sha "$SHA_NEW" --framework "1.6.0" >/dev/null 2>&1 &
first_pid=$!
sleep 1
run_update --manual >/dev/null 2>&1
assert_eq "$?" 1 "--manual fails fast while a deploy holds the lock"
wait "$first_pid"

echo "update.sh: canary failure reports BEFORE rolling back"
setup_env
touch "$CTL/sudo.fail_first"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 1 "failed canary run exits 1"
assert_order "deploy_status set failed" "CMD git reset --hard 1111111111" \
    "failed report precedes the rollback reset"
assert_in_log "deploy_status set failed.*post_deploy (migrate).*--evidence" \
    "failed canary report carries captured command evidence"
[ ! -e "$PROJ/var/deploy_failure_output" ] && \
    ok "captured deploy output is removed after reporting" || \
    fail "captured deploy output survived the report and rollback"
assert_in_log "CMD sudo bash ./aws/post_deploy.sh --framework 1.5.0$" \
    "rollback reinstalls the previous framework without --migrate"
assert_in_log "sanity_check --url $DEFAULT_URL" \
    "rollback sanity_check carries the default SANITY_URL (packaged delta)"
assert_eq "$(cat "$PROJ/var/previous_sha")" "1111111111111111111111111111111111111111" \
    "previous sha was recorded before anything moved"

echo "update.sh: success order — post_deploy, sanity, deploying report, jobman LAST"
setup_env
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 0 "canary success exits 0"
assert_in_log "CMD sudo bash ./aws/post_deploy.sh --framework 1.6.0 --migrate" \
    "post_deploy invoked with the pinned framework and --migrate"
assert_in_log "sanity_check --url $DEFAULT_URL" \
    "canary sanity_check carries the default SANITY_URL (packaged delta)"
assert_order "post_deploy.sh --framework 1.6.0 --migrate" "sanity_check" \
    "sanity_check runs after the install"
assert_order "sanity_check" "deploy_status set deploying" \
    "deploying is reported only after the sanity check"
assert_order "CMD mv .*deploy_identity.json" "deploy_status set deploying" \
    "the atomic identity lands before v2 success reporting"
assert_in_log "ENV identity_ready=2" \
    "v2 success carries the fixed identity-ready signal"
assert_eq "$(manifest_value sha "$PROJ/var/deploy_identity.json")" \
    "$SHA_NEW" "the manifest records the full live HEAD"
assert_eq "$(manifest_value deployment "$PROJ/var/deploy_identity.json")" \
    "$DEPLOYMENT_UUID" "the manifest records the deployment UUID"
assert_last_cmd "jobman start" \
    "the engine restart is the LAST thing that runs"
assert_order "jobman stop engine" "jobman start" \
    "the engine is stopped before it is started again"

echo "update.sh: identity bookkeeping failure leaves the healthy candidate in place"
setup_env
printf '{"schema":2,"sha":"%s","deployment":"%s"}\n' \
    "1111111111111111111111111111111111111111" "$PREVIOUS_UUID" \
    > "$PROJ/var/deploy_identity.json"
touch "$CTL/mv_identity.fail_once"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 1 "identity publish failure remains nonzero"
assert_not_in_log "deploy_status set deploying" \
    "identity publish failure never announces success"
assert_eq "$(cat "$CTL/head.txt")" "$SHA_NEW" \
    "identity bookkeeping failure does not roll healthy code back"
[ -e "$PROJ/var/deploy_identity.invalid" ] && \
    ok "failed identity publication remains explicitly unproven" || \
    fail "failed identity publication exposed stale proof"
assert_not_in_log "CMD sudo bash ./aws/post_deploy.sh --framework 1.5.0" \
    "identity bookkeeping failure never invokes application rollback"
assert_not_in_log "CMD jobman stop" \
    "the live parent job remains available to persist the control-plane failure"
assert_in_log "manage.py deploy_warning deployment_control_plane" \
    "identity failure attempts a fixed-phase warning incident"

echo "update.sh: callback failure does not roll back a healthy, proven candidate"
setup_env
printf '{"schema":2,"sha":"%s","deployment":"%s"}\n' \
    "1111111111111111111111111111111111111111" "$PREVIOUS_UUID" \
    > "$PROJ/var/deploy_identity.json"
touch "$CTL/deploy_status.fail_deploying"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 1 "callback failure remains nonzero"
assert_order "CMD mv .*deploy_identity.json" "deploy_status set deploying" \
    "candidate identity existed before the injected callback failure"
assert_eq "$(manifest_value deployment "$PROJ/var/deploy_identity.json")" \
    "$DEPLOYMENT_UUID" "callback failure preserves the candidate's coherent proof"
assert_eq "$(cat "$CTL/head.txt")" "$SHA_NEW" \
    "callback failure leaves the healthy candidate code serving"
assert_not_in_log "CMD sudo bash ./aws/post_deploy.sh --framework 1.5.0" \
    "callback failure never invokes application rollback"
assert_not_in_log "CMD jobman stop" \
    "the parent job remains alive to persist the callback failure"

setup_env
printf '{"schema":2,"sha":"%s","deployment":"%s"}\n' \
    "1111111111111111111111111111111111111111" "$PREVIOUS_UUID" \
    > "$PROJ/var/deploy_identity.json"
touch "$CTL/deploy_status.fail_deploying" "$CTL/sudo.fail_second"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 1 "repeated callback failure remains nonzero"
assert_eq "$(manifest_value deployment "$PROJ/var/deploy_identity.json")" \
    "$DEPLOYMENT_UUID" "no rollback path can erase candidate proof"
assert_not_in_log "CMD sudo bash ./aws/post_deploy.sh --framework 1.5.0" \
    "even a prepared rollback failure is unreachable for bookkeeping errors"

echo "update.sh: an exported SANITY_URL overrides the probe on every sanity_check"
setup_env
run_update_with_url "$SHIM_URL" --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 0 "canary success with an overridden SANITY_URL exits 0"
assert_in_log "sanity_check --url $SHIM_URL" \
    "canary sanity_check carries the shim's SANITY_URL"
assert_not_in_log "sanity_check --url $DEFAULT_URL" \
    "the default URL is never probed once the shim overrides it"
setup_env
touch "$CTL/sudo.fail_first"
run_update_with_url "$SHIM_URL" --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 1 "failed canary with an overridden SANITY_URL exits 1"
assert_in_log "sanity_check --url $SHIM_URL" \
    "rollback sanity_check carries the shim's SANITY_URL too"

echo "update.sh: fleet run — no status writes; failure stops before jobman"
setup_env
run_update --sha "$SHA_NEW" --framework "1.6.0" >/dev/null 2>&1
assert_eq "$?" 0 "fleet run exits 0"
assert_not_in_log "deploy_status set" \
    "fleet runs never report status — that is the canary's job"
assert_in_log "deploy_status handoff --deployment $DEPLOYMENT_UUID" \
    "a fleet run still closes its own node job before killing the engine"
setup_env
echo "1" > "$CTL/sudo.exit"
run_update --sha "$SHA_NEW" --framework "1.6.0" >/dev/null 2>&1
assert_eq "$?" 1 "failed fleet run exits 1"
assert_not_in_log "jobman stop" "failed fleet run never reaches jobman stop"
assert_not_in_log "deploy_status" \
    "a failed fleet run reports nothing and never reaches the handoff"

echo "update.sh: --manual is the legacy path, no status writes"
setup_env
run_update --manual >/dev/null 2>&1
assert_eq "$?" 0 "--manual exits 0"
assert_in_log "CMD git reset --hard origin/main" "--manual deploys origin/main"
assert_in_log "CMD sudo bash ./aws/post_deploy.sh$" "--manual runs post_deploy bare"
assert_not_in_log "deploy_status" \
    "--manual writes no status and hands off no deployment it never had"
assert_last_cmd "jobman start" \
    "--manual still restarts the engine last"

echo "update.sh: --contract prints the declared contract and touches nothing"
setup_env
contract_out="$(run_update --contract 2>/dev/null)"
assert_eq "$?" 0 "--contract exits 0"
marker="$(grep -m1 '^# mojo-deploy-contract:' "$PROJ/aws/update.sh" | awk '{print $3}')"
assert_eq "$contract_out" "$marker" \
    "--contract prints the same integer the marker declares (got '$contract_out', marker '$marker')"
assert_eq "$(wc -l < "$CALLLOG" | tr -d ' ')" 0 "no command ran for --contract"
[ -f "$PROJ/var/update.lock" ] && fail "--contract took the update lock" || ok "--contract never touched the lock"
# The whole point of answering before the cd: a checker must be able to ask a
# box what its script speaks even when the deployed tree is not there.
( cd "$TMP" && PROJ_PATH="$TMP/does-not-exist" PATH="$STUB:$PATH" \
    bash "$PROJ/aws/update.sh" --contract >/dev/null 2>&1 )
assert_eq "$?" 0 "--contract answers with a nonexistent PROJ_PATH"
run_update --contract --sha "$SHA_NEW" >/dev/null 2>&1
assert_eq "$?" 2 "--contract combined with a deploy flag is a usage error"
assert_eq "$(wc -l < "$CALLLOG" | tr -d ' ')" 0 "a refused --contract combination still ran nothing"

echo "update.sh: deploy_status exit 3 (superseded) is tolerated"
setup_env
echo "3" > "$CTL/deploy_status.exit"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 0 "a superseded success report does not fail the run"

# ── the engine restart tail ──────────────────────────────────────────────────
#
# On a real node this script IS root: the deploy plane execs it through
# `sudo -n`. So the branch that matters — drop to the engine's own user before
# touching jobman — is the one the harness could never reach by accident. It is
# reached deliberately here, through the `id` stub.

resolver() { # -> the resolved engine user, or empty
    # The function is extracted rather than sourced: update.sh is a script that
    # DOES things, and giving it a "source me and stop" mode would put an
    # early exit on the live deploy path to serve a test.
    awk '/^valid_engine_user\(\) \{/,/^\}/'  "$PROJ/aws/update.sh" >  "$TMP/resolver.sh"
    awk '/^resolve_engine_user\(\) \{/,/^\}/' "$PROJ/aws/update.sh" >> "$TMP/resolver.sh"
    ( cd "$PROJ" && PATH="$STUB:$PATH" CRON_ETC="$CTL/cron.d" \
        SUDO_USER="$(cat "$CTL/sudo_user.txt" 2>/dev/null)" \
        bash -c ". '$TMP/resolver.sh'; resolve_engine_user" 2>/dev/null )
}

echo "update.sh: resolve_engine_user — SUDO_USER, then cron, then var/pids"
setup_env
grep -q "^resolve_engine_user() {" "$PROJ/aws/update.sh" \
    && ok "the resolver is a top-level function the harness can extract" \
    || fail "resolve_engine_user is not extractable — the unit cases below are vacuous"

assert_eq "$(resolver)" "$APP_USER" "SUDO_USER wins when it names a real account"

echo "$APP_USER-cron" > "$CTL/sudo_user.txt"
printf '%s\n%s\n' "$APP_USER" "$APP_USER-cron" > "$CTL/known_users.txt"
assert_eq "$(resolver)" "$APP_USER-cron" "the SUDO_USER answer is used verbatim"

# root: the whole point. Starting the engine as root leaves root-owned logs the
# app user can never append to again.
echo "root" > "$CTL/sudo_user.txt"
assert_eq "$(resolver)" "$APP_USER" "SUDO_USER=root falls through to the cron entry"

echo "" > "$CTL/sudo_user.txt"
assert_eq "$(resolver)" "$APP_USER" "an empty SUDO_USER falls through to the cron entry"

write_jobs_cron "root"
assert_eq "$(resolver)" "$APP_USER" "a cron entry naming root falls through to var/pids"

write_jobs_cron "someone-who-does-not-exist"
assert_eq "$(resolver)" "$APP_USER" "a cron user that has no account is not used"

rm -f "$CTL/cron.d/3_mojo_jobs"
assert_eq "$(resolver)" "$APP_USER" "a missing cron file falls through to var/pids"

echo "UNKNOWN" > "$CTL/pids_owner.txt"
assert_eq "$(resolver)" "" "GNU stat's UNKNOWN resolves to nothing, never to a user"

echo "root" > "$CTL/pids_owner.txt"
assert_eq "$(resolver)" "" "root-owned pids resolve to nothing — cron can have it"

: > "$CTL/pids_owner.txt"
assert_eq "$(resolver)" "" "no answer anywhere resolves to nothing"

echo "$APP_USER" > "$CTL/pids_owner.txt"
touch "$CTL/stat_bsd"
assert_eq "$(resolver)" "$APP_USER" "the BSD stat spelling resolves too"
rm -f "$CTL/stat_bsd"

echo "1000" > "$CTL/pids_owner.txt"
printf '%s\n%s\n' "$APP_USER" "1000" > "$CTL/known_users.txt"
assert_eq "$(resolver)" "" "a bare uid is not a user name we were told to use"

echo 'app;rm -rf /' > "$CTL/pids_owner.txt"
assert_eq "$(resolver)" "" "an answer with shell metacharacters is refused outright"

echo "update.sh: the tail drops root before touching the engine"
setup_env
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 0 "the canary run still exits 0"
assert_in_log "CMD sudo -n -u $APP_USER python3 bin/manage.py deploy_status handoff" \
    "the job handoff runs as the engine user"
assert_in_log "CMD sudo -n -u $APP_USER ./bin/jobman stop engine --grace 2" \
    "the engine is stopped as the engine user, with the short grace"
assert_in_log "CMD sudo -n -u $APP_USER ./bin/jobman start" \
    "the engine is started as the engine user — never as the root we are"
assert_in_log "CMD jobman stop engine --grace 2" \
    "the dropped-privilege command actually reaches jobman"
assert_in_log "CMD jobman stop scheduler$" \
    "the scheduler is stopped WITHOUT --grace: it has no deploy job to release"
assert_order "deploy_status handoff" "jobman stop engine" \
    "the job row is handed off while an engine still exists to do it"
assert_order "jobman stop scheduler" "CMD jobman start" \
    "both components are stopped before either is started"
assert_last_cmd "jobman start" "the restart is the last thing that runs"
assert_in_log "deploy_status handoff --deployment $DEPLOYMENT_UUID" \
    "the handoff names this deployment"

echo "update.sh: already the engine user — no sudo, same tail"
setup_env
echo "$APP_USER" > "$CTL/whoami.txt"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 0 "a run by the engine user itself exits 0"
assert_not_in_log "CMD sudo -n -u" \
    "no sudo when we are already the user we would sudo to"
assert_in_log "CMD jobman stop engine --grace 2" "the engine is still stopped"
assert_last_cmd "jobman start" "the engine is still started, last"

echo "update.sh: an unresolvable engine user leaves the restart to cron"
setup_env
echo "" > "$CTL/sudo_user.txt"
rm -f "$CTL/cron.d/3_mojo_jobs"
echo "root" > "$CTL/pids_owner.txt"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 0 "an unresolved engine user is not a deploy failure"
assert_not_in_log "jobman" \
    "jobman is not touched at all — a root-started engine bricks the node, \
and cron restarts it within the minute anyway"
assert_not_in_log "deploy_status handoff" \
    "no handoff either: the engine is not being killed"
if grep -q "engine user unresolved" "$PROJ/var/update.log" 2>/dev/null; then
    ok "the skip is recorded in var/update.log"
else
    fail "the skip left no trace in var/update.log"
fi

echo "update.sh: a jobman without --grace still gets stopped"
setup_env
touch "$CTL/jobman.no_grace"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 0 "the run still exits 0 when jobman rejects --grace"
assert_in_log "CMD jobman stop engine --grace 2" "the graced stop is attempted first"
assert_in_log "CMD jobman stop engine$" \
    "an argparse refusal (exit 2) falls back to the plain stop — a rollback \
can restore a jobman that predates the flag"
assert_last_cmd "jobman start" "the fallback still reaches the start"

echo "update.sh: a failing sudo never fails the deploy"
setup_env
touch "$CTL/sudo_u.fail"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 0 "a node that proved its release does not fail over the restart"
assert_in_log "CMD sudo -n -u $APP_USER ./bin/jobman start" \
    "every step is still attempted"

echo "update.sh: --manual restarts the engine but hands off no deployment"
setup_env
run_update --manual >/dev/null 2>&1
assert_eq "$?" 0 "--manual exits 0"
assert_not_in_log "deploy_status handoff" \
    "--manual has no deployment UUID and must not invent one"
assert_in_log "CMD jobman stop engine --grace 2" "--manual still stops the engine"
assert_last_cmd "jobman start" "--manual still starts it again"

echo "update.sh: a canary rollback restarts the engine too"
setup_env
touch "$CTL/sudo.fail_first"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 1 "a failed canary still exits 1"
assert_order "deploy_status set failed" "CMD jobman stop engine" \
    "the failure is reported before the engine that would report it dies"
assert_last_cmd "jobman start" \
    "the replacement engine is what finalizes the terminal UUID — it has to \
exist"

# ── phase timings ────────────────────────────────────────────────────────────

phase_field() { # field-number -> that column of every recorded line
    awk -v n="$1" '{print $n}' "$PROJ/var/deploy/phase_timings" 2>/dev/null
}

echo "update.sh: every deploy records where its seconds went"
setup_env
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 0 "a timed canary run still exits 0"
if [ -s "$PROJ/var/deploy/phase_timings" ]; then
    ok "the run recorded phase timings"
else
    fail "no phase timings were recorded"
fi
assert_eq "$(phase_field 2 | tr '\n' ' ')" "git_sync post_deploy sanity_check identity total " \
    "the phases are recorded in the order they happened"
assert_eq "$(phase_field 1 | sort -u | tr '\n' ' ')" "deploy " \
    "a clean deploy records exactly one pass"
bad_values="$(phase_field 3 | grep -cv '^[0-9][0-9]*$')"
assert_eq "$bad_values" "0" \
    "every recorded value is all digits — a date without %3N echoes the \
format literally, and that must never reach the platform"
bad_units="$(phase_field 4 | grep -cv '^\(ms\|s\)$')"
assert_eq "$bad_units" "0" "every recorded unit is one this parser knows"
bad_names="$(phase_field 2 | grep -cv '^[a-z_]\{1,32\}$')"
assert_eq "$bad_names" "0" "every recorded name matches the platform's charset"
assert_eq "$(cat "$PROJ/var/deploy/phase_pass")" "deploy" \
    "post_deploy.sh reads the pass from a file, never from an argv it may be \
too old to understand"
assert_in_log "deploy_status set deploying .*--phases var/deploy/phase_timings" \
    "the timings travel with the terminal callback"
assert_in_log "deploy_status set --help" \
    "the flag is probed before it is used"

echo "update.sh: a deploy_status without --phases is never passed one"
setup_env
touch "$CTL/deploy_status.no_phases"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 0 "an older deploy_status does not fail the deploy"
assert_in_log "deploy_status set deploying" "the callback still happens"
assert_not_in_log "phases" \
    "a rollback can restore a deploy_status that argparse-exits 2 on --phases, \
which report_status would report as a node failure on a healthy deploy"

echo "update.sh: a rollback's phases are marked as the rollback's"
setup_env
touch "$CTL/sudo.fail_first"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_eq "$?" 1 "the failed canary still exits 1"
assert_eq "$(phase_field 1 | sort -u | tr '\n' ' ')" "deploy rollback " \
    "the timings distinguish the deploy pass from the rollback pass"
assert_eq "$(cat "$PROJ/var/deploy/phase_pass")" "rollback" \
    "post_deploy.sh re-entered by the rollback is told which pass it is in"
assert_in_log "deploy_status set failed .*--phases var/deploy/phase_timings" \
    "a failure report carries the timings too — that is when they matter most"

echo "update.sh: timings are this run's, not the last one's"
setup_env
mkdir -p "$PROJ/var/deploy"
echo "deploy stale_entry 999 ms" > "$PROJ/var/deploy/phase_timings"
run_update --sha "$SHA_NEW" --framework "1.6.0" --migrate >/dev/null 2>&1
assert_not_in_log "stale_entry" "no leftover is reported"
if grep -q "stale_entry" "$PROJ/var/deploy/phase_timings"; then
    fail "a previous run's timings survived into this one"
else
    ok "the timings file is truncated at the start of every run"
fi

# ── result ───────────────────────────────────────────────────────────────────

echo
echo "test_update_sh: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
