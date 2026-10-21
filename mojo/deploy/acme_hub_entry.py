"""Fixed argv selection before any root import of candidate-installed Mojo.

The root probe is deliberately stdlib-only and runs with -I -S. It supplies
no operator command, path or environment override. The enrolled entry and its
manifest are installed separately by the qualified one-time operator cutover.
"""
import os

ROOT_GATE = '''import os,stat,sys
def absent(path):
    try:
        value=os.lstat(path)
    except FileNotFoundError:
        return True
    if value.st_uid!=0 or value.st_mode & 0o022:
        raise ValueError("unsafe root entry")
    if not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):
        raise ValueError("unsafe root entry")
    return False
try:
    for parent in ("/var", "/var/lib", "/usr", "/usr/local", "/usr/local/libexec"):
        if absent(parent):
            if parent!="/usr/local/libexec":
                raise ValueError("root state unavailable")
    marker=not absent("/usr/local/libexec/mojo-acme-hub-enrollment.json")
    state=not absent("/var/lib/django-mojo-deploy")
    floor=state and not absent("/var/lib/django-mojo-deploy/acme-hub-floor.json")
    if marker or floor:
        os.execv("/usr/local/libexec/mojo-acme-hub-upgrade", ["mojo-acme-hub-upgrade", "--launch"]+sys.argv[1:])
    os.execv("/bin/bash", ["bash", "-c", 'exec bash "$(python3 -m mojo.deploy locate update.sh)" --parent-status "$@"', "django-mojo-update"]+sys.argv[1:])
except (OSError,ValueError):
    sys.stderr.write("shared ACME entry refused: evidence_unavailable\\n")
    sys.exit(1)
'''


def default_argv():
    return ["sudo", "-n", "/usr/bin/python3", "-I", "-S", "-c", ROOT_GATE]


def required():
    # Public metadata only. Denial is not absence: the root data probe must
    # make the final decision, never a caller-configured alternate command.
    for path in ("/usr/local/libexec/mojo-acme-hub-enrollment.json",
                 "/var/lib/django-mojo-deploy/acme-hub-floor.json"):
        try:
            os.lstat(path)
            return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False
