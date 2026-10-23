"""The real legacy provision resolver must not disable enrolled liveness."""
from contextlib import nullcontext
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th
from .liveness import NOW, _runner
from .review_regressions import Client


@th.django_unit_test()
def test_real_legacy_resolver_with_source_environment_and_no_provision_files(opts):
    from mojo.apps.aws.services import infra_setup
    from mojo.apps.incident.services import health_liveness as hl
    from mojo.apps.account.services import health_evidence as he
    values = {"MOJO_ENVIRONMENT": "production", "JOBS_CHANNELS": ["edge"],
              "ADMIN_FLEET_CONFIG_EXPECTED_NODES": ["node-a"]}
    cursor = SimpleNamespace(execute=lambda *args: None, fetchone=lambda: (None,))
    with TemporaryDirectory() as root, \
            mock.patch.object(hl.settings, "get_static", side_effect=lambda key, default=None: values.get(key, default)), \
            mock.patch.object(he, "primary_cursor", return_value=nullcontext(cursor)), \
            mock.patch.object(infra_setup, "_project_root", return_value=root):
        selected, problem = infra_setup._resolve_spec()
        assert selected is None and problem["details"] == {"setting": "MOJO_ENVIRONMENT", "selected": "production", "environment_count": 0}, "legacy fixture did not exercise the real selected-environment/no-files resolver"
        result = hl.collect(client=Client(), roster=lambda *args, **kwargs: [_runner()], now=NOW)
    assert result["runners"]["healthy"] is True, "a source environment with no provisioning files permanently disabled legacy liveness collection"
