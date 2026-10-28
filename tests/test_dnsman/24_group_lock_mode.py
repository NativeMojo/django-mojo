"""Maestro item #9740 — dnsman locks Group rows through group_hierarchy.lock_rows.

A plain `select_for_update()` on an account.Group row is FOR UPDATE, which
waits behind, and is passed by, the FOR KEY SHARE every child-row insert
takes on that group (see #9734 and tests/test_account/group_lock_mode.py for
the behaviour). dnsman's delegation and recipient-key writers must take the
NO KEY lock the helper provides. This guard reads the source so the rule
cannot regress silently.
"""
import ast
import os

from testit import helpers as th

SERVICES = ("recipient_keys.py", "delegation.py", "delegation_setup.py")


def _dnsman_root():
    import mojo.apps.dnsman as dnsman
    return os.path.dirname(dnsman.__file__)


def _chain(node):
    """Dotted names of an attribute/call chain, outermost last."""
    names = []
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Name):
            names.append(node.id)
            return list(reversed(names))
        else:
            return list(reversed(names))


def _group_for_update_calls(path):
    tree = ast.parse(open(path).read(), filename=path)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        names = _chain(node)
        if "select_for_update" in names and "Group" in names and "objects" in names:
            hits.append(f"{os.path.relpath(path, _dnsman_root())}:{node.lineno}")
    return sorted(set(hits))


def _lock_rows_calls(path):
    tree = ast.parse(open(path).read(), filename=path)
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, ast.Call) and _chain(node)[-2:] == ["group_hierarchy", "lock_rows"])


@th.django_unit_test("#9740: no dnsman code takes a plain select_for_update() on a Group row")
def test_no_plain_group_select_for_update_in_dnsman(opts):
    hits = []
    for folder, _dirs, files in os.walk(_dnsman_root()):
        for name in files:
            if name.endswith(".py"):
                hits.extend(_group_for_update_calls(os.path.join(folder, name)))
    assert not hits, \
        f"Group rows must be locked through group_hierarchy.lock_rows (NO KEY), not select_for_update(): {hits}"


@th.django_unit_test("#9740: the three dnsman services lock Group rows through group_hierarchy.lock_rows")
def test_dnsman_services_use_lock_rows(opts):
    services = os.path.join(_dnsman_root(), "services")
    missing = [name for name in SERVICES
               if _lock_rows_calls(os.path.join(services, name)) == 0]
    assert not missing, \
        f"these dnsman services should take their Group row lock via group_hierarchy.lock_rows: {missing}"
