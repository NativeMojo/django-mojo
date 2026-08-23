import json
import os
import shutil
import tempfile
from unittest import mock

from objict import objict
from testit import helpers as th

from test_deploy.brownfield_fixture import raw_manifest


class _Console:
    def __init__(self):
        self.lines = []

    def say(self, line=""):
        self.lines.append(str(line))

    def is_interactive(self):
        return False

    @property
    def text(self):
        return "\n".join(self.lines)


def _root():
    return tempfile.mkdtemp(prefix="testit_brownfield_cli.")


def _write(root):
    from mojo.deploy.provision import brownfield_inputs
    path = brownfield_inputs.fleet_path(root, "shadow")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(json.dumps(raw_manifest(), indent=2, sort_keys=True) + "\n")
    return path


def _run():
    observed = objict(account_id="123456789012", dependency_digest="d" * 64,
                      action_digest="a" * 64,
                      dependency_inventory={"network": {"vpc": "vpc-1"}})
    return objict(steps=objict(), observed=observed, worst="PASS",
                  blocking=False, validated=True, problems=[])


@th.django_unit_test()
def test_fleet_preview_names_explicit_request_service_selection(opts):
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import brownfield_inputs

    raw = raw_manifest()
    raw["nodes"]["items"][0]["request_service"] = False
    topology = brownfield_inputs.to_spec(brownfield_inputs.validate(raw))
    console = _Console()
    cli._render_fleet_preview(topology, [], [], _run(), console)

    th.assert_in(f"manifest digest: {topology.manifest_digest}", console.text,
                 "human preview must bind the exact normalized manifest")
    th.assert_in("node request service: maestro-api-1=false", console.text,
                 "human preview must name explicit per-node request authority")
