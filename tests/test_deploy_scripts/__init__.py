# The packaged node-script shell harnesses (maestro #2789): each test spawns a
# real bash harness that execs the packaged mojo/deploy scripts ~40-60 times
# with every external command stubbed — ~100s of wall clock in one file, and
# the single largest module in the old default tier. This is end-to-end
# coverage of packaged shell scripts, not a framework contract, so it is
# opt-in (`--extra slow` / `--all`). The cheap `bash -n` wheel-integrity gate
# stayed behind in tests/test_deploy/scripts_parse.py. Not serial: every
# harness runs in its own mktemp PROJ_PATH.
TESTIT = {
    "requires_extra": ["slow"],
}
