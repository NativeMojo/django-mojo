"""Root-installed data catalog. Application configuration is not this policy."""
import base64
import hashlib
import struct
from urllib.parse import urlsplit
if __package__:
    from . import acme_hub_operator_protocol as wire
else:
    import acme_hub_operator_protocol as wire
if __package__:
    from .acme_hub_operator_runtime import read_owned
else:
    from acme_hub_operator_runtime import read_owned

PROFILE_PATH = '/etc/mojo/acme-operator-profile.json'
KINDS = ('coordinator', 'verifier', 'provider_witness', 'producer_qualifier')


def validate(value):
    wire.fields(value, ('schema_version', 'profile_id', 'revision', 'installation_uuid',
        'wizard_origin', 'rp_id', 'application_origins', 'nodes', 'runtimes',
        'independent_approval', 'artifact_sha256', 'source_sha',
        'producer_profile_sha256', 'witness_profile_sha256'))
    wire.require(type(value['schema_version']) is int and value['schema_version'] == 1, 'profile_unavailable')
    wire.identifier(value['profile_id']); wire.revision(value['revision']); wire.uuid_text(value['installation_uuid'])
    wire.guardian_origin(value['wizard_origin'])
    host = urlsplit(value['wizard_origin']).hostname
    rp = wire.text(value['rp_id'], 253)
    wire.require(rp == rp.lower() and '.' in rp and (host == rp or host.endswith('.' + rp)), 'profile_unavailable')
    origins = value['application_origins']
    wire.require(type(origins) is list and 1 <= len(origins) <= 64 and len(set(origins)) == len(origins), 'profile_unavailable')
    for origin in origins:
        wire.guardian_origin(origin)
        wire.require(urlsplit(origin).hostname != host, 'profile_unavailable')
    wire.require(type(value['source_sha']) is str and wire.SHA.fullmatch(value['source_sha']), 'profile_unavailable')
    for key in ('artifact_sha256', 'producer_profile_sha256', 'witness_profile_sha256'):
        wire.digest(value[key])
    wire.fields(value['runtimes'], KINDS)
    for digest in value['runtimes'].values():
        wire.digest(digest)
    nodes = value['nodes']
    wire.require(type(nodes) is list and 2 <= len(nodes) <= wire.MAX_NODES, 'profile_unavailable')
    seen = set()
    for node in nodes:
        wire.fields(node, ('node_id', 'instance_id', 'host', 'port', 'host_key', 'host_key_sha256', 'node_profile_sha256'))
        wire.identifier(node['node_id']); wire.identifier(node['instance_id'])
        wire.require(node['node_id'] not in seen and type(node['port']) is int and node['port'] == 22, 'profile_unavailable')
        seen.add(node['node_id'])
        wire.require(type(node['host']) is str and wire.TEXT.fullmatch(node['host']) is not None
                     and not node['host'].startswith('-'), 'profile_unavailable')
        wire.text(node['host_key'], 1024)
        parts = node['host_key'].split(' ')
        wire.require(len(parts) == 2 and parts[0] == 'ssh-ed25519', 'profile_unavailable')
        try:
            key = base64.b64decode(parts[1], validate=True)
        except ValueError:
            raise wire.Refused('profile_unavailable') from None
        wire.require(len(key) == 51 and key[:19] == struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32)
                     and base64.b64encode(key).decode('ascii') == parts[1]
                     and hashlib.sha256(key).hexdigest() == node['host_key_sha256'], 'profile_unavailable')
        wire.digest(node['host_key_sha256']); wire.digest(node['node_profile_sha256'])
    approval = wire.fields(value['independent_approval'], ('owner_id', 'repository_id', 'workflow_ref',
        'workflow_sha', 'source_ref', 'environment_id', 'environment_name', 'reviewer_ids'))
    for key in ('owner_id', 'repository_id', 'environment_id'):
        wire.require(type(approval[key]) is int and approval[key] > 0, 'profile_unavailable')
    for key in ('workflow_ref', 'source_ref', 'environment_name'):
        wire.text(approval[key], 512)
    wire.require(type(approval['workflow_sha']) is str and wire.SHA.fullmatch(approval['workflow_sha']), 'profile_unavailable')
    reviewers = approval['reviewer_ids']
    wire.require(type(reviewers) is list and 1 <= len(reviewers) <= 20 and
                 all(type(item) is int and item > 0 for item in reviewers) and len(set(reviewers)) == len(reviewers),
                 'profile_unavailable')
    return wire.decode(wire.encoded(value))


def load():
    try:
        return validate(wire.decode(read_owned(PROFILE_PATH, 0o644)))
    except (OSError, wire.Refused):
        raise wire.Refused('profile_unavailable') from None
