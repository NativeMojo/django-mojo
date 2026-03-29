"""
Tests for the permissions audit fixes and category permissions.
Verifies all models have correct fine-grained and category permissions.
"""
from testit import helpers as th


# ---------------------------------------------------------------------------
# Category permissions — every domain should have its category perm
# ---------------------------------------------------------------------------

CATEGORY_MAP = {
    'security': [
        ('mojo.apps.incident.models.incident', 'Incident'),
        ('mojo.apps.incident.models.event', 'Event'),
        ('mojo.apps.incident.models.history', 'IncidentHistory'),
        ('mojo.apps.incident.models.ipset', 'IPSet'),
        ('mojo.apps.account.models.bouncer_device', 'BouncerDevice'),
        ('mojo.apps.account.models.bouncer_signal', 'BouncerSignal'),
        ('mojo.apps.account.models.bot_signature', 'BotSignature'),
        ('mojo.apps.account.models.geolocated_ip', 'GeoLocatedIP'),
        ('mojo.apps.logit.models.log', 'Log'),
    ],
    'users': [
        ('mojo.apps.account.models.user', 'User'),
        ('mojo.apps.account.models.pkey', 'Passkey'),
        ('mojo.apps.account.models.totp', 'UserTOTP'),
        ('mojo.apps.account.models.user_api_key', 'UserAPIKey'),
        ('mojo.apps.account.models.oauth', 'OAuthConnection'),
    ],
    'groups': [
        ('mojo.apps.account.models.group', 'Group'),
        ('mojo.apps.account.models.member', 'GroupMember'),
        ('mojo.apps.account.models.api_key', 'ApiKey'),
        ('mojo.apps.account.models.setting', 'Setting'),
    ],
    'comms': [
        ('mojo.apps.account.models.push.config', 'PushConfig'),
        ('mojo.apps.account.models.push.template', 'NotificationTemplate'),
        ('mojo.apps.account.models.push.delivery', 'NotificationDelivery'),
        ('mojo.apps.account.models.push.device', 'RegisteredDevice'),
        ('mojo.apps.phonehub.models.phone', 'PhoneNumber'),
        ('mojo.apps.phonehub.models.config', 'PhoneConfig'),
        ('mojo.apps.phonehub.models.sms', 'SMS'),
        ('mojo.apps.aws.models.mailbox', 'Mailbox'),
        ('mojo.apps.aws.models.email_domain', 'EmailDomain'),
        ('mojo.apps.aws.models.email_template', 'EmailTemplate'),
        ('mojo.apps.aws.models.sent_message', 'SentMessage'),
        ('mojo.apps.aws.models.incoming_email', 'IncomingEmail'),
        ('mojo.apps.aws.models.email_attachment', 'EmailAttachment'),
        ('mojo.apps.chat.models.room', 'ChatRoom'),
        ('mojo.apps.chat.models.message', 'ChatMessage'),
        ('mojo.apps.chat.models.membership', 'ChatMembership'),
        ('mojo.apps.chat.models.read_receipt', 'ChatReadReceipt'),
        ('mojo.apps.chat.models.reaction', 'ChatReaction'),
    ],
    'files': [
        ('mojo.apps.fileman.models.manager', 'FileManager'),
        ('mojo.apps.fileman.models.file', 'File'),
        ('mojo.apps.fileman.models.rendition', 'FileRendition'),
        ('mojo.apps.filevault.models.file', 'VaultFile'),
        ('mojo.apps.filevault.models.data', 'VaultData'),
    ],
    'jobs': [
        ('mojo.apps.jobs.models.job', 'Job'),
        ('mojo.apps.jobs.models.job', 'JobEvent'),
        ('mojo.apps.jobs.models.job', 'JobLog'),
    ],
}


def _get_model(module_path, class_name):
    """Import and return a model class."""
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


@th.django_unit_test()
def test_category_perms_in_view_perms(opts):
    """Every model should have its category perm in VIEW_PERMS."""
    for category, models in CATEGORY_MAP.items():
        for module_path, class_name in models:
            model = _get_model(module_path, class_name)
            view = model.RestMeta.VIEW_PERMS
            # Skip models with VIEW_PERMS = ['all'] (public read)
            if 'all' in view:
                continue
            assert category in view, \
                f"{class_name} VIEW_PERMS missing '{category}': {view}"


@th.django_unit_test()
def test_category_perms_in_save_perms(opts):
    """Every model with SAVE_PERMS should have its category perm there too."""
    for category, models in CATEGORY_MAP.items():
        for module_path, class_name in models:
            model = _get_model(module_path, class_name)
            save = getattr(model.RestMeta, 'SAVE_PERMS', None)
            # Skip read-only models (no SAVE_PERMS or empty list)
            if not save:
                continue
            assert category in save, \
                f"{class_name} SAVE_PERMS missing '{category}': {save}"


# ---------------------------------------------------------------------------
# Bouncer models — no admin_security, uses security + users categories
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_bouncer_no_admin_security(opts):
    """Bouncer models should not use admin_security."""
    from mojo.apps.account.models.bouncer_device import BouncerDevice
    from mojo.apps.account.models.bouncer_signal import BouncerSignal
    from mojo.apps.account.models.bot_signature import BotSignature

    for model in [BouncerDevice, BouncerSignal, BotSignature]:
        view = model.RestMeta.VIEW_PERMS
        save = getattr(model.RestMeta, 'SAVE_PERMS', [])
        assert 'admin_security' not in view, \
            f"{model.__name__} VIEW_PERMS still has admin_security: {view}"
        assert 'admin_security' not in save, \
            f"{model.__name__} SAVE_PERMS still has admin_security: {save}"


# ---------------------------------------------------------------------------
# Cross-domain models — bouncer accessible via both security and users
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_bouncer_dual_category(opts):
    """Bouncer models should be accessible via both 'security' and 'users' categories."""
    from mojo.apps.account.models.bouncer_device import BouncerDevice
    from mojo.apps.account.models.bouncer_signal import BouncerSignal
    from mojo.apps.account.models.bot_signature import BotSignature

    for model in [BouncerDevice, BouncerSignal, BotSignature]:
        view = model.RestMeta.VIEW_PERMS
        assert 'security' in view, f"{model.__name__} VIEW_PERMS missing 'security': {view}"
        assert 'users' in view, f"{model.__name__} VIEW_PERMS missing 'users': {view}"


# ---------------------------------------------------------------------------
# File models — explicit SAVE_PERMS
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_file_models_have_save_perms(opts):
    """File models should have explicit SAVE_PERMS with manage_files."""
    from mojo.apps.fileman.models.manager import FileManager
    from mojo.apps.fileman.models.file import File
    from mojo.apps.fileman.models.rendition import FileRendition

    for model in [FileManager, File, FileRendition]:
        save = model.RestMeta.SAVE_PERMS
        assert 'manage_files' in save, f"{model.__name__} SAVE_PERMS missing manage_files: {save}"
        assert 'files' in save, f"{model.__name__} SAVE_PERMS missing 'files': {save}"


# ---------------------------------------------------------------------------
# Write perms always in view perms (no write-without-read)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_write_perms_subset_of_view_perms(opts):
    """SAVE_PERMS should be a subset of VIEW_PERMS (excluding owner and view/manage pairs).

    The view/manage convention means manage_X in SAVE_PERMS pairs with view_X in VIEW_PERMS.
    As long as the category perm is in both, a write-user can always read via the category.
    """
    # Known view/manage pairs where manage is intentionally only in SAVE
    KNOWN_PAIRS = {
        'manage_security': 'view_security',
        'manage_notifications': 'view_notifications',
    }

    for category, models in CATEGORY_MAP.items():
        for module_path, class_name in models:
            model = _get_model(module_path, class_name)
            view = set(model.RestMeta.VIEW_PERMS)
            save = set(getattr(model.RestMeta, 'SAVE_PERMS', []))
            # owner is a special case
            save_check = save - {'owner'}
            # Skip models with 'all' in view (public read)
            if 'all' in view:
                continue
            # Remove known manage-only perms that have a view counterpart in VIEW_PERMS
            for manage_perm, view_perm in KNOWN_PAIRS.items():
                if manage_perm in save_check and view_perm in view:
                    save_check.discard(manage_perm)
            missing = save_check - view
            assert not missing, \
                f"{class_name}: SAVE_PERMS {missing} not in VIEW_PERMS"


# ---------------------------------------------------------------------------
# Docs — VIEW is 'all', category only in SAVE (fine-grained, not a category toggle)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_docs_save_perms(opts):
    """Docit models should have 'docs' in SAVE_PERMS."""
    from mojo.apps.docit.models.book import Book
    from mojo.apps.docit.models.asset import Asset
    from mojo.apps.docit.models.page_revision import PageRevision

    for model in [Book, Asset, PageRevision]:
        save = model.RestMeta.SAVE_PERMS
        assert 'docs' in save, f"{model.__name__} SAVE_PERMS missing 'docs': {save}"
