"""Shared helpers for the RestMeta CAN_* feature-flag tests.

Leading underscore: testit's collector skips `_`-prefixed files, so nothing in
here is picked up as a test module.

WHY THIS EXISTS
---------------
The CAN_* flags live on a model's `RestMeta` *class*, so `setattr(Model.RestMeta,
"CAN_DELETE", False)` mutates process-global state. testit runs test modules as
parallel threads in one process, so any other module that reads that flag while
the window is open sees the flipped value and fails for a reason unrelated to
what it is testing.

That is not hypothetical. These tests used to borrow `incident.RuleSet`, which
declares `CAN_DELETE = True` natively (mojo/apps/incident/models/rule.py). The
old cleanup helper *deleted* the flags instead of restoring them, so after the
first test ran, `RuleSet.RestMeta` had no `CAN_DELETE` at all — permanently, for
the rest of the process. `get_rest_meta_prop("CAN_DELETE", False)` then defaults
to False, and `tests/test_assistant/23_test_delete_tools.py` (which reads it via
the assistant's delete tool) failed with "Deletion is not allowed on
incident.RuleSet". `-t test_models -t test_assistant` reproduced it every time.

Two tools to keep that from coming back:

`FlagProbe` — a throwaway model that nothing else in the suite reads. Prefer it.
Every CAN_* gate raises before touching the database, so a probe needs no table
and no migration. Tests that only assert "the gate refuses" should use this and
mutate nothing shared.

`flag_override` — for the handful of tests that need a real, persisted row and
therefore must use a real model. It snapshots the exact attribute state
(including *absent*) and restores it precisely, so it can never delete a flag the
model declares for itself. It still leaves a window open to other threads, so it
is the second choice, not the default.
"""
from contextlib import contextmanager

from mojo.models import MojoModel


_MISSING = object()

#: Pass as a flag value to `flag_override` to make the attribute ABSENT for the
#: duration of the block — for tests that assert the framework's default when a
#: model declares no flag at all. Restored afterwards like any other value.
REMOVE = object()


class FlagProbe(MojoModel):
    """A model-shaped target for CAN_* gate tests that owns its own RestMeta.

    Deliberately NOT a django models.Model: every CAN_* gate in
    mojo/models/rest.py raises as its first statement, before any database
    access, so the gate can be exercised with no table behind it. Instances are
    only ever passed to handlers that reject before reading them.

    Because no production code refers to this class, flipping flags on it is
    invisible to every other test module — which is the entire point.
    """

    class RestMeta:
        VIEW_PERMS = ["view_security", "security"]
        CREATE_PERMS = ["manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        DELETE_PERMS = ["manage_security", "security"]


@contextmanager
def flag_override(model, **flags):
    """Set RestMeta flags on `model` for the duration of the block, then put the
    attributes back exactly as they were — restoring a prior value, and removing
    only what was genuinely absent to begin with.

    Use ONLY when the test needs a real persisted instance. Prefer `FlagProbe`,
    which mutates nothing shared. Never use this for CAN_DELETE on a real model:
    the assistant's delete tool reads that flag and will fail while the window is
    open.
    """
    saved = {}
    for name in flags:
        saved[name] = getattr(model.RestMeta, name, _MISSING)
    try:
        for name, value in flags.items():
            if value is REMOVE:
                if hasattr(model.RestMeta, name):
                    delattr(model.RestMeta, name)
            else:
                setattr(model.RestMeta, name, value)
        yield model
    finally:
        for name, previous in saved.items():
            if previous is _MISSING:
                if hasattr(model.RestMeta, name):
                    delattr(model.RestMeta, name)
            else:
                setattr(model.RestMeta, name, previous)
