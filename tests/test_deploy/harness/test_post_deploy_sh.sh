#!/bin/bash
# Harness for mojo/deploy/scripts/post_deploy.sh (maestro item 1612; harness
# ported from mverify_api #1582 / django-mojo-skeleton #1572, re-fixtured for
# the packaged render/var-deploy pipeline).
#
# Runs the REAL packaged script against a throwaway PROJ_PATH with the
# NGINX_ETC / SYSTEMD_ETC / CRON_ETC seams pointed into the temp dir and every
# external command stubbed on PATH — EXCEPT `python3 -m mojo.deploy*` and the
# embedded TLS-vhost validator, which the python3 stub passes through with
# PYTHONPATH=$REPO so the render step runs for real (a control file forces it
# to fail instead). Packaged deltas under test: templates render into
# var/deploy/ and install from there fully substituted, the
# node_overrides.conf collision policy, node_retired.conf retirement, the
# PROBE_URL / APP_USER / WEB_USER / ASGI_WORKERS inputs, and the post-success
# self-snapshot. The mverify-era orderings and absences (deps before
# framework, migrate before restart, bare never migrates, die-loudly) are
# preserved.
set -u

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PROJ="$TMP/proj"
STUB="$TMP/stubs"
CTL="$TMP/ctl"
OUT="$TMP/run.out"
export CALLLOG="$TMP/calls.log"
export STUBCTL="$CTL"

# The interpreter the render passthrough uses must be able to import the
# repo's mojo (needs its deps): the repo venv when present (manual runs from
# a bare shell), else PATH's python3 (the testit wrapper already runs inside
# the venv).
REAL_PYTHON3="$REPO/.venv/bin/python3"
[ -x "$REAL_PYTHON3" ] || REAL_PYTHON3="$(command -v python3)"

PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

assert_eq() {
    if [ "$1" = "$2" ]; then ok "$3"; else fail "$3 (got: $1, want: $2)"; fi
}
assert_in_log() {
    if grep -q "$1" "$CALLLOG" 2>/dev/null; then ok "$2"; else fail "$2 (no '$1' in log)"; fi
}
assert_not_in_log() {
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
assert_file()    { if [ -f "$1" ]; then ok "$2"; else fail "$2 ($1 missing)"; fi }
assert_no_file() { if [ -f "$1" ]; then fail "$2 ($1 present)"; else ok "$2"; fi }
assert_has() { # file pattern label
    if grep -q -- "$2" "$1" 2>/dev/null; then ok "$3"; else fail "$3 (no '$2' in $(basename "$1"))"; fi
}
assert_lacks() { # file pattern label
    if grep -q -- "$2" "$1" 2>/dev/null; then fail "$3 ('$2' present in $(basename "$1"))"; else ok "$3"; fi
}

# The stand-in for the root-owned stable helper at
# /usr/local/lib/mojosec/mojosec_changes.py. It drives the REAL journal against
# throwaway state, so the packaged script's own declaration is validated by the
# real validator — which is the only way a self-conflicting declaration (item
# 2014) is reachable from a test. It never needs $REPO or $TMP baked in: the
# python3 stub execs it with PYTHONPATH=$REPO, and $TMP is its own directory.
write_trusted_shim() {
    cat > "$TMP/trusted_shim.py" <<'PYEOF'
import os
import subprocess
import sys

TMP = os.path.dirname(os.path.abspath(__file__))

argv = sys.argv[1:]
start = next((i for i, token in enumerate(argv) if token in ("run", "pip-run")), None)
if start is None:
    print("trusted shim: no subcommand in argv", file=sys.stderr)
    raise SystemExit(2)
command, rest = argv[start], argv[start + 1:]

flags, paths, child = {}, [], []
index = 0
while index < len(rest):
    token = rest[index]
    if token == "--":
        child = rest[index + 1:]
        break
    if token == "--path":
        paths.append(rest[index + 1])
        index += 2
    elif token.startswith("--"):
        flags[token] = rest[index + 1] if index + 1 < len(rest) else ""
        index += 2
    else:
        index += 1

if not child:
    print("mojosec changes: trusted-change run requires one child argv", file=sys.stderr)
    raise SystemExit(2)
if command == "pip-run":
    os.execvp(child[0], child)

import mojo.deploy.mojosec_changes as m

# Pre-fix the function does not exist, so the declaration reaches validate_paths
# untouched and the run reproduces the production abort.
if hasattr(m, "split_control_state_paths"):
    paths, dropped = m.split_control_state_paths(paths)
    for path in dropped:
        print("mojosec changes: ignoring declared MojoSec control-state path "
              f"(converge owns it): {path}", file=sys.stderr)

operation = flags.get("--operation-id", "")
journal = m.ChangeJournal(
    journal_path=os.path.join(TMP, "journal", "journal.json"),
    lock_path=os.path.join(TMP, "journal", "journal.lock"),
    manifest_path=os.path.join(TMP, "journal", "expected.json"),
    allowed_roots=tuple(m._GENERAL_ROOTS) + tuple(m._HOME_ROOTS) + (TMP,),
)
try:
    journal.begin(operation, flags.get("--kind", "harness"), paths,
                  deployment_id=flags.get("--deployment-id") or None)
except (m.ChangeError, OSError, ValueError) as err:
    print(f"mojosec changes: {err}", file=sys.stderr)
    raise SystemExit(2)
try:
    done = subprocess.run(child)
    if done.returncode != 0:
        journal.abort(operation)
        raise SystemExit(done.returncode)
    journal.complete(operation)
except (m.ChangeError, OSError, ValueError) as err:
    journal.abort(operation)
    print(f"mojosec changes: {err}", file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(0)
PYEOF
}

setup_env() {
    rm -rf "$PROJ" "$STUB" "$CTL" "$TMP/nginx_etc" "$TMP/letsencrypt" \
           "$TMP/systemd_etc" "$TMP/cron_etc" \
           "$TMP/logrotate_etc" "$TMP/mojosec_etc" "$TMP/mojosec_lib" "$TMP/journal"
    mkdir -p "$PROJ/var/logs" "$PROJ/bin" "$PROJ/aws/nginx/systemd" "$PROJ/aws/nginx/sec.d" \
             "$PROJ/aws/nginx/conf.d" "$PROJ/aws/cron.d" \
             "$STUB" "$CTL" "$TMP/nginx_etc" "$TMP/systemd_etc" "$TMP/cron_etc" \
             "$TMP/logrotate_etc" "$TMP/mojosec_etc" "$TMP/mojosec_lib" "$TMP/journal"
    : > "$CALLLOG"
    write_trusted_shim

    # The shim contract executes the located packaged script; the harness
    # plays the shim.
    cp "$REPO/mojo/deploy/scripts/post_deploy.sh" "$PROJ/aws/post_deploy.sh"
    # install_file dies on a missing source — the top-level configs must exist.
    echo "# test" > "$PROJ/aws/nginx/nginx.conf"
    echo "# test" > "$PROJ/aws/nginx/asgi.inc"
    echo "# test" > "$PROJ/aws/nginx/django.inc"
    echo "# test hardening" > "$PROJ/aws/nginx/sec.d/hardening.conf"
    # Neutral vhost fixtures: two real ones plus an .example that must be
    # skipped by the conf.d glob.
    echo "# test probe vhost" > "$PROJ/aws/nginx/conf.d/probe.conf"
    echo "# test app vhost" > "$PROJ/aws/nginx/conf.d/app.conf"
    echo "# operator template, never installed" > "$PROJ/aws/nginx/conf.d/sample.example.conf"
    echo "# req" > "$PROJ/requirements.txt"
    # A project cron EXTRA (non-colliding name, references the project path so
    # the structural sweep recognises it as ours).
    printf 'SHELL=/bin/bash\n* * * * * root %s/bin/extra.sh >> %s/var/logs/extra.log 2>&1\n' \
        "$PROJ" "$PROJ" > "$PROJ/aws/cron.d/9_project_extra"
    # A project systemd EXTRA, service + timer.
    printf '[Unit]\nDescription=extra for %s\n' "$PROJ" > "$PROJ/aws/nginx/systemd/project-extra.service"
    printf '[Unit]\nDescription=extra timer\n[Timer]\nOnBootSec=1min\n' > "$PROJ/aws/nginx/systemd/project-extra.timer"
    : > "$PROJ/bin/manage.py"

    # Stale names pre-seeded on the "node". 1mojocron / 3mojo_jogs reference
    # the project path — the structural sweep discovers them. 2certbot does
    # NOT (the real fleet file never did, which is why a discovery rule alone
    # cannot catch it) — aws/node_retired.conf is what retires it.
    echo "* * * * * www $PROJ/bin/cron.py --run" > "$TMP/cron_etc/1mojocron"
    echo "0 6 * * 0 root certbot renew --quiet" > "$TMP/cron_etc/2certbot"
    echo "* * * * * ec2-user $PROJ/bin/jobman start" > "$TMP/cron_etc/3mojo_jogs"
    # NOT ours: never mentions PROJ_PATH, not declared retired — must survive.
    echo "01 * * * * root run-parts /etc/cron.hourly" > "$TMP/cron_etc/0hourly"
    # A conf.d vhost this project once shipped and has since declared retired.
    mkdir -p "$TMP/nginx_etc/conf.d"
    echo "# persistent framework runtime contract" > \
        "$TMP/nginx_etc/conf.d/00_django_mojo_runtime.conf"
    echo "# superseded vhost" > "$TMP/nginx_etc/conf.d/stale-old.conf"
    printf '# names this project retired\ncron.d/2certbot\nconf.d/stale-old.conf\n' \
        > "$PROJ/aws/node_retired.conf"

    for cmd in pip nginx systemctl curl chown; do
        cat > "$STUB/$cmd" <<EOF
#!/bin/bash
echo "CMD $cmd \$*" >> "\$CALLLOG"
if [ "$cmd" = "pip" ] && [[ "\$*" == *install*django-mojo==* ]]; then
    # pip.framework_fail_times: the next N framework resolutions fail the way
    # a stale Simple index does. Counts down per invocation (the mojosec
    # dry-run and the real install each consume one, like production).
    remaining="\$(cat "\$STUBCTL/pip.framework_fail_times" 2>/dev/null || echo 0)"
    if [ "\$remaining" -gt 0 ] 2>/dev/null; then
        echo "\$((remaining - 1))" > "\$STUBCTL/pip.framework_fail_times"
        echo "ERROR: No matching distribution found for django-mojo" >&2
        exit 1
    fi
fi
if [ "$cmd" = "curl" ]; then
    for arg in "\$@"; do
        case "\$arg" in
            https://pypi.org/pypi/django-mojo/json)
                # Latest-resolution body; default resolves the harness pin.
                cat "\$STUBCTL/pypi.latest.body" 2>/dev/null \
                    || printf '{"info":{"version":"9.9.9"}}'
                exit 0 ;;
            https://pypi.org/pypi/django-mojo/*/json)
                # Existence probe answers with an HTTP code (-w '%{http_code}').
                printf '%s' "\$(cat "\$STUBCTL/pypi.version.code" 2>/dev/null || echo 200)"
                exit 0 ;;
        esac
    done
fi
ctl="\$STUBCTL/$cmd.exit"
[ -f "\$ctl" ] && exit "\$(cat "\$ctl")"
exit 0
EOF
        chmod +x "$STUB/$cmd"
    done
    cat > "$STUB/systemctl" <<'EOF'
#!/bin/bash
echo "CMD systemctl $*" >> "$CALLLOG"
case "$*" in
    "disable --now mojo-asgi.service")
        [ -f "$STUBCTL/asgi.absent" ] && exit 1
        [ -f "$STUBCTL/asgi.revoke_fail" ] && exit 1
        touch "$STUBCTL/asgi.inactive" "$STUBCTL/asgi.disabled"
        exit 0 ;;
    "show --property=LoadState --value mojo-asgi.service")
        [ -f "$STUBCTL/asgi.query_fail" ] && exit 1
        if [ -f "$STUBCTL/asgi.absent" ]; then echo not-found; else echo loaded; fi
        exit 0 ;;
    "show --property=ActiveState --value mojo-asgi.service")
        [ -f "$STUBCTL/asgi.query_fail" ] && exit 1
        if [ -f "$STUBCTL/asgi.inactive" ] || [ -f "$STUBCTL/asgi.absent" ]; then
            echo inactive
        else
            echo active
        fi
        exit 0 ;;
    "show --property=UnitFileState --value mojo-asgi.service")
        [ -f "$STUBCTL/asgi.query_fail" ] && exit 1
        if [ -f "$STUBCTL/asgi.disabled" ]; then echo disabled; else echo enabled; fi
        exit 0 ;;
esac
case "$1 $2" in
    "is-active --quiet") [ ! -f "$STUBCTL/mojosec.inactive" ]; exit $? ;;
    "is-enabled --quiet") [ ! -f "$STUBCTL/mojosec.disabled" ]; exit $? ;;
    "stop mojosec.service") touch "$STUBCTL/mojosec.inactive"; exit 0 ;;
    "start mojosec.service") rm -f "$STUBCTL/mojosec.inactive"; exit 0 ;;
    "enable mojosec.service") rm -f "$STUBCTL/mojosec.disabled"; exit 0 ;;
    "disable mojosec.service") touch "$STUBCTL/mojosec.disabled"; exit 0 ;;
esac
ctl="$STUBCTL/systemctl.exit"
[ -f "$ctl" ] && exit "$(cat "$ctl")"
exit 0
EOF
    chmod +x "$STUB/systemctl"
    cat > "$STUB/stat" <<'EOF'
#!/bin/bash
# Fallback ownership probe: the harness models root-owned /etc seams.
case "$*" in
    *"%a"*) cat "$STUBCTL/stat.mode" 2>/dev/null || echo 600 ;;
    *)      cat "$STUBCTL/stat.owner" 2>/dev/null || echo 0 ;;
esac
EOF
    chmod +x "$STUB/stat"

    # python3: `-m mojo.deploy*` passes through to the REAL interpreter with
    # PYTHONPATH=$REPO (render.exit forces the render step to fail instead);
    # everything else (manage.py) is argv-logged and scripted.
    cat > "$STUB/python3" <<EOF
#!/bin/bash
echo "CMD python3 \$*" >> "\$CALLLOG"
case "\$*" in
    "-m mojo.deploy.request_service")
        value="\$(cat "\$STUBCTL/request_service.value" 2>/dev/null || echo true)"
        if [ "\$value" = error ]; then
            echo "request-service: sealed authority refused" >&2
            exit 2
        fi
        printf '%s\n' "\$value"
        exit 0
        ;;
    *"-c import mojo.deploy.vhost_install"*)
        # The interpreter probe post_deploy.sh runs once before the vhost loop.
        # vhost_install.absent forces "cannot import", which is the only way to
        # reach the install_file fallback branch from a test.
        [ -f "\$STUBCTL/vhost_install.absent" ] && exit 1
        exec env -u DJANGO_SETTINGS_MODULE PYTHONPATH="$REPO" "$REAL_PYTHON3" \
            -c 'import mojo.deploy.vhost_install'
        ;;
    *"-m mojo.deploy.vhost_install"*)
        # Strip the production -E/-P before passing through: -E would discard
        # the PYTHONPATH this passthrough depends on to find the repo's mojo.
        # The harness therefore does NOT exercise the production flag set —
        # a known, deliberate gap.
        vhost_args=()
        for arg in "\$@"; do
            case "\$arg" in -E|-P) continue ;; esac
            vhost_args+=("\$arg")
        done
        exec env -u DJANGO_SETTINGS_MODULE PYTHONPATH="$REPO" "$REAL_PYTHON3" \
            "\${vhost_args[@]}"
        ;;
    *"sys.version_info >= (3,11)"*)
        [ -f "\$STUBCTL/mojosec.version.exit" ] && exit "\$(cat "\$STUBCTL/mojosec.version.exit")"
        exit 0
        ;;
    *"find_spec(\"mojo.deploy.mojosec\")"*)
        echo "MOJOSEC_CWD \$PWD" >> "\$CALLLOG"
        [ -f "\$STUBCTL/mojosec.preflight.exit" ] && exit "\$(cat "\$STUBCTL/mojosec.preflight.exit")"
        exit 0
        ;;
    "-E -P -m mojo.deploy.mojosec converge"*|"-E -m mojo.deploy.mojosec converge"*)
        echo "MOJOSEC_CWD \$PWD" >> "\$CALLLOG"
        if [ "\$(cat "\$STUBCTL/mojosec.preflight.exit" 2>/dev/null || true)" = "4" ] &&
                [[ " \$* " == *" --project-path "* ]]; then
            echo "old argparse rejected --project-path" >> "\$CALLLOG"
            exit 2
        fi
        [ -f "\$STUBCTL/mojosec.converge.exit" ] && exit "\$(cat "\$STUBCTL/mojosec.converge.exit")"
        exit 0
        ;;
    *"mojosec_audit.py flush-pending"*)
        [ -f "\$MOJOSEC_AUDIT_HELPER" ] && echo "ASSET_PRESENT flush" >> "\$CALLLOG"
        [ -f "\$STUBCTL/audit.flush.exit" ] && exit "\$(cat "\$STUBCTL/audit.flush.exit")"
        exit 0
        ;;
    *"mojosec_audit.py restore"*)
        [ -f "\$MOJOSEC_AUDIT_HELPER" ] && echo "ASSET_PRESENT restore" >> "\$CALLLOG"
        [ -f "\$STUBCTL/audit.restore.exit" ] && exit "\$(cat "\$STUBCTL/audit.restore.exit")"
        exit 0
        ;;
    *"mojosec_changes.py run --operation-id"*|*"mojosec_changes.py pip-run --operation-id"*)
        exec env -u DJANGO_SETTINGS_MODULE PYTHONPATH="$REPO" "$REAL_PYTHON3" \
            "$TMP/trusted_shim.py" "\$@"
        ;;
    "-m mojo.deploy"*)
        if [ -f "\$STUBCTL/render.exit" ]; then exit "\$(cat "\$STUBCTL/render.exit")"; fi
        exec env -u DJANGO_SETTINGS_MODULE PYTHONPATH="$REPO" "$REAL_PYTHON3" "\$@"
        ;;
    *"print(mojo.__version__)"*)
        # The installed-framework probe. mojo.version ctl: absent = a normal
        # node (1.0.0); present-but-empty = no framework installed at all.
        cat "\$STUBCTL/mojo.version" 2>/dev/null || echo "1.0.0"
        exit 0
        ;;
esac
ctl="\$STUBCTL/python3.exit"
[ -f "\$ctl" ] && exit "\$(cat "\$ctl")"
exit 0
EOF
    chmod +x "$STUB/python3"
}

# MOJOSEC_ETC / MOJOSEC_STABLE_HELPER default into $TMP for EVERY run, so no
# scenario depends on the host's real /etc/mojosec being absent (on an enrolled
# Linux box it is not).
run_post_deploy() { # args...
    ( cd "$TMP" && PROJ_PATH="$PROJ" NGINX_ETC="$TMP/nginx_etc" \
        SYSTEMD_ETC="$TMP/systemd_etc" CRON_ETC="$TMP/cron_etc" \
        LOGROTATE_ETC="$TMP/logrotate_etc" PATH="$STUB:$PATH" \
        MOJOSEC_ETC="$TMP/mojosec_etc" \
        MOJOSEC_STABLE_HELPER="$TMP/mojosec_lib/mojosec_changes.py" \
        MOJOSEC_PYTHON=python3 \
        bash "$PROJ/aws/post_deploy.sh" "$@" )
}

run_post_deploy_env() { # VAR=val ... -- args...
    local envs=()
    while [ "$1" != "--" ]; do envs+=("$1"); shift; done
    shift
    ( cd "$TMP" && env MOJOSEC_ETC="$TMP/mojosec_etc" \
        MOJOSEC_STABLE_HELPER="$TMP/mojosec_lib/mojosec_changes.py" \
        "${envs[@]}" PROJ_PATH="$PROJ" NGINX_ETC="$TMP/nginx_etc" \
        SYSTEMD_ETC="$TMP/systemd_etc" CRON_ETC="$TMP/cron_etc" \
        LOGROTATE_ETC="$TMP/logrotate_etc" PATH="$STUB:$PATH" \
        MOJOSEC_PYTHON=python3 \
        bash "$PROJ/aws/post_deploy.sh" "$@" )
}

set_request_service() { # exact sealed stage-0 projection value
    printf '%s\n' "$1" > "$CTL/request_service.value"
}

# ── tests ────────────────────────────────────────────────────────────────────

setup_tls_case() {
    local server_name="${1:-app.example.test}"
    setup_env
    echo 9.9.9 > "$CTL/mojo.version"
    TLS_LIVE="$TMP/letsencrypt/live/app.example.test"
    TLS_ARCHIVE="$TMP/letsencrypt/archive/app.example.test"
    mkdir -p "$TLS_LIVE" "$TLS_ARCHIVE"
    printf 'certificate\n' > "$TLS_ARCHIVE/fullchain7.pem"
    printf 'private-key\n' > "$TLS_ARCHIVE/privkey7.pem"
    chmod 0644 "$TLS_ARCHIVE/fullchain7.pem"
    chmod 0600 "$TLS_ARCHIVE/privkey7.pem"
    ln -s ../../archive/app.example.test/fullchain7.pem "$TLS_LIVE/fullchain.pem"
    ln -s ../../archive/app.example.test/privkey7.pem "$TLS_LIVE/privkey.pem"
    cat > "$PROJ/aws/nginx/conf.d/app.conf" <<EOF
# HTTPS — repository-owned vhost comments may use UTF-8.
server {
    listen 443 ssl;
    server_name $server_name;
    ssl_certificate /etc/ssl/certs/ssl-cert-snakeoil.pem;
    ssl_certificate_key /etc/ssl/private/ssl-cert-snakeoil.key;
    add_header X-Repository yes;
}
EOF
}

write_installed_tls() { # server_name cert-directive key-directive [extra]
    local host="$1" cert="$2" key="$3" extra="${4:-}"
    cat > "$TMP/nginx_etc/conf.d/app.conf" <<EOF
server {
    listen 443 ssl;
    server_name $host;
$cert
$key
    include /etc/letsencrypt/options-ssl-nginx.conf;
    location /old-operator-route { return 418; }
    # installed-only comment
}
$extra
EOF
}

assert_repo_tls() {
    assert_has "$TMP/nginx_etc/conf.d/app.conf" "ssl-cert-snakeoil.pem" "$1 uses the repository certificate"
    assert_has "$TMP/nginx_etc/conf.d/app.conf" "ssl-cert-snakeoil.key" "$1 uses the repository key"
}

assert_tls_refused() {
    local label="$1" rc
    run_post_deploy > "$OUT" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then ok "$label is refused"; else fail "$label deployed"; fi
    assert_lacks "$TMP/nginx_etc/conf.d/app.conf" "ssl-cert-snakeoil" \
        "$label leaves the installed vhost untouched"
    assert_has "$OUT" "TLS vhost convergence refused unsafe or changed state" \
        "$label reports one bounded refusal"
    assert_lacks "$OUT" "$TLS_LIVE" "$label does not log certificate paths"
}

test_tls_lineage_preservation() {
    echo "post_deploy.sh: repository vhost keeps only a proven TLS lineage"

    setup_tls_case
    run_post_deploy > "$OUT" 2>&1
    assert_eq "$?" 0 "a fresh placeholder vhost deploys"
    assert_repo_tls "a fresh vhost"

    setup_tls_case
    write_installed_tls app.example.test \
        "    ssl_certificate $TLS_LIVE/fullchain.pem;" \
        "    ssl_certificate_key $TLS_LIVE/privkey.pem;"

    run_post_deploy > "$OUT" 2>&1
    local rc=$?
    [ "$rc" -eq 0 ] || sed -n '1,20p' "$OUT"
    assert_eq "$rc" 0 "a valid installed lineage deploys"
    assert_has "$TMP/nginx_etc/conf.d/app.conf" "$TLS_LIVE/fullchain.pem" \
        "the proven fullchain lineage survives repository convergence"
    assert_has "$TMP/nginx_etc/conf.d/app.conf" "$TLS_LIVE/privkey.pem" \
        "the proven private-key lineage survives repository convergence"
    assert_has "$TMP/nginx_etc/conf.d/app.conf" "X-Repository yes" \
        "the repository remains authoritative for non-certificate directives"
    assert_has "$TMP/nginx_etc/conf.d/app.conf" "HTTPS — repository-owned" \
        "UTF-8 repository comments survive certificate overlay"
    assert_lacks "$TMP/nginx_etc/conf.d/app.conf" "old-operator-route" \
        "an installed route is not carried into repository bytes"
    assert_lacks "$TMP/nginx_etc/conf.d/app.conf" "options-ssl-nginx.conf" \
        "an installed Certbot option include is not carried forward"
    assert_lacks "$TMP/nginx_etc/conf.d/app.conf" "installed-only comment" \
        "an installed comment is not carried forward"

    cp "$TMP/nginx_etc/conf.d/app.conf" "$TMP/first-app.conf"
    run_post_deploy > "$OUT" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || sed -n '1,20p' "$OUT"
    assert_eq "$rc" 0 "a repeated valid-lineage deploy succeeds"
    if cmp -s "$TMP/first-app.conf" "$TMP/nginx_etc/conf.d/app.conf"; then
        ok "the TLS-lineage overlay is byte-idempotent"
    else
        fail "the TLS-lineage overlay changed bytes on its second run"
    fi

    setup_tls_case
    write_installed_tls app.example.test \
        "    ssl_certificate $TLS_LIVE/fullchain.pem;" \
        "    ssl_certificate_key $TLS_LIVE/privkey.pem;"
    printf '\377' >> "$PROJ/aws/nginx/conf.d/app.conf"
    assert_tls_refused "invalid UTF-8 repository bytes"

    setup_tls_case k.example.test
    write_installed_tls K.example.test \
        "    ssl_certificate $TLS_LIVE/fullchain.pem;" \
        "    ssl_certificate_key $TLS_LIVE/privkey.pem;"
    assert_tls_refused "a Unicode-confusable server name"

    setup_tls_case
    write_installed_tls app.example.test \
        "    ssl_certificate /etc/ssl/certs/ssl-cert-snakeoil.pem;" \
        "    ssl_certificate_key /etc/ssl/private/ssl-cert-snakeoil.key;"
    run_post_deploy > "$OUT" 2>&1
    assert_eq "$?" 0 "an unambiguous non-Certbot pair takes repository bytes"
    assert_repo_tls "an unambiguous non-Certbot pair"
    assert_lacks "$TMP/nginx_etc/conf.d/app.conf" "old-operator-route" \
        "a non-Certbot vhost retains no installed routing"

    setup_tls_case
    write_installed_tls renamed.example.test \
        "    ssl_certificate $TLS_LIVE/fullchain.pem;" \
        "    ssl_certificate_key $TLS_LIVE/privkey.pem;"
    assert_tls_refused "a renamed host"

    setup_tls_case
    local duplicate_cert
    duplicate_cert="$(printf '    ssl_certificate %s/fullchain.pem;\n    ssl_certificate %s/fullchain.pem;' \
        "$TLS_LIVE" "$TLS_LIVE")"
    write_installed_tls app.example.test \
        "$duplicate_cert" \
        "    ssl_certificate_key $TLS_LIVE/privkey.pem;"
    assert_tls_refused "duplicate certificate directives"

    setup_tls_case
    write_installed_tls app.example.test \
        "    ssl_certificate $TLS_LIVE/fullchain.pem;" \
        "    # missing private key"
    assert_tls_refused "an incomplete certificate pair"

    setup_tls_case
    write_installed_tls app.example.test \
        "    ssl_certificate $TLS_LIVE/../other/fullchain.pem;" \
        "    ssl_certificate_key $TLS_LIVE/../other/privkey.pem;"
    assert_tls_refused "a traversal-shaped live path"

    setup_tls_case
    write_installed_tls app.example.test \
        "    ssl_certificate $TLS_LIVE/fullchain.pem;" \
        "    ssl_certificate_key $TLS_LIVE/privkey.pem;"
    rm -f "$TLS_ARCHIVE/privkey7.pem"
    assert_tls_refused "a missing archive target"

    setup_tls_case
    write_installed_tls app.example.test \
        "    ssl_certificate $TLS_LIVE/fullchain.pem;" \
        "    ssl_certificate_key $TLS_LIVE/privkey.pem;"
    chmod 0640 "$TLS_ARCHIVE/privkey7.pem"
    assert_tls_refused "a group-readable private key"

    setup_tls_case
    write_installed_tls app.example.test \
        "    ssl_certificate $TLS_LIVE/fullchain.pem;" \
        "    ssl_certificate_key $TLS_LIVE/privkey.pem;"
    chmod 0664 "$TMP/nginx_etc/conf.d/app.conf"
    assert_tls_refused "unsafe installed-vhost mode"

    setup_tls_case
    write_installed_tls app.example.test \
        "    ssl_certificate $TLS_LIVE/fullchain.pem;" \
        "    ssl_certificate_key $TLS_LIVE/privkey.pem;"
    chmod 0775 "$TMP/nginx_etc/conf.d"
    assert_tls_refused "a group-writable destination directory"

    setup_tls_case
    write_installed_tls app.example.test \
        "    ssl_certificate $TLS_LIVE/fullchain.pem;" \
        "    ssl_certificate_key $TLS_LIVE/privkey.pem;" \
        'server { listen 443 ssl; server_name second.example.test; ssl_certificate /tmp/a; ssl_certificate_key /tmp/b; }'
    assert_tls_refused "multiple TLS servers"

    setup_tls_case
    printf 'do-not-overwrite\n' > "$TMP/vhost-symlink-target"
    ln -s "$TMP/vhost-symlink-target" "$TMP/nginx_etc/conf.d/app.conf"
    run_post_deploy > "$OUT" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then ok "an installed-vhost symlink is refused"; else fail "an installed-vhost symlink deployed"; fi
    assert_has "$TMP/vhost-symlink-target" "do-not-overwrite" \
        "the symlink target is never followed or overwritten"
    if [ -L "$TMP/nginx_etc/conf.d/app.conf" ]; then ok "the refused destination remains a symlink"; else fail "the refused destination symlink was replaced"; fi

    setup_tls_case
    write_installed_tls app.example.test \
        "    ssl_certificate $TLS_LIVE/fullchain.pem;" \
        "    ssl_certificate_key $TLS_LIVE/privkey.pem;"
    "$REAL_PYTHON3" - "$TMP/nginx_etc/conf.d/app.conf" \
        "$CTL/tls-race-ready" "$CTL/tls-race-stop" <<'PY' &
import os
import sys

path, ready, stop = sys.argv[1:]
os.utime(path, None)
open(ready, "wb").close()
while not os.path.exists(stop):
    os.utime(path, None)
PY
    local race_pid=$!
    while [ ! -e "$CTL/tls-race-ready" ]; do :; done
    run_post_deploy > "$OUT" 2>&1
    rc=$?
    touch "$CTL/tls-race-stop"
    wait "$race_pid"
    if [ "$rc" -ne 0 ]; then ok "a changed installed snapshot is refused"; else fail "a racing installed snapshot deployed"; fi
    assert_has "$OUT" "TLS vhost convergence refused unsafe or changed state" \
        "snapshot refusal is bounded and does not disclose TLS paths"
    assert_lacks "$OUT" "$TLS_LIVE" "snapshot refusal does not log certificate paths"
}

if [ "${POST_DEPLOY_TLS_ONLY:-0}" = "1" ]; then
    test_tls_lineage_preservation
    echo
    echo "test_post_deploy_sh: $PASS passed, $FAIL failed"
    [ "$FAIL" -eq 0 ]
    exit $?
fi

test_tls_lineage_preservation

echo "post_deploy.sh: --framework converges the pin; bare resolves latest; deps come FIRST"
setup_env
run_post_deploy --framework 9.9.9 > "$OUT" 2>&1
rc=$?
[ "$rc" -eq 0 ] || sed -n '1,20p' "$OUT"
assert_eq "$rc" 0 "--framework run exits 0"
assert_in_log "CMD pip install -r $PROJ/requirements.txt" \
    "requirements install uses an absolute path that survives the trusted helper cwd"
assert_in_log "CMD curl .*https://pypi.org/pypi/django-mojo/9.9.9/json" \
    "the pin is confirmed against the PyPI JSON API — the fresh pipe — first"
assert_in_log "CMD pip install django-mojo==9.9.9" \
    "pinned install targets the exact fleet version"
assert_order "CMD pip install -r" "CMD pip install django-mojo==9.9.9" \
    "requirements install precedes the framework pin"
setup_env
run_post_deploy >/dev/null 2>&1
assert_eq "$?" 0 "bare run exits 0"
assert_in_log "CMD curl .*https://pypi.org/pypi/django-mojo/json" \
    "bare run resolves latest from the PyPI JSON API"
assert_in_log "CMD pip install django-mojo==9.9.9" \
    "bare run converges onto the resolved latest as an exact pin"
assert_not_in_log "pip install .*--upgrade django-mojo" \
    "the blind --upgrade path is gone — a stale index silently installed the previous version"
assert_order "CMD pip install -r" "CMD pip install django-mojo==9.9.9" \
    "requirements install precedes the framework install"
assert_order "CMD pip install django-mojo==9.9.9" "CMD python3 -m mojo.deploy render" \
    "render runs AFTER the framework install (renders the just-installed templates)"

echo "post_deploy.sh: framework convergence retries through caches, then fails OPEN"
setup_env
echo 1 > "$CTL/pip.framework_fail_times"
run_post_deploy_env FRAMEWORK_RETRY_DELAY=0 -- --framework 9.9.9 > "$OUT" 2>&1
assert_eq "$?" 0 "a stale-then-fresh catalog still deploys"
assert_in_log "CMD pip install django-mojo==9.9.9" "first attempt uses the normal cache"
assert_in_log "CMD pip install --no-cache-dir django-mojo==9.9.9" \
    "the retry bypasses every pip cache — no feature-detected flags, any pip version"
assert_not_in_log "deploy_warning framework" \
    "a converged retry is a success, not a warning"

setup_env
echo 404 > "$CTL/pypi.version.code"
run_post_deploy --framework 8.8.8 > "$OUT" 2>&1
assert_eq "$?" 0 "a pin that does not exist on PyPI must not veto a tested API deploy"
assert_not_in_log "CMD pip install django-mojo==8.8.8" \
    "a JSON-confirmed-absent version is never install-attempted (waiting cannot help)"
assert_in_log "deploy_warning framework" \
    "skipping the framework files a deploy warning"
assert_in_log "CMD nginx -t" "the deploy still completes past nginx"
assert_has "$OUT" "deploying on installed" \
    "the fallback names the framework that stays live"

setup_env
echo 99 > "$CTL/pip.framework_fail_times"
run_post_deploy_env FRAMEWORK_RETRY_DELAY=0 FRAMEWORK_RETRIES=2 -- --framework 9.9.9 > "$OUT" 2>&1
assert_eq "$?" 0 "exhausted install retries fail OPEN, never the deploy"
assert_in_log "deploy_warning framework" "the exhausted convergence files a deploy warning"
assert_in_log "CMD systemctl restart mojo-asgi" \
    "the app still restarts on the installed framework"

setup_env
echo 9.9.9 > "$CTL/mojo.version"
run_post_deploy --framework 9.9.9 > "$OUT" 2>&1
assert_eq "$?" 0 "already-at-target run exits 0"
assert_not_in_log "CMD pip install django-mojo==" \
    "an already-installed target skips the install entirely"

setup_env
: > "$CTL/mojo.version"
echo 99 > "$CTL/pip.framework_fail_times"
run_post_deploy_env FRAMEWORK_RETRY_DELAY=0 FRAMEWORK_RETRIES=1 -- --framework 9.9.9 > "$OUT" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    ok "a node with NO framework at all still refuses (nothing could serve)"
else
    fail "a node with NO framework at all still refuses (exit was 0)"
fi
assert_has "$OUT" "no django-mojo is installed at all" \
    "the fatal names the one truly fatal state"

echo "post_deploy.sh: --migrate runs migrate_locked BEFORE the restart; absent runs none"
setup_env
run_post_deploy --framework 9.9.9 --migrate >/dev/null 2>&1
assert_eq "$?" 0 "--migrate run exits 0"
assert_in_log "manage.py migrate_locked --noinput" "migrate_locked invoked"
assert_order "migrate_locked" "CMD systemctl restart mojo-asgi" \
    "migration lands before the app restart"
setup_env
run_post_deploy --framework 9.9.9 >/dev/null 2>&1
assert_not_in_log "migrate" "no migration of any kind without --migrate"

echo "post_deploy.sh: BARE invocation completes the full sequence (cutover-load-bearing)"
setup_env
run_post_deploy > "$OUT" 2>&1
assert_eq "$?" 0 "bare invocation exits 0"
assert_in_log "CMD nginx -t" "nginx config test ran"
assert_in_log "CMD systemctl restart mojo-asgi" "app restarted"
assert_in_log "CMD curl .*https://127.0.0.1/api/version" \
    "the probe targets the default PROBE_URL"
assert_file "$TMP/nginx_etc/nginx.conf" "nginx.conf landed in the NGINX_ETC seam"
assert_file "$TMP/nginx_etc/conf.d/00_django_mojo_runtime.conf" \
    "a forward-installed runtime fragment survives an older shell rollback"
assert_file "$TMP/nginx_etc/sec.d/hardening.conf" "sec.d hardening landed"
for v in probe.conf app.conf; do
    assert_file "$TMP/nginx_etc/conf.d/$v" "vhost $v converged into conf.d"
done
assert_no_file "$TMP/nginx_etc/conf.d/sample.example.conf" \
    ".example vhost excluded from the conf.d glob"
assert_has "$OUT" "converged 2 vhost(s)" "conf.d convergence logs its count"
assert_not_in_log "migrate" "bare invocation never migrates"

echo "post_deploy.sh: sealed request-service selection is strict and role-local"
setup_env
set_request_service true
run_post_deploy > "$OUT" 2>&1
assert_eq "$?" 0 "explicit request_service=true preserves a successful request deploy"
assert_in_log "CMD systemctl restart mojo-asgi" \
    "explicit true follows the existing ASGI restart path"
assert_in_log "CMD curl .*https://127.0.0.1/api/version" \
    "explicit true keeps the request readiness probe"

setup_env
set_request_service false
run_post_deploy > "$OUT" 2>&1
assert_eq "$?" 0 "request_service=false completes without framework ASGI"
assert_in_log "CMD systemctl disable --now mojo-asgi.service" \
    "a non-request node revokes an existing framework request service"
assert_order "disable --now mojo-asgi.service" "CMD pip install -r" \
    "request authority is revoked before dependency or project convergence"
assert_not_in_log "CMD systemctl restart mojo-asgi" \
    "a non-request node never restarts framework ASGI"
assert_not_in_log "CMD curl .*https://127.0.0.1/api/version" \
    "intentional ASGI absence skips the request probe"
assert_has "$OUT" "framework request service disabled" \
    "non-request success names the lifecycle it proved"
assert_file "$PROJ/var/deploy/post_deploy.sh" \
    "non-request success still snapshots rollback's canonical script"

setup_env
set_request_service False
run_post_deploy > "$OUT" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    ok "a malformed sealed value fails closed"
else
    fail "a malformed sealed value was treated as request authority"
fi
assert_in_log "CMD systemctl disable --now mojo-asgi.service" \
    "malformed request authority is actively revoked"
assert_not_in_log "CMD pip install -r" \
    "malformed authority aborts before deployment work"

setup_env
set_request_service error
run_post_deploy > "$OUT" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    ok "a refused sealed authority fails closed"
else
    fail "a refused sealed authority granted request authority"
fi
assert_in_log "CMD systemctl disable --now mojo-asgi.service" \
    "a refused sealed authority revokes request authority"

setup_env
set_request_service false
touch "$CTL/asgi.revoke_fail"
run_post_deploy > "$OUT" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    ok "failed request-service revocation aborts the deploy"
else
    fail "failed request-service revocation was reported ready"
fi
assert_has "$OUT" "unsafe ActiveState active" \
    "revocation failure names the still-active authority"
assert_not_in_log "CMD pip install -r" \
    "revocation failure stops before dependency work"

setup_env
set_request_service false
touch "$CTL/asgi.absent"
run_post_deploy > "$OUT" 2>&1
assert_eq "$?" 0 "an intentionally absent ASGI unit is a safe non-request state"
assert_not_in_log "CMD systemctl restart mojo-asgi" \
    "an absent ASGI unit is never resurrected"

setup_env
set_request_service false
touch "$CTL/asgi.query_fail"
run_post_deploy > "$OUT" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    ok "a systemd state-query failure aborts non-request readiness"
else
    fail "a systemd state-query failure false-passed non-request readiness"
fi
assert_has "$OUT" "cannot verify mojo-asgi LoadState" \
    "a state-query failure names the unproved systemd state"
assert_not_in_log "CMD pip install -r" \
    "an unproved systemd state stops before dependency work"

echo "post_deploy.sh: the run records its own phase timings, under update.sh's pass"
setup_env
run_post_deploy > "$OUT" 2>&1
assert_eq "$?" 0 "phase-timing fixture completes the request path"
assert_file "$PROJ/var/deploy/phase_timings" "post_deploy recorded phase timings"
pd_field() { awk -v n="$1" '{print $n}' "$PROJ/var/deploy/phase_timings" 2>/dev/null; }
assert_eq "$(pd_field 2 | tr '\n' ' ')" \
    "deps framework collectstatic render nginx mojosec restart probe " \
    "the phases are recorded in the order they happened"
assert_eq "$(pd_field 3 | grep -cv '^[0-9][0-9]*$')" "0" \
    "every value is all digits — this host's date has no %3N, and the literal \
format must never be recorded as a number"
assert_eq "$(pd_field 1 | sort -u | tr '\n' ' ')" "deploy " \
    "with no phase_pass file the pass defaults to deploy"

setup_env
mkdir -p "$PROJ/var/deploy"
printf 'rollback\n' > "$PROJ/var/deploy/phase_pass"
run_post_deploy --framework 9.9.9 > "$OUT" 2>&1
assert_eq "$?" 0 "a rollback-pass run still exits 0"
assert_eq "$(pd_field 1 | sort -u | tr '\n' ' ')" "rollback " \
    "the pass comes from the FILE — a rollback can leave this script older \
than the update.sh calling it, and a new flag would die on argv"
printf 'nonsense\n' > "$PROJ/var/deploy/phase_pass"
: > "$PROJ/var/deploy/phase_timings"
run_post_deploy --framework 9.9.9 > "$OUT" 2>&1
assert_eq "$?" 0 "an unreadable pass never fails a deploy"
assert_eq "$(pd_field 1 | sort -u | tr '\n' ' ')" "deploy " \
    "an unrecognized pass falls back to deploy"

setup_env
run_post_deploy --framework 9.9.9 --migrate > "$OUT" 2>&1
assert_eq "$?" 0 "a migrating run exits 0"
assert_eq "$(pd_field 2 | grep -c '^migrate$')" "1" \
    "the migration is timed when it runs"

setup_env
run_post_deploy > "$OUT" 2>&1

echo "post_deploy.sh: templates render into var/deploy, then install substituted"
assert_file "$PROJ/var/deploy/cron.d/1_certbot" "rendered cron landed in var/deploy"
assert_file "$PROJ/var/deploy/systemd/mojo-asgi.service" "rendered unit landed in var/deploy"
for c in 1_certbot 2_mojo_cron 3_mojo_jobs 4_certbot_sync 9_project_extra; do
    assert_file "$TMP/cron_etc/$c" "cron $c installed from var/deploy"
done
for u in mojo-asgi.service config-sync.service config-sync.timer project-extra.service project-extra.timer; do
    assert_file "$TMP/systemd_etc/$u" "unit $u installed from var/deploy"
done
assert_lacks "$TMP/cron_etc/1_certbot" "@PROJ_PATH@" "@PROJ_PATH@ fully substituted in installed cron"
assert_has "$TMP/cron_etc/1_certbot" "$PROJ/var/django.conf" "certbot cron carries the real project path"
assert_has "$TMP/cron_etc/1_certbot" "python3 -m mojo.deploy.certbot_sync" "certbot cron invokes the packaged module"
assert_lacks "$TMP/systemd_etc/mojo-asgi.service" "@WORKERS@" "@WORKERS@ fully substituted in installed unit"
assert_has "$TMP/systemd_etc/mojo-asgi.service" "--workers 4" "default ASGI_WORKERS renders 4"
assert_has "$TMP/cron_etc/3_mojo_jobs" "ec2-user" "default APP_USER renders into 3_mojo_jobs"
assert_in_log "CMD systemctl enable --now config-sync.timer" "shipped timer enabled"
assert_in_log "CMD systemctl enable --now project-extra.timer" "project extra timer enabled"
assert_in_log "CMD chown -R ec2-user:www" "var/logs ownership pass uses the default users"
assert_in_log "CMD python3 -E -P -m mojo.deploy.mojosec converge --mode enrolled --criticality enrolled" \
    "ordinary deploys preserve the root-enrolled MojoSec lifecycle"
assert_in_log "MOJOSEC_CWD /" \
    "MojoSec preflight and convergence discard the app-writable cwd"
assert_file "$PROJ/var/deploy/post_deploy.sh" "self-snapshot present after success"
if cmp -s "$PROJ/aws/post_deploy.sh" "$PROJ/var/deploy/post_deploy.sh"; then
    ok "self-snapshot is byte-identical to the executing copy"
else
    fail "self-snapshot differs from the executing copy"
fi

echo "post_deploy.sh: cron convergence — sweep discovers, node_retired.conf declares"
for c in 1mojocron 3mojo_jogs; do
    assert_no_file "$TMP/cron_etc/$c" "stale project cron $c discovered and removed"
done
assert_no_file "$TMP/cron_etc/2certbot" "declared retired cron 2certbot removed (never mentions PROJ_PATH)"
assert_file "$TMP/cron_etc/0hourly" "non-project cron 0hourly left alone"
assert_no_file "$TMP/nginx_etc/conf.d/stale-old.conf" "declared retired vhost removed"
assert_has "$OUT" "retiring declared name: cron.d/2certbot" "declared retirement is logged"

echo "post_deploy.sh: PROBE_URL / APP_USER / WEB_USER / ASGI_WORKERS overrides"
setup_env
run_post_deploy_env PROBE_URL="http://127.0.0.1:8080/api/version" APP_USER="appu" \
    WEB_USER="webu" ASGI_WORKERS="7" -- >/dev/null 2>&1
assert_eq "$?" 0 "overridden run exits 0"
assert_in_log "CMD curl .*http://127.0.0.1:8080/api/version" \
    "the probe targets the overridden PROBE_URL"
assert_not_in_log "CMD curl .*https://127.0.0.1/api/version " \
    "the default probe URL is not used once overridden"
assert_in_log "CMD chown -R appu:webu" "chown argv carries APP_USER:WEB_USER"
assert_has "$TMP/systemd_etc/mojo-asgi.service" "--workers 7" "ASGI_WORKERS renders into the unit"
assert_has "$TMP/systemd_etc/mojo-asgi.service" "User=webu" "WEB_USER renders into the unit"
assert_has "$TMP/cron_etc/3_mojo_jobs" "appu" "APP_USER renders into 3_mojo_jobs"

echo "post_deploy.sh: MojoSec mode and criticality are explicit"
setup_env
run_post_deploy_env MOJOSEC_MODE="off" MOJOSEC_DEPLOY_CRITICALITY="required" -- >/dev/null 2>&1
assert_eq "$?" 0 "explicit MojoSec off run exits 0"
assert_in_log "CMD python3 -E -P -m mojo.deploy.mojosec converge --mode off --criticality required" \
    "off/required reaches the exact package lifecycle command"

echo "post_deploy.sh: MojoSec failures warn, restore the new route baseline, and continue"
setup_env
printf '# old graph\n# MojoSec exact receiver cap\ninclude /etc/nginx/snippets/mojosec_receiver.conf;\n' \
    > "$TMP/nginx_etc/django.inc"
echo "1" > "$CTL/mojosec.converge.exit"
run_post_deploy_env MOJOSEC_MODE="off" MOJOSEC_DEPLOY_CRITICALITY="required" -- \
    > "$OUT" 2>&1
assert_eq "$?" 0 "generic MojoSec failure does not veto a healthy app deploy"
if cmp -s "$PROJ/aws/nginx/django.inc" "$TMP/nginx_etc/django.inc"; then
    ok "generic failure restores the new release's django.inc bytes"
else
    fail "generic failure did not restore the new release's django.inc"
fi
assert_has "$OUT" "phase=mojosec; deploy continues" \
    "generic failure is an explicit deployment warning"
assert_in_log "manage.py deploy_warning mojosec" \
    "generic failure files a fixed-phase incident"
assert_lacks "$OUT" "module absent" "generic failure never enters downgrade cleanup"

setup_env
echo "3" > "$CTL/mojosec.preflight.exit"
run_post_deploy_env MOJOSEC_MODE="off" MOJOSEC_DEPLOY_CRITICALITY="required" -- \
    > "$OUT" 2>&1
assert_eq "$?" 0 "true pre-feature module absence performs exact off cleanup"
assert_has "$OUT" "module absent" "true absence is logged as downgrade cleanup"
assert_not_in_log "CMD python3 -E -P -m mojo.deploy.mojosec converge" \
    "absent module is never mistaken for a runnable converge"

echo "post_deploy.sh: legacy Python can retire/off but cannot activate observe"
setup_env
echo "1" > "$CTL/mojosec.version.exit"
echo "3" > "$CTL/mojosec.preflight.exit"
run_post_deploy_env MOJOSEC_MODE="off" MOJOSEC_DEPLOY_CRITICALITY="required" -- \
    > "$OUT" 2>&1
assert_eq "$?" 0 "Python 3.10 module-absent legacy node still performs off cleanup"
assert_not_in_log "CMD python3 -E -P" "legacy interpreter is never passed unsupported -P"

setup_env
echo "1" > "$CTL/mojosec.version.exit"
run_post_deploy > "$OUT" 2>&1
assert_eq "$?" 0 "Python 3.10 package-present unenrolled node can converge enrolled-off"
assert_in_log "CMD python3 -E -m mojo.deploy.mojosec converge --mode enrolled --criticality enrolled" \
    "legacy enrolled-off convergence uses -E from root-owned cwd without unsupported -P"

echo "post_deploy.sh: old MojoSec argparse and downgrade lifecycle are exact"
setup_env
echo "4" > "$CTL/mojosec.preflight.exit"
run_post_deploy_env MOJOSEC_MODE="off" MOJOSEC_DEPLOY_CRITICALITY="required" -- \
    > "$OUT" 2>&1
assert_eq "$?" 0 "capability-zero old module accepts its historical argparse contract"
assert_in_log "CMD python3 -E -P -m mojo.deploy.mojosec converge --mode off --criticality required" \
    "old module receives mode and criticality"
assert_not_in_log "old argparse rejected --project-path" \
    "new project-path flag is omitted for a capability-zero old module"

setup_env
helper="$TMP/mojosec_audit.py"; state="$TMP/audit-state.json"
: > "$helper"; : > "$state"
echo "4" > "$CTL/mojosec.preflight.exit"
run_post_deploy_env MOJOSEC_MODE="off" MOJOSEC_DEPLOY_CRITICALITY="required" \
    MOJOSEC_AUDIT_HELPER="$helper" MOJOSEC_AUDIT_STATE="$state" \
    MOJOSEC_AUDIT_PYTHON=python3 -- > "$OUT" 2>&1
assert_eq "$?" 0 "old module handoff succeeds before provenance retirement"
assert_in_log "ASSET_PRESENT flush" "old handoff retains helper through flush"
assert_in_log "ASSET_PRESENT restore" "old handoff retains helper through Audit restore"
assert_no_file "$helper" "old converge success retires provenance helper"

for prior in active inactive; do
    setup_env
    helper="$TMP/mojosec_audit.py"
    state="$TMP/audit-state.json"
    : > "$helper"; : > "$state"
    echo "4" > "$CTL/mojosec.preflight.exit"
    echo "1" > "$CTL/mojosec.converge.exit"
    [ "$prior" = "active" ] || touch "$CTL/mojosec.inactive"
    run_post_deploy_env MOJOSEC_MODE="off" MOJOSEC_DEPLOY_CRITICALITY="required" \
        MOJOSEC_AUDIT_HELPER="$helper" MOJOSEC_AUDIT_STATE="$state" \
        MOJOSEC_AUDIT_PYTHON=python3 -- > "$OUT" 2>&1
    assert_eq "$?" 0 "old converge failure does not veto the app deploy ($prior)"
    if [ "$prior" = "active" ]; then
        [ ! -f "$CTL/mojosec.inactive" ] && ok "old converge failure restores active" || \
            fail "old converge failure stranded active service stopped"
    else
        [ -f "$CTL/mojosec.inactive" ] && ok "old converge failure preserves inactive" || \
            fail "old converge failure started an originally inactive service"
    fi
    assert_file "$helper" "old converge failure preserves provenance assets ($prior)"
done

setup_env
helper="$TMP/mojosec_audit.py"; state="$TMP/audit-state.json"
: > "$helper"; : > "$state"
echo "3" > "$CTL/mojosec.preflight.exit"
run_post_deploy_env MOJOSEC_MODE="observe" MOJOSEC_DEPLOY_CRITICALITY="required" \
    MOJOSEC_AUDIT_HELPER="$helper" MOJOSEC_AUDIT_STATE="$state" \
    MOJOSEC_AUDIT_PYTHON=python3 -- > "$OUT" 2>&1
assert_eq "$?" 0 "module-absent observe failure does not veto the app deploy"
[ ! -f "$CTL/mojosec.inactive" ] && ok "module-absent failure restores active" || \
    fail "module-absent failure stranded active service stopped"
assert_file "$helper" "module-absent terminal failure preserves provenance assets"

setup_env
helper="$TMP/mojosec_audit.py"; state="$TMP/audit-state.json"
: > "$helper"; : > "$state"
echo "3" > "$CTL/mojosec.preflight.exit"
run_post_deploy_env MOJOSEC_MODE="off" MOJOSEC_DEPLOY_CRITICALITY="required" \
    MOJOSEC_AUDIT_HELPER="$helper" MOJOSEC_AUDIT_STATE="$state" \
    MOJOSEC_AUDIT_PYTHON=python3 -- > "$OUT" 2>&1
assert_eq "$?" 0 "module-absent success completes downgrade handoff"
assert_in_log "ASSET_PRESENT flush" "stable helper exists throughout pending flush"
assert_in_log "ASSET_PRESENT restore" "stable helper exists throughout Audit restore"
assert_no_file "$helper" "stable helper retires only after fallback cleanup succeeds"

setup_env
printf '# active old graph\n# MojoSec exact receiver cap\ninclude /etc/nginx/snippets/mojosec_receiver.conf;\n' \
    > "$TMP/nginx_etc/django.inc"
cp "$TMP/nginx_etc/django.inc" "$TMP/prior-django.inc"
echo "old security log" > "$TMP/nginx_etc/conf.d/00_mojosec.conf"
echo "old rotation" > "$TMP/logrotate_etc/mojosec"
echo "3" > "$CTL/mojosec.preflight.exit"
echo "1" > "$CTL/nginx.exit"
run_post_deploy_env MOJOSEC_MODE="off" MOJOSEC_DEPLOY_CRITICALITY="required" -- \
    > "$OUT" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then ok "failed downgrade cleanup exits non-zero"; else fail "failed downgrade cleanup claimed success"; fi
assert_has "$TMP/nginx_etc/conf.d/00_mojosec.conf" "old security log" \
    "failed downgrade cleanup restores prior security fragment"
assert_has "$TMP/logrotate_etc/mojosec" "old rotation" \
    "failed downgrade cleanup restores prior rotation file"
if cmp -s "$PROJ/aws/nginx/django.inc" "$TMP/nginx_etc/django.inc"; then
    ok "failed downgrade cleanup restores the new release's django.inc"
else
    fail "failed downgrade cleanup lost the new release's django.inc"
fi

echo "post_deploy.sh: an enrolled node deploys under an ACTIVE trusted-change journal (item 2014)"
# The regression for the Mojoware EU outage: with trusted-change support live,
# the MojoSec step declared /etc/mojosec/audit-state.json — control state the
# validator rejects by design — so the deploy aborted BEFORE the converge child,
# where item 2007's observe-and-degrade handler lives.
setup_env
echo '{}' > "$TMP/mojosec_etc/config.json"
: > "$TMP/mojosec_lib/mojosec_changes.py"
run_post_deploy --framework 9.9.9 --migrate > "$OUT" 2>&1
assert_eq "$?" 0 "an enrolled node completes a pinned migrate deploy"
assert_in_log "mojosec_changes.py run --operation-id" "the trusted-change journal really is active"
assert_in_log "kind mojosec-converge" "the MojoSec step is journaled"
assert_in_log "CMD python3 -E -P -m mojo.deploy.mojosec converge" \
    "the converge child is reached — the wrapper cannot abort first (item 2007)"
assert_order "kind mojosec-converge" "CMD python3 -E -P -m mojo.deploy.mojosec converge" \
    "the journal opens before its converge child"
assert_not_in_log "path /etc/mojosec" \
    "the outer journal declares no MojoSec control state"
assert_lacks "$OUT" "MojoSec control state is never trusted-change scope" \
    "no self-conflicting declaration reaches the validator"
assert_lacks "$OUT" "FATAL: MojoSec deployment failed" "the enrolled deploy does not abort"
assert_in_log "CMD pip install django-mojo==9.9.9" \
    "the pinned install still runs through the pip-run passthrough"
assert_in_log "migrate_locked" "the canary still migrates under the journal"
assert_in_log "MOJOSEC_CWD /" "the converge child keeps its cwd invariant under the wrapper"

echo "post_deploy.sh: an ACTIVE journal contains a broken converge without vetoing the app"
setup_env
echo '{}' > "$TMP/mojosec_etc/config.json"
: > "$TMP/mojosec_lib/mojosec_changes.py"
printf '# active old graph\n' > "$TMP/nginx_etc/django.inc"
cp "$TMP/nginx_etc/django.inc" "$TMP/prior-django.inc"
echo "1" > "$CTL/mojosec.converge.exit"
run_post_deploy --framework 9.9.9 > "$OUT" 2>&1
assert_eq "$?" 0 "a failing converge under the journal does not veto the app deploy"
assert_has "$OUT" "FATAL: MojoSec deployment failed" "the real failure is still named"
assert_has "$OUT" "phase=mojosec; deploy continues" \
    "the contained journal failure is promoted to a deployment warning"
if cmp -s "$PROJ/aws/nginx/django.inc" "$TMP/nginx_etc/django.inc"; then
    ok "journaled rollback restores the new release's django.inc"
else
    fail "journaled rollback lost the new release's django.inc"
fi

echo "post_deploy.sh: auxiliary timer failure warns; web restart and probe still decide"
setup_env
cat > "$STUB/systemctl" <<'EOF'
#!/bin/bash
echo "CMD systemctl $*" >> "$CALLLOG"
case "$1 $2" in
    "is-active --quiet") exit 0 ;;
    "is-enabled --quiet") exit 0 ;;
    "enable --now") [[ "$3" = *.timer ]] && exit 1 ;;
esac
exit 0
EOF
chmod +x "$STUB/systemctl"
run_post_deploy > "$OUT" 2>&1
assert_eq "$?" 0 "timer failure does not veto a healthy app deploy"
assert_has "$OUT" "phase=timers; deploy continues" "timer failure is explicit"
assert_in_log "CMD systemctl restart mojo-asgi" \
    "the web app still restarts after the timer warning"
assert_in_log "CMD curl .*https://127.0.0.1/api/version" \
    "the real app probe still gates the warned deploy"

echo "post_deploy.sh: undeclared collision is inert; declared override applies"
setup_env
echo "* * * * * root $PROJ/custom-jobs.sh # project fork" > "$PROJ/aws/cron.d/3_mojo_jobs"
run_post_deploy > "$OUT" 2>&1
assert_eq "$?" 0 "run with an undeclared collision still exits 0 (framework wins)"
assert_has "$OUT" "collides with a framework template" "undeclared collision logged loudly by render"
assert_lacks "$TMP/cron_etc/3_mojo_jobs" "custom-jobs.sh" "project copy was NOT installed"
assert_has "$TMP/cron_etc/3_mojo_jobs" "jobman start" "framework copy won the collision"
echo "3_mojo_jobs" > "$PROJ/aws/node_overrides.conf"
run_post_deploy > "$OUT" 2>&1
assert_eq "$?" 0 "run with the override declared exits 0"
assert_has "$TMP/cron_etc/3_mojo_jobs" "custom-jobs.sh" "declared override installed the project copy"
assert_has "$OUT" "declared override applied" "override application is logged"

echo "post_deploy.sh: a failed render stops the deploy before /etc is touched"
setup_env
echo "1" > "$CTL/render.exit"
run_post_deploy --framework 9.9.9 >/dev/null 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then ok "failed render exits non-zero"; else fail "failed render exited 0"; fi
assert_no_file "$TMP/nginx_etc/nginx.conf" "nginx.conf never installed after a failed render"
assert_file "$TMP/cron_etc/1mojocron" "no cron sweep ran after a failed render"
assert_not_in_log "CMD systemctl restart mojo-asgi" "no restart after a failed render"

echo "post_deploy.sh: a probe that never answers names the edge catch-all cause"
setup_env
# Stub sleep away: the real probe loop is 15 x 2s, and none of that wait is
# what this asserts.
printf '#!/bin/bash\nexit 0\n' > "$STUB/sleep"; chmod +x "$STUB/sleep"
echo "1" > "$CTL/curl.exit"
run_post_deploy > "$OUT" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then ok "an unanswered probe fails the deploy"; else fail "an unanswered probe exited 0"; fi
assert_has "$OUT" "did not answer" "the probe failure names the URL it tried"
assert_has "$OUT" "returns 444" \
    "the probe failure explains the edge-converged port-80 catch-all"
assert_has "$OUT" "PROBE_URL from the shim" "the probe failure carries the cure"

echo "post_deploy.sh: a failed step aborts before the restart (die-loudly)"
setup_env
echo "1" > "$CTL/pip.exit"
run_post_deploy --framework 9.9.9 >/dev/null 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then ok "failed install exits non-zero"; else fail "failed install exited 0"; fi
assert_not_in_log "systemctl restart mojo-asgi" "no restart after a failed install"

# ── result ───────────────────────────────────────────────────────────────────

echo
echo "test_post_deploy_sh: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
