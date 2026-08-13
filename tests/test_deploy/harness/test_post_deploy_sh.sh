#!/bin/bash
# Harness for mojo/deploy/scripts/post_deploy.sh (maestro item 1612; harness
# ported from mverify_api #1582 / django-mojo-skeleton #1572, re-fixtured for
# the packaged render/var-deploy pipeline).
#
# Runs the REAL packaged script against a throwaway PROJ_PATH with the
# NGINX_ETC / SYSTEMD_ETC / CRON_ETC seams pointed into the temp dir and every
# external command stubbed on PATH — EXCEPT `python3 -m mojo.deploy*`, which
# the python3 stub passes through to the real interpreter with
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

setup_env() {
    rm -rf "$PROJ" "$STUB" "$CTL" "$TMP/nginx_etc" "$TMP/systemd_etc" "$TMP/cron_etc" "$TMP/logrotate_etc"
    mkdir -p "$PROJ/var/logs" "$PROJ/bin" "$PROJ/aws/nginx/systemd" "$PROJ/aws/nginx/sec.d" \
             "$PROJ/aws/nginx/conf.d" "$PROJ/aws/cron.d" \
             "$STUB" "$CTL" "$TMP/nginx_etc" "$TMP/systemd_etc" "$TMP/cron_etc" "$TMP/logrotate_etc"
    : > "$CALLLOG"

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
ctl="\$STUBCTL/$cmd.exit"
[ -f "\$ctl" ] && exit "\$(cat "\$ctl")"
exit 0
EOF
        chmod +x "$STUB/$cmd"
    done
    cat > "$STUB/systemctl" <<'EOF'
#!/bin/bash
echo "CMD systemctl $*" >> "$CALLLOG"
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
echo 0
EOF
    chmod +x "$STUB/stat"

    # python3: `-m mojo.deploy*` passes through to the REAL interpreter with
    # PYTHONPATH=$REPO (render.exit forces the render step to fail instead);
    # everything else (manage.py) is argv-logged and scripted.
    cat > "$STUB/python3" <<EOF
#!/bin/bash
echo "CMD python3 \$*" >> "\$CALLLOG"
case "\$*" in
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
        [ -f "\$STUBCTL/audit.flush.exit" ] && exit "\$(cat "\$STUBCTL/audit.flush.exit")"
        exit 0
        ;;
    *"mojosec_audit.py restore"*)
        [ -f "\$STUBCTL/audit.restore.exit" ] && exit "\$(cat "\$STUBCTL/audit.restore.exit")"
        exit 0
        ;;
    "-m mojo.deploy"*)
        if [ -f "\$STUBCTL/render.exit" ]; then exit "\$(cat "\$STUBCTL/render.exit")"; fi
        exec env -u DJANGO_SETTINGS_MODULE PYTHONPATH="$REPO" "$REAL_PYTHON3" "\$@"
        ;;
esac
ctl="\$STUBCTL/python3.exit"
[ -f "\$ctl" ] && exit "\$(cat "\$ctl")"
exit 0
EOF
    chmod +x "$STUB/python3"
}

run_post_deploy() { # args...
    ( cd "$TMP" && PROJ_PATH="$PROJ" NGINX_ETC="$TMP/nginx_etc" \
        SYSTEMD_ETC="$TMP/systemd_etc" CRON_ETC="$TMP/cron_etc" \
        LOGROTATE_ETC="$TMP/logrotate_etc" PATH="$STUB:$PATH" \
        MOJOSEC_PYTHON=python3 \
        bash "$PROJ/aws/post_deploy.sh" "$@" )
}

run_post_deploy_env() { # VAR=val ... -- args...
    local envs=()
    while [ "$1" != "--" ]; do envs+=("$1"); shift; done
    shift
    ( cd "$TMP" && env "${envs[@]}" PROJ_PATH="$PROJ" NGINX_ETC="$TMP/nginx_etc" \
        SYSTEMD_ETC="$TMP/systemd_etc" CRON_ETC="$TMP/cron_etc" \
        LOGROTATE_ETC="$TMP/logrotate_etc" PATH="$STUB:$PATH" \
        MOJOSEC_PYTHON=python3 \
        bash "$PROJ/aws/post_deploy.sh" "$@" )
}

# ── tests ────────────────────────────────────────────────────────────────────

echo "post_deploy.sh: --framework pins the install; bare upgrades; deps come FIRST"
setup_env
run_post_deploy --framework 9.9.9 >/dev/null 2>&1
assert_eq "$?" 0 "--framework run exits 0"
assert_in_log "CMD pip install -r $PROJ/requirements.txt" \
    "requirements install uses an absolute path that survives the trusted helper cwd"
assert_in_log "CMD pip install django-mojo==9.9.9" "pinned install argv"
assert_order "CMD pip install -r" "CMD pip install django-mojo==9.9.9" \
    "requirements install precedes the framework pin"
setup_env
run_post_deploy >/dev/null 2>&1
assert_eq "$?" 0 "bare run exits 0"
assert_in_log "CMD pip install --upgrade django-mojo" "bare run upgrades to latest"
assert_order "CMD pip install -r" "CMD pip install --upgrade django-mojo" \
    "requirements install precedes the latest upgrade"
assert_order "CMD pip install --upgrade django-mojo" "CMD python3 -m mojo.deploy render" \
    "render runs AFTER the framework install (renders the just-installed templates)"

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
assert_in_log "CMD curl .*http://127.0.0.1/api/version" \
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
assert_not_in_log "CMD curl .*http://127.0.0.1/api/version " \
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

echo "post_deploy.sh: MojoSec absence is distinct from a converge safety failure"
setup_env
printf '# old graph\n# MojoSec exact receiver cap\ninclude /etc/nginx/snippets/mojosec_receiver.conf;\n' \
    > "$TMP/nginx_etc/django.inc"
cp "$TMP/nginx_etc/django.inc" "$TMP/prior-django.inc"
echo "1" > "$CTL/mojosec.converge.exit"
run_post_deploy_env MOJOSEC_MODE="off" MOJOSEC_DEPLOY_CRITICALITY="required" -- \
    > "$OUT" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then ok "generic MojoSec converge failure exits non-zero"; else fail "generic MojoSec failure was masked"; fi
if cmp -s "$TMP/prior-django.inc" "$TMP/nginx_etc/django.inc"; then
    ok "generic failure restores exact pre-deploy django.inc bytes"
else
    fail "generic failure did not restore exact pre-deploy django.inc"
fi
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
    rc=$?
    if [ "$rc" -ne 0 ]; then ok "old converge failure exits non-zero ($prior)"; \
    else fail "old converge failure was masked ($prior)"; fi
    if [ "$prior" = "active" ]; then
        [ ! -f "$CTL/mojosec.inactive" ] && ok "old converge failure restores active" || \
            fail "old converge failure stranded active service stopped"
    else
        [ -f "$CTL/mojosec.inactive" ] && ok "old converge failure preserves inactive" || \
            fail "old converge failure started an originally inactive service"
    fi
done

setup_env
helper="$TMP/mojosec_audit.py"; state="$TMP/audit-state.json"
: > "$helper"; : > "$state"
echo "3" > "$CTL/mojosec.preflight.exit"
run_post_deploy_env MOJOSEC_MODE="observe" MOJOSEC_DEPLOY_CRITICALITY="required" \
    MOJOSEC_AUDIT_HELPER="$helper" MOJOSEC_AUDIT_STATE="$state" \
    MOJOSEC_AUDIT_PYTHON=python3 -- > "$OUT" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then ok "module-absent terminal failure exits non-zero"; \
else fail "module-absent observe failure was masked"; fi
[ ! -f "$CTL/mojosec.inactive" ] && ok "module-absent failure restores active" || \
    fail "module-absent failure stranded active service stopped"

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
if cmp -s "$TMP/prior-django.inc" "$TMP/nginx_etc/django.inc"; then
    ok "failed downgrade cleanup restores exact prior django.inc"
else
    fail "failed downgrade cleanup lost prior django.inc"
fi

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
