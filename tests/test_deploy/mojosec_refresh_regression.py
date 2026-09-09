"""An installed candidate must refresh the active stale sensor before activation."""

import os
import tempfile

from testit import helpers as th


@th.django_unit_test()
def test_active_stale_sensor_refreshes_before_candidate_activation(opts):
    from mojo.deploy.mojosec_refresh import Refresh

    events = []

    class Host:
        boot_id = "test-boot"
        generation = 100

        def version(self, timeout):
            return "2.0"

        def observe(self, timeout):
            return {"active": True, "generation": ["test-boot", 42, self.generation],
                    "proved": self.generation == 200, "framework_version":
                    "2.0" if self.generation == 200 else "1.0"}

        def jobs(self, timeout):
            return []

        def restart(self, timeout):
            events.append("refresh")
            self.generation = 200

        def cancel(self, jobs, timeout):
            events.append("cancel")

    with tempfile.TemporaryDirectory() as root:
        result = Refresh(os.path.join(root, "evidence.json"), host=Host(),
                         owner_uid=os.getuid()).run("deploy-1", "candidate", "2.0")
        events.append("activate")
        assert result["outcome"] == "refreshed", "stale runtime must prove the candidate version"
        assert events == ["refresh", "activate"], "refresh must precede application activation"
