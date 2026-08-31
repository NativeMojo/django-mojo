#!/usr/bin/env python3
"""Small, pre-Django deployment helpers.

``locate`` is the permanent settings-free endpoint for packaged deployment
scripts. ``export-scripts`` is an optional debugging/customization aid.
``render`` does plain placeholder substitution for cron and systemd files.
``app-user`` resolves the node's non-root application account for shell
callers (update.sh). None imports Django or changes privileged host state.
"""

import argparse
import os
import shutil
import sys

from mojo.deploy import app_user


SCRIPT_NAMES = ("update.sh", "post_deploy.sh")
TEMPLATE_SETS = (
    ("cron.d", os.path.join("aws", "cron.d")),
    ("systemd", os.path.join("aws", "nginx", "systemd")),
)
OVERRIDES_NAME = os.path.join("aws", "node_overrides.conf")


def package_dir():
    return os.path.dirname(os.path.abspath(__file__))


def project_scripts_dir():
    return os.path.join(package_dir(), "project_scripts")


def template_dir(subdir):
    return os.path.join(package_dir(), "templates", subdir)


def build_context(project_path, app_user, web_user, workers):
    return {
        "@PROJ_PATH@": project_path.rstrip("/") or "/",
        "@APP_USER@": app_user,
        "@WEB_USER@": web_user,
        "@WORKERS@": str(workers),
    }


def substitute(value, context):
    for placeholder, replacement in context.items():
        value = value.replace(placeholder, replacement)
    return value


def unresolved_placeholders(value):
    found = set()
    start = value.find("@")
    while start != -1:
        end = value.find("@", start + 1)
        if end == -1:
            break
        token = value[start + 1:end]
        if token and all(char.isupper() or char == "_" for char in token):
            found.add("@%s@" % token)
        start = value.find("@", end + 1)
    return sorted(found)


def list_files(directory):
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [name for name in names
            if not name.startswith(".")
            and os.path.isfile(os.path.join(directory, name))]


def read_overrides(project_path):
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


def write_rendered(path, value):
    with open(path, "w") as handle:
        handle.write(value)
    os.chmod(path, 0o644)


def cmd_export_scripts(args):
    try:
        os.makedirs(args.dest, exist_ok=True)
        for name in SCRIPT_NAMES:
            source = os.path.join(project_scripts_dir(), name)
            destination = os.path.join(args.dest, name)
            if not os.path.isfile(source):
                raise OSError("packaged project script is missing: %s" % source)
            if os.path.exists(destination) and not args.force:
                raise OSError("destination exists (use --force): %s" % destination)
            temporary = destination + ".tmp.%s" % os.getpid()
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o755)
            os.replace(temporary, destination)
    except OSError as err:
        print("mojo.deploy export-scripts: %s" % err, file=sys.stderr)
        return 1
    print("mojo.deploy export-scripts: wrote %s to %s"
          % (", ".join(SCRIPT_NAMES), args.dest))
    return 0


def cmd_locate(args):
    """Resolve one allowlisted packaged script for a permanent small shim."""
    if args.name not in SCRIPT_NAMES:
        print("mojo.deploy locate: unknown project script", file=sys.stderr)
        return 2
    path = os.path.join(project_scripts_dir(), args.name)
    if not os.path.isfile(path):
        print("mojo.deploy locate: packaged project script missing",
              file=sys.stderr)
        return 2
    print(path)
    return 0


def cmd_app_user(args):
    """Print the resolved application account, or fail closed (item #3429).

    The candidate is the caller's $APP_USER value; the resolver ranks the
    deployed cron entry above it and the checkout owner below it, and never
    consults $SUDO_USER. An empty answer is exit 1 with nothing on stdout, so
    a shell caller can `APP_USER="$(...)" || die`.
    """
    name = app_user.resolve_app_user(args.root, candidate=args.candidate or None)
    if not name:
        print("mojo.deploy app-user: no non-root application account resolves",
              file=sys.stderr)
        return 1
    print(name)
    return 0


def cmd_render(args):
    # Syntax-only: render legitimately runs on a machine without the target
    # node's accounts, but stamping root (or an option-shaped string) into
    # /etc/cron.d would hand the job engine to root on every future tick —
    # the persistence half of the #3429 poisoning.
    if not app_user.valid_app_user_name(args.app_user):
        print("mojo.deploy render: --app-user must be a plain non-root "
              "account name", file=sys.stderr)
        return 1
    project = args.project_path.rstrip("/") or "/"
    context = build_context(project, args.app_user, args.web_user, args.workers)
    overrides = read_overrides(project)
    produced = []
    rendered = 0
    overlaid = 0

    for subdir, overlay_rel in TEMPLATE_SETS:
        source_dir = template_dir(subdir)
        names = list_files(source_dir)
        if not names:
            print("mojo.deploy render: no templates under %s" % source_dir,
                  file=sys.stderr)
            return 1
        destination_dir = os.path.join(args.dest, subdir)
        try:
            os.makedirs(destination_dir, exist_ok=True)
        except OSError as err:
            print("mojo.deploy render: cannot create %s: %s"
                  % (destination_dir, err), file=sys.stderr)
            return 1
        written = set()
        for name in names:
            with open(os.path.join(source_dir, name)) as handle:
                value = substitute(handle.read(), context)
            leftovers = unresolved_placeholders(value)
            if leftovers:
                print("mojo.deploy render: %s/%s still contains %s"
                      % (subdir, name, ", ".join(leftovers)), file=sys.stderr)
                return 1
            try:
                write_rendered(os.path.join(destination_dir, name), value)
            except OSError as err:
                print("mojo.deploy render: cannot write %s/%s: %s"
                      % (destination_dir, name, err), file=sys.stderr)
                return 1
            written.add(name)
            rendered += 1

        overlay_dir = os.path.join(project, overlay_rel)
        for name in list_files(overlay_dir):
            collides = name in names
            if collides and name not in overrides:
                print("mojo.deploy render: WARNING: project file %s/%s "
                      "collides with a framework template; framework copy wins"
                      % (overlay_rel, name), file=sys.stderr)
                continue
            with open(os.path.join(overlay_dir, name)) as handle:
                value = handle.read()
            try:
                write_rendered(os.path.join(destination_dir, name), value)
            except OSError as err:
                print("mojo.deploy render: cannot write %s/%s: %s"
                      % (destination_dir, name, err), file=sys.stderr)
                return 1
            written.add(name)
            overlaid += 1
        produced.append((subdir, destination_dir, written))

    for subdir, destination_dir, written in produced:
        for name in list_files(destination_dir):
            path = os.path.join(destination_dir, name)
            if name in written or os.path.islink(path):
                continue
            try:
                os.unlink(path)
            except OSError as err:
                print("mojo.deploy render: cannot remove stale %s/%s: %s"
                      % (subdir, name, err), file=sys.stderr)
                return 1

    # The jobman tick decides who owns the job engine (it is rung 1 of the
    # app-user resolution ladder), so validate the PRODUCED file — a project
    # overlay named in node_overrides.conf is copied verbatim and would
    # otherwise bypass the --app-user check above. Other cron files may
    # legitimately run as root (certbot); only this one is constrained.
    for subdir, destination_dir, written in produced:
        if subdir != "cron.d" or app_user.CRON_NAME not in written:
            continue
        entry = app_user.cron_app_user(
            os.path.join(destination_dir, app_user.CRON_NAME))
        if not app_user.valid_app_user_name(entry or ""):
            print("mojo.deploy render: %s names an unusable job account: %r"
                  % (app_user.CRON_NAME, entry), file=sys.stderr)
            return 1

    print("mojo.deploy render: %d framework template(s) + %d project file(s) -> %s"
          % (rendered, overlaid, args.dest))
    return 0


def main(argv):
    parser = argparse.ArgumentParser(prog="python3 -m mojo.deploy")
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser(
        "export-scripts", help="copy stable deploy scripts into a project")
    export.add_argument("--dest", required=True)
    export.add_argument("--force", action="store_true")

    locate = commands.add_parser(
        "locate", help="resolve a packaged deployment script")
    locate.add_argument("name")

    render = commands.add_parser("render", help="render cron/systemd templates")
    render.add_argument("--dest", required=True)
    render.add_argument("--project-path", default="/opt/api")
    render.add_argument("--app-user", default="ec2-user")
    render.add_argument("--web-user", default="www")
    render.add_argument("--workers", default="4")

    account = commands.add_parser(
        "app-user", help="resolve the node's non-root application account")
    account.add_argument("--root", required=True,
                         help="the project checkout (its owner is the final "
                              "rung of the resolution ladder)")
    account.add_argument("--candidate", default="",
                         help="the caller's $APP_USER value, if any")

    args = parser.parse_args(argv)
    if args.command == "export-scripts":
        return cmd_export_scripts(args)
    if args.command == "locate":
        return cmd_locate(args)
    if args.command == "app-user":
        return cmd_app_user(args)
    return cmd_render(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
