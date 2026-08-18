#!/bin/bash
# Harness for mojo/deploy/scripts/update.sh (maestro item 1612; harness ported
# from mverify_api #1582 / django-mojo-skeleton #1572).
#
# Runs the REAL packaged script in a throwaway PROJ_PATH with every external
# command stubbed on PATH — each stub appends its argv to a call log, and
# per-command control files script exit codes. The properties under test are
# mostly ORDERINGS (release failure reports BEFORE rollback, jobman stop LAST) and
# ABSENCES (fleet runs never touch deploy_status, a short-circuit fetches
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
    if grep -q "$1" "$CALLLOG" 2>/dev/null; then ok "$2"; else fail "$2 (no '$1' in log)"; fi
}
assert_not_in_log() { # pattern label
    if grep -q "$1" "$CALLLOG" 2>/dev/null; then fail "$2 ('$1' present)"; else ok "$2"; fi
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
exit 0
EOF

    chmod +x "$STUB/git" "$STUB/python3" "$STUB/sudo" "$STUB/mv" "$PROJ/bin/jobman"

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
}

run_update() { # args...
    local args=("$@") joined=" $* "
    if [[ "$joined" == *" --sha "* ]] && [[ "$joined" != *" --deployment "* ]]; then
        args+=(--deployment "$DEPLOYMENT_UUID")
    fi
    ( cd "$TMP" && PROJ_PATH="$PROJ" PATH="$STUB:$PATH" bash "$PROJ/aws/update.sh" "${args[@]}" )
}

run_update_with_url() { # url args...
    local url="$1"; shift
    local args=("$@") joined=" $* "
    if [[ "$joined" == *" --sha "* ]] && [[ "$joined" != *" --deployment "* ]]; then
        args+=(--deployment "$DEPLOYMENT_UUID")
    fi
    ( cd "$TMP" && SANITY_URL="$url" PROJ_PATH="$PROJ" PATH="$STUB:$PATH" bash "$PROJ/aws/update.sh" "${args[@]}" )
}

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
assert_not_in_log "CMD sudo" "no post_deploy on a short-circuit"
assert_in_log "deploy_status set deploying" \
    "same-SHA with a fresh UUID still reports terminal intent"
assert_in_log "ENV identity_ready=2" \
    "same-SHA callback carries the fixed v2 identity-ready signal"
assert_eq "$(manifest_value deployment "$PROJ/var/deploy_identity.json")" \
    "$DEPLOYMENT_UUID" "same-SHA publishes the fresh deployment UUID"
assert_eq "$(grep "^CMD" "$CALLLOG" | tail -1 | grep -c "jobman stop")" 1 \
    "same-SHA still reaches the normal jobman-last tail"

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
assert_eq "$(grep "^CMD" "$CALLLOG" | tail -1 | grep -c "jobman stop")" 1 \
    "jobman stop is the LAST command"

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
assert_not_in_log "deploy_status" "fleet runs never touch deploy_status"
setup_env
echo "1" > "$CTL/sudo.exit"
run_update --sha "$SHA_NEW" --framework "1.6.0" >/dev/null 2>&1
assert_eq "$?" 1 "failed fleet run exits 1"
assert_not_in_log "jobman stop" "failed fleet run never reaches jobman stop"
assert_not_in_log "deploy_status" "failed fleet run still writes no status"

echo "update.sh: --manual is the legacy path, no status writes"
setup_env
run_update --manual >/dev/null 2>&1
assert_eq "$?" 0 "--manual exits 0"
assert_in_log "CMD git reset --hard origin/main" "--manual deploys origin/main"
assert_in_log "CMD sudo bash ./aws/post_deploy.sh$" "--manual runs post_deploy bare"
assert_not_in_log "deploy_status" "--manual writes no status"
assert_eq "$(grep "^CMD" "$CALLLOG" | tail -1 | grep -c "jobman stop")" 1 \
    "--manual still stops jobman last"

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

# ── result ───────────────────────────────────────────────────────────────────

echo
echo "test_update_sh: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
