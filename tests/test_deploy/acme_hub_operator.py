"""Pure-phase contracts; no fixture value grants production authority."""
import base64
import struct
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid
from testit import helpers as th


def _refuses(call, expected=None):
    from mojo.deploy.acme_hub_operator_protocol import Refused
    try:
        call()
    except Refused as error:
        assert expected is None or error.code == expected, f'expected {expected}, received {error.code}'
    else:
        raise AssertionError('unsafe input must be refused')


def _profile():
    key = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + b'x' * 32
    return {'schema_version': 1, 'profile_id': 'fixture', 'revision': 1,
        'installation_uuid': str(uuid.uuid4()), 'wizard_origin': 'https://guardian.fixture.test',
        'rp_id': 'fixture.test', 'application_origins': ['https://app.fixture.test'],
        'nodes': [{'node_id': f'node-{index}', 'instance_id': f'instance-{index}',
            'host': f'node{index}.fixture.test', 'port': 22,
            'host_key': 'ssh-ed25519 ' + base64.b64encode(key).decode('ascii'),
            'host_key_sha256': hashlib.sha256(key).hexdigest(), 'node_profile_sha256': 'b' * 64} for index in (1, 2)],
        'runtimes': {kind: 'c' * 64 for kind in ('coordinator', 'verifier', 'provider_witness', 'producer_qualifier')},
        'independent_approval': {'owner_id': 1, 'repository_id': 2,
            'workflow_ref': 'fixture/repo/.github/workflows/enroll.yml@refs/heads/fixture',
            'workflow_sha': 'e' * 40, 'source_ref': 'refs/heads/fixture',
            'environment_id': 3, 'environment_name': 'fixture-only', 'reviewer_ids': [4]},
        'artifact_sha256': 'f' * 64, 'source_sha': 'e' * 40,
        'producer_profile_sha256': '1' * 64, 'witness_profile_sha256': '2' * 64}


@th.django_unit_test()
def test_retained_protocol_strict_frames_and_references(opts):
    from mojo.deploy import acme_hub_operator_protocol as wire
    assert wire.decode(b'{"version":1}') == {'version': 1}, 'canonical JSON object should decode'
    for raw in (b'{"version":1,"version":2}', b'{"n":NaN}', b'[]', b'{}' * 40000):
        _refuses(lambda: wire.decode(raw), 'invalid_frame')
    base = {'operation': str(uuid.uuid4()), 'expected_revision': 1,
            'replay_key': str(uuid.uuid4()), 'authorization': None}
    assert wire.validate_action('inspect', base) == base, 'read action uses exact context with no authorization bearer'
    _refuses(lambda: wire.validate_action('activate', base))
    _refuses(lambda: wire.validate_action('inspect', dict(base, helper_pid=42)))
    _refuses(lambda: wire.validate_action('inspect', dict(base, expected_revision=True)))
    _refuses(lambda: wire.validate_action('shell', base))
    status = wire.unavailable_status()
    assert status['operation'] is None and status['nodes'] == [], 'absent integration must not manufacture an operation or roster'
    assert not any(action['enabled'] for action in status['actions']), 'pure service must never enable a deferred root action'
    assert status['producer_coverage']['state'] == 'unavailable', 'missing source is not coverage'
    error = wire.envelope(error='dependent_integration_unavailable')
    assert error['data'] is None and error['ok'] is False, 'refusal must carry no fabricated successful result'


@th.django_unit_test()
def test_guardian_origin_and_exact_navigation(opts):
    from mojo.deploy import acme_hub_operator_protocol as wire
    origin = 'https://guardian.fixture.test'
    operation = str(uuid.uuid4())
    assert wire.destination(origin, operation)['url'] == origin + wire.PREFIX + '?operation=' + operation, 'navigation must bind exact approved guardian and opaque context'
    for unsafe in ('https://app.fixture.test/path', '//guardian.fixture.test', 'http://guardian.fixture.test',
                   'https://guardian.fixture.test:443', 'https://user@guardian.fixture.test',
                   'https://guardian.fixture.test#app', 'https://127.0.0.1', 'https://guardian.fixture.test/?return=x'):
        _refuses(lambda: wire.guardian_origin(unsafe))
    profile = _profile()
    from mojo.deploy import acme_hub_operator_profile
    assert acme_hub_operator_profile.validate(profile) == profile, 'declared fixture profile must validate as data only'
    _refuses(lambda: acme_hub_operator_profile.validate(dict(profile, application_origins=[origin])), 'profile_unavailable')
    _refuses(lambda: acme_hub_operator_profile.validate(dict(profile, wizard_origin='https://app.fixture.test')), 'profile_unavailable')
    _refuses(lambda: acme_hub_operator_profile.validate(dict(profile, force_qualified=True)))
    bad_nodes = [dict(node, host_key_sha256='0' * 64) for node in profile['nodes']]
    _refuses(lambda: acme_hub_operator_profile.validate(dict(profile, nodes=bad_nodes)), 'profile_unavailable')
    _refuses(lambda: acme_hub_operator_profile.validate(dict(profile, rp_id='unrelated.test')), 'profile_unavailable')


@th.django_unit_test()
def test_runtime_manifest_rejects_execution_hooks_and_unknown_inventory(opts):
    from mojo.deploy import acme_hub_operator_runtime as runtime
    manifest = {'version': 1, 'kind': 'verifier',
        'interpreter': {'path': runtime.INTERPRETER, 'sha256': 'a' * 64},
        'entry': 'verify.py', 'files': [{'path': 'verify.py', 'sha256': 'b' * 64, 'size': 50, 'mode': 0o444}],
        'source_sha': 'c' * 40, 'profile_sha256': 'd' * 64}
    assert runtime.validate_manifest(manifest) is manifest, 'immutable explicit manifest should validate without executing it'
    for path in ('../escape.py', '/tmp/escape.py', 'sitecustomize.py', 'lib/evil.pth', './verify.py'):
        _refuses(lambda: runtime.relative_path(path), 'runtime_unavailable')
    _refuses(lambda: runtime.validate_manifest(dict(manifest, interpreter={'path': '/tmp/python', 'sha256': 'a' * 64})), 'runtime_unavailable')
    _refuses(lambda: runtime.validate_manifest(dict(manifest, files=manifest['files'] * 2)), 'runtime_unavailable')
    _refuses(lambda: runtime.validate_manifest(dict(manifest, files=[dict(manifest['files'][0], mode=0o777)])), 'runtime_unavailable')


@th.django_unit_test()
def test_retained_root_import_does_not_enter_mojo_or_django(opts):
    directory = Path(__file__).resolve().parents[2] / 'mojo/deploy'
    code = ('import sys;sys.path.insert(0,sys.argv[1]);'
            'import acme_hub_operator_protocol,acme_hub_operator_runtime,'
            'acme_hub_operator_profile,acme_hub_operator_bridge;'
            'assert not any(k=="mojo" or k.startswith("mojo.") or k=="django" or k.startswith("django.") for k in sys.modules),'
            '"retained root path must not import framework packages"')
    result = subprocess.run([sys.executable, '-I', '-S', '-c', code, str(directory)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f'isolated stdlib-only root import refused: {result.stderr[-500:]}'
