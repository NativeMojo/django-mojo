"""Fileman stores derived from the system default manager (#7586, #7990).

Serial for execution reasons, not state ones (maestro #7538): these tests add
and remove a competing system default (user=None, group=None,
is_default=True), the row get_for_user/get_for_group derive every store
from. Any package running beside them can see that row come and go —
test_filevault's setup adopts the first system default and demotes the
others, so a parallel run could leave no default at all. Serial packages run
after the parallel ring, alone. Nothing is patched and no settings change.
"""
TESTIT = {
    "default_core": True,  # framework bucket; the control test is tagged core
    "serial": True,
}
