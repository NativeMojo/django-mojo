"""Structural guard — fileman must not contain Celery code.

The old Celery layer (tasks.py, signals.py) is dead. This test locks it out
so nobody re-introduces a Celery-based pipeline by accident.
"""
import os
import re
from testit import helpers as th
from testit.helpers import assert_true


FILEMAN_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "mojo", "apps", "fileman",
)
FILEMAN_DIR = os.path.abspath(FILEMAN_DIR)


@th.django_unit_test("Structure: mojo/apps/fileman/tasks.py does not exist")
def test_no_tasks_module(opts):
    path = os.path.join(FILEMAN_DIR, "tasks.py")
    assert_true(not os.path.exists(path),
                f"mojo/apps/fileman/tasks.py should not exist; found at {path}")


@th.django_unit_test("Structure: mojo/apps/fileman/signals.py does not exist")
def test_no_signals_module(opts):
    path = os.path.join(FILEMAN_DIR, "signals.py")
    assert_true(not os.path.exists(path),
                f"mojo/apps/fileman/signals.py should not exist; found at {path}")


@th.django_unit_test("Structure: no celery imports anywhere under mojo/apps/fileman")
def test_no_celery_imports(opts):
    pattern = re.compile(r"^\s*(from|import)\s+celery\b", re.MULTILINE)
    offenders = []
    for root, _, files in os.walk(FILEMAN_DIR):
        # Skip bytecode caches.
        if "__pycache__" in root:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            full = os.path.join(root, name)
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            if pattern.search(text):
                offenders.append(full)
    assert_true(not offenders,
                f"celery imports must not appear under mojo/apps/fileman: {offenders}")
