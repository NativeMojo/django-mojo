"""Settings-free retained operator wire. A document is never process authority.

Root callers import only manifest-pinned bytes. Public inputs contain selectors
and review references; no PID, receipt, provider body or executable is accepted.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

VERSION = 1
PREFIX = '/_mojo/acme-upgrade/'
API_PREFIX = PREFIX + 'api/'
MAX_FRAME = 65536
MAX_NODES = 64
ACTIONS = ('inspect', 'enroll-retained', 'review-maintenance', 'establish-holds',
           'witness', 'prepare', 'activate', 'cancel', 'reconcile', 'retry',
           'restore', 'verify')
READ_ACTIONS = frozenset(('inspect', 'verify'))
STATES = ('unavailable', 'bootstrap_only', 'enrollment_pending', 'enrolled',
          'planning', 'holding', 'prepared', 'reviewing', 'activating',
          'active', 'restoring', 'verified', 'held_recovery_required',
          'commit_unknown', 'cancelled')
OUTCOMES = ('not_started', 'pending', 'committed', 'unknown', 'refused')
ERRORS = frozenset(('invalid_request', 'invalid_frame', 'origin_refused',
    'csrf_refused', 'not_authenticated', 'reauth_required', 'operator_not_authorized',
    'generation_changed', 'replay_refused', 'request_expired', 'session_unavailable',
    'runtime_unavailable', 'profile_unavailable', 'operator_service_unavailable',
    'independent_approval_unavailable', 'database_enrollment_unavailable',
    'coordinator_unavailable', 'provider_witness_unavailable',
    'producer_profile_unavailable', 'producer_identity_mismatch',
    'producer_coverage_unavailable', 'application_coverage_unavailable',
    'transport_unavailable', 'node_unavailable', 'floor_unavailable',
    'configuration_busy', 'held_recovery_required', 'commit_unknown',
    'qualification_environment_unavailable', 'dependent_integration_unavailable'))
HEX64 = re.compile(r'^[0-9a-f]{64}$')
HEX32 = re.compile(r'^[0-9a-f]{32}$')
SHA = re.compile(r'^[0-9a-f]{40}$')
TEXT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')


class Refused(ValueError):
    def __init__(self, code='invalid_request'):
        self.code = code if code in ERRORS else 'invalid_request'
        super().__init__(self.code)


def require(condition, code='invalid_request'):
    if not condition:
        raise Refused(code)


def fields(value, names, code='invalid_request'):
    require(type(value) is dict and set(value) == set(names), code)
    return value


def text(value, maximum=128):
    require(type(value) is str and 0 < len(value) <= maximum
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))
    return value


def identifier(value):
    require(type(value) is str and TEXT.fullmatch(value) is not None)
    return value


def uuid_text(value):
    try:
        require(type(value) is str and str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError, TypeError):
        raise Refused() from None
    return value


def digest(value):
    require(type(value) is str and HEX64.fullmatch(value) is not None)
    return value


def revision(value):
    require(type(value) is int and 0 <= value < 2**63)
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'invalid_frame')
        value[key] = item
    return value


def decode(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_FRAME, 'invalid_frame')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda item: require(False, 'invalid_frame'))
    except (ValueError, UnicodeError, RecursionError):
        raise Refused('invalid_frame') from None
    require(type(value) is dict, 'invalid_frame')
    return value


def encoded(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True, allow_nan=False).encode('ascii')
    except (ValueError, TypeError, RecursionError):
        raise Refused('invalid_frame') from None
    require(len(raw) <= MAX_FRAME, 'invalid_frame')
    return raw


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def guardian_origin(value):
    text(value, 260)
    try:
        parsed = urlsplit(value)
        require(parsed.scheme == 'https' and parsed.hostname is not None
                and parsed.netloc == parsed.hostname and parsed.port is None
                and not parsed.path and not parsed.query and not parsed.fragment
                and parsed.hostname == parsed.hostname.lower()
                and re.fullmatch(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', parsed.hostname)
                and '.' in parsed.hostname and '..' not in parsed.hostname
                and not re.fullmatch(r'[0-9.]+', parsed.hostname))
    except ValueError:
        raise Refused('profile_unavailable') from None
    return value


def destination(origin, operation=None):
    guardian_origin(origin)
    suffix = '' if operation is None else '?operation=' + uuid_text(operation)
    return {'label': 'Open guarded operator wizard', 'url': origin + PREFIX + suffix}


def validate_action(action, body):
    require(action in ACTIONS)
    fields(body, ('operation', 'expected_revision', 'replay_key', 'authorization'))
    uuid_text(body['operation'])
    revision(body['expected_revision'])
    uuid_text(body['replay_key'])
    if action in READ_ACTIONS:
        require(body['authorization'] is None)
    else:
        uuid_text(body['authorization'])
    return body


def validate_options(body):
    fields(body, ('purpose', 'action', 'operation', 'expected_revision', 'review'))
    require(body['purpose'] in ('sign_in', 'action', 'bootstrap_possession'))
    revision(body['expected_revision'])
    if body['purpose'] == 'action':
        require(body['action'] in ACTIONS and body['action'] not in READ_ACTIONS)
        uuid_text(body['operation'])
        uuid_text(body['review'])
    else:
        require(body['action'] is None and body['operation'] is None and body['review'] is None)
    return body


def validate_assertion(body):
    fields(body, ('request', 'credential'))
    uuid_text(body['request'])
    credential = fields(body['credential'], ('id', 'rawId', 'response', 'type',
                                            'clientExtensionResults', 'authenticatorAttachment'))
    require(credential['type'] == 'public-key')
    for key in ('id', 'rawId'):
        require(type(credential[key]) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,2048}', credential[key]))
    require(credential['authenticatorAttachment'] in (None, 'platform', 'cross-platform'))
    fields(credential['clientExtensionResults'], ())
    response = fields(credential['response'], ('clientDataJSON', 'authenticatorData', 'signature', 'userHandle'))
    for key in ('clientDataJSON', 'authenticatorData', 'signature'):
        require(type(response[key]) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,16384}', response[key]))
    require(response['userHandle'] is None or (type(response['userHandle']) is str
            and re.fullmatch(r'[A-Za-z0-9_-]{1,2048}', response['userHandle'])))
    return body


def envelope(data=None, error=None, current_revision=0, request_id=None):
    revision(current_revision)
    require(error is None or error in ERRORS)
    request_id = request_id or uuid.uuid4().hex
    require(HEX32.fullmatch(request_id) is not None)
    return {'version': VERSION, 'ok': error is None, 'data': data if error is None else None,
            'error': None if error is None else {'code': error, 'retryable': False},
            'request_id': request_id, 'revision': current_revision, 'observed_at': utc_now()}


def unavailable_status(profile=None, csrf_token=None):
    """Truthful pure-phase projection: configured expectations are not proof."""
    now = utc_now()
    return {'schema_version': VERSION,
        'installation': {'uuid': None if profile is None else profile['installation_uuid'],
                         'state': 'unavailable', 'enrolled': False},
        'operation': None,
        'session': {'authenticated': False, 'actor': None, 'fresh_until': None,
                    'csrf_token': csrf_token},
        'clock': {'server_time': now, 'deadline_at': None},
        'actions': [{'name': name, 'enabled': False, 'reason': 'dependent_integration_unavailable',
                     'requires_reauth': name not in READ_ACTIONS} for name in ACTIONS],
        'facts': [{'id': 'integration', 'label': 'Service integration', 'state': 'unavailable',
                   'source': 'retained_guardian', 'observed_at': now,
                   'items': [{'key': 'required', 'value': 'reviewed foundation and adapter integration',
                              'classification': 'unavailable'}]}],
        'nodes': [], 'bootstrap': {'state': 'unavailable', 'candidate_id': None,
                                  'independent_approval_url': None, 'error_code': 'dependent_integration_unavailable'},
        'producer_identity': {'state': 'unavailable', 'missing_facts': ['dependent_integration_unavailable']},
        'producer_coverage': {'state': 'unavailable', 'missing_facts': ['producer_coverage_unavailable']},
        'provider_witness': {'state': 'unavailable', 'missing_facts': ['provider_witness_unavailable']},
        'destinations': {'operator_wizard': None, 'independent_approval': None,
                         'provider_console': None, 'history_console': None}}
