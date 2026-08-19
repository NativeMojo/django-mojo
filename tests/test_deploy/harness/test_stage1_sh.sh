#!/bin/bash
# Harness for the packaged mojo/deploy/provision/scripts/stage1.sh (maestro item 2170).
#
# WHAT THIS IS ACTUALLY GUARDING: the ORDER of stage 1's steps. Every one of
# them "works" in isolation and three of them are silently wrong in the wrong
# order —
#
#   * the version pin must run AFTER ec2_bootstrap.sh, or bootstrap's unpinned
#     `pip install django-mojo` overwrites it and the node runs a different
#     framework release than the CLI that provisioned it;
#   * var/profile must be written AFTER ec2_deploy.sh's ownership sweep and
#     BEFORE config_sync, whose CONFIG_SYNC_RESTART restart would otherwise
#     bring the app up on settings.local — where it serves happily and ignores
#     every endpoint that was just provisioned;
#   * the CloudWatch agent must be configured before that same restart, or the
#     first minutes of the app's life go unlogged.
#
# None of that is visible in a diff, so it is asserted here.
#
# The REAL packaged script runs, substituted through the CLI's own
# `storage.stage1_script()` — so the placeholder contract is exercised rather
# than reimplemented with sed. Every external command is stubbed onto PATH and
# logs to $CALLLOG; the tarball is a real gzip tarball unpacked by real tar,
# so "untar before bootstrap" is proven by ec2_bootstrap.sh existing to be run.
set -u

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

STUB="$TMP/stubs"
PAYLOAD="$TMP/payload"
PROJ="$TMP/proj"
CWETC="$TMP/cwetc"
OUT="$TMP/stage1.log"
export CALLLOG="$TMP/calls.log"

VERSION="9.9.9-test"

PY3="$REPO/.venv/bin/python3"
[ -x "$PY3" ] || PY3="$(command -v python3)"

PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

assert_eq() { # actual expected label
    if [ "$1" = "$2" ]; then ok "$3"; else fail "$3 (got: '$1', want: '$2')"; fi
}
assert_has() { # file pattern label
    if grep -qF -- "$2" "$1" 2>/dev/null; then ok "$3"; else fail "$3 (no '$2' in $(basename "$1"))"; fi
}
assert_lacks() { # file pattern label
    if grep -qF -- "$2" "$1" 2>/dev/null; then fail "$3 ('$2' present in $(basename "$1"))"; else ok "$3"; fi
}
line_of() { grep -nF -- "$1" "$CALLLOG" 2>/dev/null | head -1 | cut -d: -f1; }
assert_before() { # earlier later label
    local a b
    a="$(line_of "$1")"; b="$(line_of "$2")"
    if [ -z "$a" ]; then fail "$3 ('$1' never ran)"; return; fi
    if [ -z "$b" ]; then fail "$3 ('$2' never ran)"; return; fi
    if [ "$a" -lt "$b" ]; then ok "$3"; else fail "$3 ('$1' at line $a, '$2' at line $b)"; fi
}

# ── the substituted script, and the names it must carry ──────────────────────

runpy() { env -u DJANGO_SETTINGS_MODULE PYTHONPATH="$REPO" "$PY3" "$@"; }

runpy - "$VERSION" > "$TMP/stage1.sh" <<'PY'
import sys
from mojo.deploy.provision import storage
sys.stdout.write(storage.stage1_script(sys.argv[1]))
PY
STAGE1="$TMP/stage1.sh"

if [ ! -s "$STAGE1" ]; then
    echo "FATAL: storage.stage1_script() produced nothing"
    exit 1
fi

# The agent config the CLI would publish, plus the three log-group names it
# must carry — both derived from spec.names(), the same source
# observability.py creates the groups from and identity.py scopes the grant to.
mkdir -p "$PAYLOAD"
runpy - "$PAYLOAD" > "$TMP/names.env" <<'PY'
import os
import sys
from mojo.deploy.provision import spec as spec_module
from mojo.deploy.provision import storage

target = sys.argv[1]
topology = spec_module.build("demo", "prod", "us-west-2", preset="micro")
with open(os.path.join(target, "cloudwatch-agent.json"), "w") as handle:
    handle.write(storage.cloudwatch_agent_config(topology))

groups = spec_module.names(topology)["log_groups"]
print("NGINX_GROUP=%s" % groups["nginx"])
print("APP_GROUP=%s" % groups["app"])
print("INIT_GROUP=%s" % groups["cloud-init"])
PY
# shellcheck disable=SC1090
. "$TMP/names.env"

# ── the fake project tree, as a real tarball ─────────────────────────────────

setup_tree() {
    rm -rf "$TMP/fixture" "$PROJ" "$CWETC"
    mkdir -p "$TMP/fixture/aws" "$PROJ/var" "$CWETC"

    cat > "$TMP/fixture/aws/ec2_bootstrap.sh" <<'EOF'
#!/bin/bash
echo "CMD ec2_bootstrap.sh" >> "$CALLLOG"
EOF
    cat > "$TMP/fixture/aws/ec2_deploy.sh" <<'EOF'
#!/bin/bash
echo "CMD ec2_deploy.sh" >> "$CALLLOG"
EOF
    chmod +x "$TMP/fixture/aws/"*.sh
    tar -czf "$PAYLOAD/app.tar.gz" -C "$TMP/fixture" .

    cat > "$PROJ/var/bootstrap.conf" <<'EOF'
AWS_REGION=us-west-2
AWS_CONFIG_BUCKET=demo-prod-config
AWS_CONFIG_PREFIX=config/demo/prod
CONFIG_SYNC_OWNER=ec2-user:www
CONFIG_SYNC_RESTART=true
EOF
}

setup_stubs() {
    rm -rf "$STUB"
    mkdir -p "$STUB"
    : > "$CALLLOG"

    # `aws s3 cp [--region R] <uri> <dest>` — serves whatever the CLI would
    # have published, by object name.
    cat > "$STUB/aws" <<EOF
#!/bin/bash
echo "CMD aws \$*" >> "\$CALLLOG"
dst="\${@: -1}"
src="\${@: -2:1}"
cp "$PAYLOAD/\$(basename "\$src")" "\$dst"
EOF

    cat > "$STUB/pip" <<'EOF'
#!/bin/bash
echo "CMD pip $*" >> "$CALLLOG"
EOF

    # A successful install is what makes amazon-cloudwatch-agent-ctl appear —
    # which is exactly what the second run must then find and skip over.
    cat > "$STUB/dnf" <<EOF
#!/bin/bash
echo "CMD dnf \$*" >> "\$CALLLOG"
cat > "$STUB/amazon-cloudwatch-agent-ctl" <<'CTL'
#!/bin/bash
echo "CMD amazon-cloudwatch-agent-ctl \$*" >> "\$CALLLOG"
CTL
chmod +x "$STUB/amazon-cloudwatch-agent-ctl"
EOF

    cat > "$STUB/systemctl" <<'EOF'
#!/bin/bash
echo "CMD systemctl $*" >> "$CALLLOG"
EOF

    # config_sync must not actually run: it would want boto3, a bucket and a
    # network. Its INVOCATION, and where it falls in the order, is the subject.
    cat > "$STUB/python3" <<'EOF'
#!/bin/bash
echo "CMD python3 $*" >> "$CALLLOG"
EOF

    # chown to ec2-user:www cannot work in a temp dir as a normal user, and the
    # call itself is what the contract is about.
    cat > "$STUB/chown" <<'EOF'
#!/bin/bash
echo "CMD chown $*" >> "$CALLLOG"
EOF

    chmod +x "$STUB"/*
}

run_stage1() {
    ( env -u DJANGO_SETTINGS_MODULE \
        PATH="$STUB:$PATH" \
        PROJ_PATH="$PROJ" \
        BOOTSTRAP_CONF="$PROJ/var/bootstrap.conf" \
        STAGE1_LOG="$OUT" \
        CW_AGENT_ETC="$CWETC" \
        CALLLOG="$CALLLOG" \
        bash "$STAGE1" )
    return $?
}

# ── Group A: the script itself ───────────────────────────────────────────────

echo "stage1.sh: shape"
assert_has "$STAGE1" "set -euo pipefail" "runs under set -euo pipefail"
assert_lacks "$STAGE1" "@DJANGO_MOJO_VERSION@" "the version placeholder is substituted"
assert_has "$STAGE1" "DJANGO_MOJO_VERSION=\"${VERSION}\"" "the pin names the CLI's own version"

echo "stage1.sh: an unsubstituted copy refuses to run"
setup_tree
setup_stubs
( env -u DJANGO_SETTINGS_MODULE PATH="$STUB:$PATH" PROJ_PATH="$PROJ" \
    BOOTSTRAP_CONF="$PROJ/var/bootstrap.conf" STAGE1_LOG="$TMP/raw.log" \
    CW_AGENT_ETC="$CWETC" CALLLOG="$CALLLOG" \
    bash "$REPO/mojo/deploy/provision/scripts/stage1.sh" ); rc=$?
if [ "$rc" -ne 0 ]; then ok "the packaged (unsubstituted) script exits non-zero"; else fail "the packaged script ran with an unsubstituted version pin"; fi
assert_has "$TMP/raw.log" "never substituted" "and says why"
assert_lacks "$CALLLOG" "CMD pip" "nothing was installed on that path"

# ── Group B: the first run ───────────────────────────────────────────────────

echo "stage1.sh: first run"
setup_tree
setup_stubs
run_stage1; rc=$?
assert_eq "$rc" 0 "a clean first run exits 0"

assert_has "$CALLLOG" "app.tar.gz" "the application tarball is fetched"
if [ -f "$PROJ/aws/ec2_bootstrap.sh" ]; then ok "the tree is unpacked into PROJ_PATH"; else fail "the tree is unpacked into PROJ_PATH"; fi
assert_before "app.tar.gz" "CMD ec2_bootstrap.sh" "the tarball is unpacked BEFORE ec2_bootstrap.sh runs (it lives inside it)"
assert_before "CMD ec2_bootstrap.sh" "CMD pip install --upgrade django-mojo==${VERSION}" \
    "the version pin runs AFTER ec2_bootstrap.sh, so it overwrites the unpinned install"
assert_before "CMD pip install --upgrade django-mojo==${VERSION}" "CMD ec2_deploy.sh" \
    "the pin is in place before the project deploy runs"

echo "stage1.sh: var/profile"
assert_eq "$(cat "$PROJ/var/profile" 2>/dev/null)" "prod" "var/profile contains exactly 'prod'"
assert_eq "$(stat -f '%Lp' "$PROJ/var/profile" 2>/dev/null || stat -c '%a' "$PROJ/var/profile" 2>/dev/null)" \
    "640" "var/profile is mode 640"
assert_has "$CALLLOG" "CMD chown ec2-user:www $PROJ/var/profile" "var/profile is chowned ec2-user:www"
assert_before "CMD ec2_deploy.sh" "CMD chown ec2-user:www $PROJ/var/profile" \
    "the profile is written AFTER ec2_deploy.sh (whose var sweep would otherwise be the last word)"
assert_before "CMD chown ec2-user:www $PROJ/var/profile" "CMD python3 -m mojo.deploy.config_sync" \
    "the profile exists BEFORE config_sync, whose restart would otherwise boot settings.local"

echo "stage1.sh: the CloudWatch agent"
assert_has "$CALLLOG" "CMD dnf install -y amazon-cloudwatch-agent" "the agent is installed when absent"
assert_has "$CWETC/amazon-cloudwatch-agent.json" "$NGINX_GROUP" "the agent config names the nginx log group"
assert_has "$CWETC/amazon-cloudwatch-agent.json" "$APP_GROUP" "the agent config names the app log group"
assert_has "$CWETC/amazon-cloudwatch-agent.json" "$INIT_GROUP" "the agent config names the cloud-init log group"
assert_has "$CWETC/amazon-cloudwatch-agent.json" "/opt/api/var/logs/" "the agent collects the app's logit output directory"
assert_has "$CWETC/amazon-cloudwatch-agent.json" "/var/log/cloud-init-output.log" "the agent collects cloud-init's output"
assert_lacks "$CWETC/amazon-cloudwatch-agent.json" "@LOG_GROUP" "no placeholder survives into the node's copy"
assert_before "CMD systemctl enable --now amazon-cloudwatch-agent" "CMD python3 -m mojo.deploy.config_sync" \
    "the agent is enabled BEFORE the config_sync restart, so the app's first minutes are logged"

echo "stage1.sh: config_sync is last"
assert_has "$CALLLOG" "CMD python3 -m mojo.deploy.config_sync --config $PROJ/var/bootstrap.conf" \
    "config_sync runs against the bootstrap config stage 0 wrote"
if tail -n 1 "$CALLLOG" | grep -qF "CMD python3 -m mojo.deploy.config_sync"; then
    ok "config_sync is the LAST command stage 1 runs"
else
    fail "config_sync is the LAST command stage 1 runs (last was: $(tail -n 1 "$CALLLOG"))"
fi

# ── Group C: the re-run ──────────────────────────────────────────────────────

echo "stage1.sh: re-run is idempotent"
: > "$CALLLOG"
run_stage1; rc=$?
assert_eq "$rc" 0 "a second run over a converged node exits 0"
assert_lacks "$CALLLOG" "CMD dnf install" "an already-installed CloudWatch agent is not re-installed"
assert_has "$CALLLOG" "CMD systemctl enable --now amazon-cloudwatch-agent" "but it is still converged and enabled"
assert_eq "$(cat "$PROJ/var/profile" 2>/dev/null)" "prod" "var/profile still reads prod"
assert_has "$CALLLOG" "CMD python3 -m mojo.deploy.config_sync" "config_sync runs again"

echo "stage1.sh: a missing bootstrap.conf is fatal, not silent"
setup_stubs
rm -f "$PROJ/var/bootstrap.conf"
run_stage1; rc=$?
if [ "$rc" -ne 0 ]; then ok "no bootstrap.conf exits non-zero"; else fail "no bootstrap.conf was tolerated"; fi
assert_lacks "$CALLLOG" "CMD pip" "and nothing was installed"

# ── result ───────────────────────────────────────────────────────────────────

echo
echo "test_stage1_sh: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
