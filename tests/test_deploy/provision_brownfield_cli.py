import json
import os
import shutil
import tempfile
from unittest import mock

from objict import objict
from testit import helpers as th

from .brownfield_fixture import raw_manifest


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
def test_fleet_dry_run_never_reaches_apply_or_managed_dag(opts):
    from mojo.deploy.provision import __main__ as cli

    root = _root()
    try:
        _write(root)
        console = _Console()
        observed = mock.Mock(return_value=([], [], _run()))
        applied = mock.Mock(return_value=([], [], _run()))
        with mock.patch.object(cli.clients_module, "build_clients",
                               return_value=object()), \
                mock.patch.object(cli.clients_module, "identify", return_value={
                    "account_id": "123456789012",
                    "arn": "arn:aws:iam::123456789012:user/ops"}), \
                mock.patch.object(cli.brownfield_plan, "observe", observed), \
                mock.patch.object(cli.brownfield_plan, "apply", applied), \
                mock.patch.object(cli.plan, "observe",
                                  side_effect=AssertionError(
                                      "managed DAG must remain unreachable")):
            code = cli.main([
                "fleet-apply", "--fleet", "shadow", "--project-root", root,
                "--dry-run"], console=console)
        th.assert_eq(code, 0, f"a clean fleet dry run must pass: {console.text}")
        th.assert_eq(observed.call_count, 1,
                     "fleet apply must perform its isolated preview")
        th.assert_eq(applied.called, False,
                     "--dry-run must structurally stop before fleet apply")
        for phrase in ("dependency digest", "manifest digest", "forced false", "DNS",
                       "certificates/ACM", "external public cutover"):
            th.assert_in(phrase, console.text,
                         f"the preview must say the negative boundary: {console.text}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


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


@th.django_unit_test()
def test_fleet_status_json_contains_redacted_dependency_inventory(opts):
    from mojo.deploy.provision import __main__ as cli

    root = _root()
    try:
        _write(root)
        console = _Console()
        with mock.patch.object(cli.clients_module, "build_clients",
                               return_value=object()), \
                mock.patch.object(cli.clients_module, "identify", return_value={
                    "account_id": "123456789012", "arn": "caller"}), \
                mock.patch.object(cli.brownfield_plan, "observe",
                                  return_value=([], [], _run())):
            code = cli.main([
                "fleet-status", "--fleet", "shadow", "--project-root", root,
                "--json"], console=console)
        th.assert_eq(code, 0, f"fleet status must pass: {console.text}")
        payload = json.loads(console.lines[0])
        th.assert_eq(payload["fleet"], "shadow",
                     f"machine output must name the fleet: {payload}")
        th.assert_eq(payload["dependency_digest"], "d" * 64,
                     f"the apply CAS input must be machine readable: {payload}")
        th.assert_eq(payload["action_digest"], "a" * 64,
                     f"the confirmed action CAS must be machine readable: {payload}")
        rendered = json.dumps(payload)
        for forbidden in ("password", "secret_value", "private_key"):
            th.assert_eq(forbidden in rendered, False,
                         f"status JSON must never carry {forbidden}: {rendered}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_fleet_commands_refuse_managed_environment_files(opts):
    from mojo.deploy.provision import __main__ as cli

    root = _root()
    try:
        os.makedirs(os.path.join(root, "aws", "environments"), exist_ok=True)
        with open(os.path.join(root, "aws", "environments", "shadow.json"),
                  "w") as handle:
            handle.write("{}\n")
        console = _Console()
        code = cli.main([
            "fleet-status", "--fleet", "shadow", "--project-root", root],
            console=console)
        th.assert_eq(code, 2,
                     "fleet commands must not reinterpret managed env files")
    finally:
        shutil.rmtree(root, ignore_errors=True)
