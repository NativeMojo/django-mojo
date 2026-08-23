#!/bin/bash
# Harness for mojo.deploy.certbot_sync (maestro item 1612; Groups A+B ported
# from django-mojo-skeleton's test_certbot_plane.sh, #1599).
#
# Covers the defects that only surface on a real two-node fleet: the EXDEV
# staging bug that made every replica pull fail forever, and ungated renewal
# on replicas.
#
# The module runs for real here as `python3 -m mojo.deploy.certbot_sync` —
# boto3 is only imported past the config gate, so the gating paths need no
# AWS and no network. Every run sets CERTBOT_SYNC_LOCK into the temp dir:
# acquire_lock() opens the path for writing, and the production default
# (/var/run/...) needs root. PYTHONPATH also points at a poisoned
# boto3/botocore, so any path that imports them when it shouldn't fails
# loudly instead of quietly passing.
set -u

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

STUB="$TMP/stubs"
POISON="$TMP/poison"
OUT="$TMP/out.log"
export CALLLOG="$TMP/calls.log"

# The interpreter that can import the repo's mojo (needs its deps): the repo
# venv when present (manual runs from a bare shell), else PATH's python3
# (the testit wrapper already runs inside the venv).
PY3="$REPO/.venv/bin/python3"
[ -x "$PY3" ] || PY3="$(command -v python3)"

PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

assert_eq() { # actual expected label
    if [ "$1" = "$2" ]; then ok "$3"; else fail "$3 (got: $1, want: $2)"; fi
}
assert_has() { # file pattern label
    if grep -q -- "$2" "$1" 2>/dev/null; then ok "$3"; else fail "$3 (no '$2' in $(basename "$1"))"; fi
}
assert_lacks() { # file pattern label
    if grep -q -- "$2" "$1" 2>/dev/null; then fail "$3 ('$2' present in $(basename "$1"))"; else ok "$3"; fi
}
assert_in_log()     { assert_has "$CALLLOG" "$1" "$2"; }
assert_not_in_log() { assert_lacks "$CALLLOG" "$1" "$2"; }

setup_env() {
    rm -rf "$STUB" "$POISON"
    mkdir -p "$STUB" "$POISON"
    : > "$CALLLOG"

    cat > "$STUB/certbot" <<'EOF'
#!/bin/bash
echo "CMD certbot $*" >> "$CALLLOG"
exit 0
EOF
    chmod +x "$STUB/certbot"

    # Importing either of these is itself the failure — the gating paths under
    # test must return before any S3 work.
    echo 'raise ImportError("boto3 must not be imported on this path")'   > "$POISON/boto3.py"
    echo 'raise ImportError("botocore must not be imported on this path")' > "$POISON/botocore.py"
}

run_sync() { # conf args...
    local conf="$1"; shift
    ( cd "$TMP" && env -u DJANGO_SETTINGS_MODULE \
        CERTBOT_SYNC_LOCK="$TMP/certbot_sync.lock" \
        PATH="$STUB:$PATH" PYTHONPATH="$POISON:$REPO" \
        "$PY3" -m mojo.deploy.certbot_sync --config "$conf" --verbose "$@" ) > "$OUT" 2>&1
}

SYNC_PY="$REPO/mojo/deploy/certbot_sync.py"

# ── Group A: staging invariant (the EXDEV defect) ────────────────────────────

echo "certbot_sync: staging shares a filesystem with the destination"
( cd "$TMP" && env -u DJANGO_SETTINGS_MODULE PYTHONPATH="$REPO" "$PY3" - <<'PY' > "$TMP/group_a.out" 2>&1
import mojo.deploy.certbot_sync as mod
print("LETSENCRYPT_DIR=%s" % mod.LETSENCRYPT_DIR)
print("LINEAGE=%s" % mod.lineage_dir("example.com"))
print("ANCESTOR=%s" % mod.lineage_dir("example.com").startswith(mod.LETSENCRYPT_DIR + "/"))
PY
)
assert_has "$TMP/group_a.out" "^LETSENCRYPT_DIR=/etc/letsencrypt$" "LETSENCRYPT_DIR is /etc/letsencrypt"
assert_has "$TMP/group_a.out" "^LINEAGE=/etc/letsencrypt/live/example.com$" "lineage_dir is derived from it"
assert_has "$TMP/group_a.out" "^ANCESTOR=True$" "staging root is an ancestor of the destination (no cross-device rename)"
assert_lacks "$SYNC_PY" 'dir="/tmp"' "nothing stages in /tmp"

# ── Group B: --renew gating (the co-install and replica defects) ─────────────

echo "certbot_sync --renew: an unconfigured box still renews itself"
setup_env
run_sync "$TMP/unconfigured.conf" --renew --dry-run; rc=$?
assert_eq "$rc" 0 "unconfigured --renew exits 0"
assert_has "$OUT" "would run: $STUB/certbot renew" "heartbeat logged, and find_certbot() honors PATH"
assert_lacks "$OUT" "replica node" "an unconfigured box is never treated as a replica"

setup_env
run_sync "$TMP/unconfigured.conf" --renew; rc=$?
assert_eq "$rc" 0 "unconfigured --renew (for real) exits 0"
assert_in_log "CMD certbot renew --post-hook systemctl reload nginx --quiet" "certbot invoked with the reload post-hook"
assert_has "$OUT" "renew ok (single-node box" "success is logged, not silent"

echo "certbot_sync --renew: a configured replica never runs certbot"
setup_env
printf 'AWS_CERT_BUCKET = "acme-certs"\nLOAD_BALANCER_DOMAIN = "acme.com"\nPRIMARY_BALANCER_HOST = "not-this-host"\n' \
    > "$TMP/replica.conf"
run_sync "$TMP/replica.conf" --renew; rc=$?
assert_eq "$rc" 0 "replica --renew exits 0"
assert_has "$OUT" "replica node — renewal is the primary's job, skipping" "the skip is logged"
assert_not_in_log "certbot" "certbot NEVER runs on a replica (a synced lineage would be corrupted)"

echo "certbot_sync --renew: configured with no PRIMARY_BALANCER_HOST refuses"
setup_env
printf 'AWS_CERT_BUCKET = "acme-certs"\nLOAD_BALANCER_DOMAIN = "acme.com"\n' > "$TMP/noprimary.conf"
run_sync "$TMP/noprimary.conf" --renew; rc=$?
assert_eq "$rc" 1 "refusing to guess which node renews exits 1"
assert_has "$OUT" "refusing to guess which node renews" "the refusal is logged"
assert_not_in_log "certbot" "nothing renews while the role is ambiguous"

echo "certbot_sync: the sync path is still dormant when unconfigured"
setup_env
run_sync "$TMP/unconfigured.conf"; rc=$?
assert_eq "$rc" 0 "a bare run on an unconfigured box exits 0"
assert_not_in_log "certbot" "and runs nothing"

# ── Group C: syntax gates for the packaged python ────────────────────────────

echo "syntax"
( cd "$REPO" && "$PY3" -m py_compile mojo/deploy/certbot_sync.py \
    mojo/deploy/check_node.py mojo/deploy/__main__.py ) 2>"$TMP/syntax.err"
assert_eq "$?" 0 "certbot_sync, check_node and __main__ compile"

# ── result ───────────────────────────────────────────────────────────────────

echo
echo "test_certbot_sync: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
