#!/usr/bin/env python3
"""The `python3 -m mojo.deploy` entry: locate packaged scripts, render templates.

    python3 -m mojo.deploy locate update.sh
    python3 -m mojo.deploy locate post_deploy.sh
    python3 -m mojo.deploy render --dest /opt/api/var/deploy \\
        --project-path /opt/api --app-user ec2-user --web-user www --workers 4

`locate` prints the absolute path of a script shipped inside this package, so a
three-line project shim (`aws/update.sh`, `aws/post_deploy.sh`) can exec the
framework copy without knowing where pip put site-packages. The allowlist is
deliberate: locate is an execution oracle for shims running under sudo, and it
answers only for the two names the shim contract defines. Unknown or missing
names are one stderr line and exit 2, never a guess.

`render` materializes the packaged cron/systemd templates into
`<dest>/{cron.d,systemd}` with the node's parameters substituted
(`@PROJ_PATH@`, `@APP_USER@`, `@WEB_USER@`, `@WORKERS@` — plain string
replacement, no template engine), then overlays project extras from
`<project-path>/aws/cron.d/` and `<project-path>/aws/nginx/systemd/`.

COLLISION POLICY — framework fixes propagate by default. A project file whose
name collides with a framework-shipped template is INERT (the framework copy
wins) unless that name is declared in `<project-path>/aws/node_overrides.conf`
(one name per line, `#` comments). An undeclared collision is logged loudly
here and reported by `python3 -m mojo.deploy.check_node`; it never silently
pins a node to a fork.

post_deploy.sh installs /etc copies FROM the rendered dest, and check_node
byte-compares /etc against the same dest — so `<dest>` (by contract
`${PROJ_PATH}/var/deploy`) is the single source of truth for what this node's
last deploy shipped.

Pre-Django by contract, like every module in this package: imports argparse /
os / sys only. See mojo/deploy/__init__.py.
"""

import argparse
import os
import sys

# The only names locate will resolve. Everything else exits 2 — see above.
LOCATABLE = ("update.sh", "post_deploy.sh")

PLACEHOLDER_FLAGS = (
    ("@PROJ_PATH@", "project_path"),
    ("@APP_USER@", "app_user"),
    ("@WEB_USER@", "web_user"),
    ("@WORKERS@", "workers"),
)

# (dest subdir == template subdir, project overlay dir relative to project path)
TEMPLATE_SETS = (
    ("cron.d", os.path.join("aws", "cron.d")),
    ("systemd", os.path.join("aws", "nginx", "systemd")),
)

OVERRIDES_NAME = os.path.join("aws", "node_overrides.conf")


def package_dir():
    return os.path.dirname(os.path.abspath(__file__))


def template_dir(subdir):
    return os.path.join(package_dir(), "templates", subdir)


def scripts_dir():
    return os.path.join(package_dir(), "scripts")


def build_context(project_path, app_user, web_user, workers):
    return {
        "@PROJ_PATH@": project_path.rstrip("/") or "/",
        "@APP_USER@": app_user,
        "@WEB_USER@": web_user,
        "@WORKERS@": str(workers),
    }


def substitute(text, context):
    for placeholder, value in context.items():
        text = text.replace(placeholder, value)
    return text


def unresolved_placeholders(text):
    """Every `@TOKEN@` (TOKEN all caps/underscore) still present after
    substitution. A leftover means a template grew a placeholder this render
    does not know — installing it would put the literal token into /etc, so
    render refuses instead."""
    found = set()
    start = text.find("@")
    while start != -1:
        end = text.find("@", start + 1)
        if end == -1:
            break
        token = text[start + 1:end]
        if token and all(ch.isupper() or ch == "_" for ch in token):
            found.add("@%s@" % token)
            start = text.find("@", end + 1)
        else:
            start = end
    return sorted(found)


def list_files(directory):
    """Regular, non-hidden files in a directory; [] when it is absent."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [n for n in names
            if not n.startswith(".")
            and os.path.isfile(os.path.join(directory, n))]


def read_overrides(project_path):
    """Names declared override-able in <project>/aws/node_overrides.conf —
    one name per line, `#` comments. Absent file = nothing declared."""
    path = os.path.join(project_path, OVERRIDES_NAME)
    names = set()
    try:
        with open(path) as handle:
            for line in handle:
                line = line.split("#", 1)[0].strip()
                if line:
                    names.add(line)
    except OSError:
        pass
    return names


def write_rendered(dest_path, text):
    with open(dest_path, "w") as handle:
        handle.write(text)
    os.chmod(dest_path, 0o644)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_locate(args):
    if args.name not in LOCATABLE:
        print("mojo.deploy locate: unknown script %r (serves: %s)"
              % (args.name, ", ".join(LOCATABLE)), file=sys.stderr)
        return 2
    path = os.path.join(scripts_dir(), args.name)
    if not os.path.isfile(path):
        print("mojo.deploy locate: packaged script missing: %s" % path,
              file=sys.stderr)
        return 2
    print(path)
    return 0


def cmd_render(args):
    # This is intentionally inside the command.  A running old post_deploy
    # shell reaches the newly installed module through this unchanged argv,
    # which makes the first framework upgrade self-healing.
    project = args.project_path.rstrip("/") or "/"
    canonical_dest = os.path.join(project, "var", "deploy")
    # Avoid importing tempfile on the pre-Django production path: test seams
    # are accepted only for a project explicitly rooted beneath the process's
    # temporary directory (the shell/unit harness shape), never a deployed tree.
    requested_test_seams = os.environ.get("MOJO_DEPLOY_TEST_SEAMS") == "1"
    real_project = os.path.realpath(project)
    temp_root = os.path.realpath(os.environ.get("TMPDIR") or "/tmp")
    try:
        beneath_temp = os.path.commonpath((real_project, temp_root)) == temp_root
    except ValueError:
        beneath_temp = False
    test_seams = requested_test_seams and beneath_temp
    host_deploy = (os.path.realpath(os.getcwd()) == os.path.realpath(project)
                   or test_seams)
    if (os.geteuid() == 0 and host_deploy and
            os.path.normpath(args.dest) == canonical_dest):
        from mojo.deploy.nginx_runtime import (
            NginxRuntimeError, converge, fragment_path)
        nginx_etc = os.environ.get("NGINX_ETC", "/etc/nginx")
        runtime_root = os.environ.get(
            "MOJO_NGINX_RUNTIME_ROOT", "/var/lib/django-mojo/nginx")
        # Alternate roots are test-only seams. A real host deploy may not
        # redirect privileged persistent state through its environment.
        if not test_seams and (
                nginx_etc != "/etc/nginx" or
                runtime_root != "/var/lib/django-mojo/nginx"):
            print("mojo.deploy render: refusing redirected nginx runtime "
                  "paths for a production project", file=sys.stderr)
            return 1

        def converge_runtime():
            converge(args.web_user, nginx_etc=nginx_etc, root=runtime_root)

        # Journal the runtime fragment write when a sensor is enrolled so the
        # rendered host config is provenance-explained instead of an
        # unexplained protected change. Annotation is best-effort: a journal
        # that cannot BEGIN never blocks the serving-plane converge, and a
        # journal failure AFTER the mutation never re-runs it.
        journal = None
        if not test_seams and os.path.exists("/etc/mojosec/config.json"):
            from mojo.deploy.mojosec_changes import ChangeError, ChangeJournal
            journal = ChangeJournal()
            try:
                journal.begin(
                    "nginx-runtime-fragment", "rendered-host-config",
                    [fragment_path(nginx_etc)],
                    deployment_id=os.environ.get("MOJO_DEPLOY_ID") or None)
            except (ChangeError, OSError, ValueError) as err:
                print("mojo.deploy render: trusted-change journal "
                      "unavailable (%s); converging unjournaled" % err,
                      file=sys.stderr)
                journal = None
        try:
            converge_runtime()
        except NginxRuntimeError as err:
            if journal is not None:
                from mojo.deploy.mojosec_changes import ChangeError
                try:
                    journal.abort("nginx-runtime-fragment")
                except (ChangeError, OSError, ValueError):
                    pass
            print("mojo.deploy render: nginx runtime convergence failed: %s"
                  % err, file=sys.stderr)
            return 1
        if journal is not None:
            from mojo.deploy.mojosec_changes import ChangeError
            try:
                journal.complete("nginx-runtime-fragment")
            except (ChangeError, OSError, ValueError) as err:
                print("mojo.deploy render: trusted-change completion failed "
                      "(%s); the fragment install stays unannotated" % err,
                      file=sys.stderr)

    context = build_context(args.project_path, args.app_user,
                            args.web_user, args.workers)
    overrides = read_overrides(project)
    rendered = 0
    overlaid = 0

    for subdir, overlay_rel in TEMPLATE_SETS:
        src_dir = template_dir(subdir)
        names = list_files(src_dir)
        if not names:
            # A package with no templates is a broken install, not a quiet
            # no-op — post_deploy would converge an empty set into /etc.
            print("mojo.deploy render: no templates under %s — broken or "
                  "partial django-mojo install" % src_dir, file=sys.stderr)
            return 1

        dest_dir = os.path.join(args.dest, subdir)
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as err:
            print("mojo.deploy render: cannot create %s: %s" % (dest_dir, err),
                  file=sys.stderr)
            return 1

        # Framework templates first: substitute, refuse leftovers, write 0644.
        for name in names:
            with open(os.path.join(src_dir, name)) as handle:
                text = substitute(handle.read(), context)
            leftover = unresolved_placeholders(text)
            if leftover:
                print("mojo.deploy render: %s/%s still contains %s after "
                      "substitution — refusing to install a literal "
                      "placeholder" % (subdir, name, ", ".join(leftover)),
                      file=sys.stderr)
                return 1
            try:
                write_rendered(os.path.join(dest_dir, name), text)
            except OSError as err:
                print("mojo.deploy render: cannot write %s/%s: %s"
                      % (dest_dir, name, err), file=sys.stderr)
                return 1
            rendered += 1

        # Project overlay: extras copy through verbatim; a name collision is
        # inert unless declared in aws/node_overrides.conf (see module doc).
        overlay_dir = os.path.join(project, overlay_rel)
        for name in list_files(overlay_dir):
            collides = name in names
            if collides and name not in overrides:
                print("mojo.deploy render: WARNING: project file %s/%s "
                      "collides with a framework template and is NOT declared "
                      "in aws/node_overrides.conf — framework copy wins, "
                      "project copy ignored" % (overlay_rel, name),
                      file=sys.stderr)
                continue
            with open(os.path.join(overlay_dir, name)) as handle:
                text = handle.read()
            try:
                write_rendered(os.path.join(dest_dir, name), text)
            except OSError as err:
                print("mojo.deploy render: cannot write %s/%s: %s"
                      % (dest_dir, name, err), file=sys.stderr)
                return 1
            if collides:
                print("mojo.deploy render: declared override applied: %s/%s "
                      "replaces the framework template" % (overlay_rel, name))
            overlaid += 1

    print("mojo.deploy render: %d framework template(s) + %d project file(s) "
          "-> %s" % (rendered, overlaid, args.dest))
    return 0


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def main(argv):
    parser = argparse.ArgumentParser(
        prog="python3 -m mojo.deploy",
        description="Locate packaged node scripts and render node templates")
    sub = parser.add_subparsers(dest="command", required=True)

    locate = sub.add_parser(
        "locate", help="print the absolute path of a packaged script")
    locate.add_argument("name",
                        help="one of: %s" % ", ".join(LOCATABLE))

    render = sub.add_parser(
        "render",
        help="render cron/systemd templates into <dest>/{cron.d,systemd}")
    render.add_argument("--dest", required=True,
                        help="output directory (by contract: "
                             "${PROJ_PATH}/var/deploy)")
    render.add_argument("--project-path", default="/opt/api",
                        help="substituted for @PROJ_PATH@ and the root the "
                             "aws/ overlays are read from (default: /opt/api)")
    render.add_argument("--app-user", default="ec2-user",
                        help="substituted for @APP_USER@ (default: ec2-user)")
    render.add_argument("--web-user", default="www",
                        help="substituted for @WEB_USER@ (default: www)")
    render.add_argument("--workers", default="4",
                        help="substituted for @WORKERS@ (default: 4)")

    args = parser.parse_args(argv)
    if args.command == "locate":
        return cmd_locate(args)
    return cmd_render(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
