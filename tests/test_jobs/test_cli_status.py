"""Status output must distinguish CLI daemons from deployed jobman workers."""

import contextlib
import io
import tempfile

from testit import helpers as th


@th.django_unit_test()
def test_empty_status_names_the_daemon_process_plane(opts):
    from mojo.apps.jobs import cli

    output = io.StringIO()
    with tempfile.TemporaryDirectory() as pid_root:
        with contextlib.redirect_stdout(output):
            running = cli.status_command(pid_root=pid_root)

    th.assert_true(not running, "an empty daemon pid directory reported a process")
    th.assert_eq(
        output.getvalue().strip(),
        "No jobs CLI daemon-mode processes running; check deployed foreground "
        "processes with: python3 -m mojo.deploy.jobman status",
        "empty CLI status implied that deployed jobman workers were stopped",
    )
