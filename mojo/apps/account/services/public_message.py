"""
Public message (contact/support) kind schemas, validation, and notification fan-out.

This module is the single source of truth for per-kind fields. Both the submit
endpoint validator and the contact-page template context pull from KIND_SCHEMAS.
"""
import re

from mojo.helpers import logit
from mojo.helpers.settings import settings


logger = logit.get_logger('bouncer', 'bouncer.log')


# Field specs are (name, required, max_length, choices-or-None, label, placeholder, kind).
# "kind" is the HTML input type: text, email, textarea, select.
KIND_SCHEMAS = {
    'contact_us': {
        'title': 'Contact Us',
        'subtitle': "We'd love to hear from you.",
        'fields': [
            {'name': 'name', 'required': True, 'max_length': 120,
             'label': 'Your Name', 'placeholder': 'Jane Doe', 'input': 'text'},
            {'name': 'email', 'required': True, 'max_length': 254,
             'label': 'Email', 'placeholder': 'you@example.com', 'input': 'email'},
            {'name': 'company', 'required': False, 'max_length': 120,
             'label': 'Company (optional)', 'placeholder': 'Acme Inc.', 'input': 'text',
             'metadata': True},
            {'name': 'message', 'required': True, 'max_length': 4000,
             'label': 'Message', 'placeholder': 'Tell us what you need…', 'input': 'textarea'},
        ],
    },
    'support': {
        'title': 'Get Support',
        'subtitle': "Describe the problem and we'll take a look.",
        'fields': [
            {'name': 'name', 'required': True, 'max_length': 120,
             'label': 'Your Name', 'placeholder': 'Jane Doe', 'input': 'text'},
            {'name': 'email', 'required': True, 'max_length': 254,
             'label': 'Email', 'placeholder': 'you@example.com', 'input': 'email'},
            {'name': 'category', 'required': True, 'max_length': 32,
             'label': 'Category', 'input': 'select', 'metadata': True,
             'choices': [
                 ('billing', 'Billing'),
                 ('account', 'Account'),
                 ('bug', 'Bug report'),
                 ('other', 'Other'),
             ]},
            {'name': 'severity', 'required': True, 'max_length': 16,
             'label': 'Severity', 'input': 'select', 'metadata': True,
             'choices': [
                 ('low', 'Low'),
                 ('normal', 'Normal'),
                 ('high', 'High'),
             ]},
            {'name': 'message', 'required': True, 'max_length': 4000,
             'label': 'Describe the problem', 'placeholder': 'What happened?', 'input': 'textarea'},
        ],
    },
}

DEFAULT_KIND = 'contact_us'

# Common fields saved directly on the PublicMessage row — everything else
# falls into metadata.
_COMMON_FIELDS = {'name', 'email', 'subject', 'message'}

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def get_kind(kind):
    """Return the schema for a kind, or None if unknown."""
    return KIND_SCHEMAS.get(kind)


def resolve_kind(raw_kind):
    """Clean an untrusted kind string, falling back to DEFAULT_KIND."""
    if raw_kind and raw_kind in KIND_SCHEMAS:
        return raw_kind
    return DEFAULT_KIND


def render_context_for_kind(kind):
    """
    Build the render context block for the contact template.

    Returns a dict with: kind, title, subtitle, fields (list of dicts).
    """
    schema = KIND_SCHEMAS.get(kind) or KIND_SCHEMAS[DEFAULT_KIND]
    return {
        'kind': kind if kind in KIND_SCHEMAS else DEFAULT_KIND,
        'kind_title': schema['title'],
        'kind_subtitle': schema['subtitle'],
        'kind_fields': schema['fields'],
        'known_kinds': list(KIND_SCHEMAS.keys()),
    }


def validate_submission(kind, data):
    """
    Validate the submitted form against the kind schema.

    data is a dict-like (request.DATA / objict). Returns (common, metadata) dicts.
    Raises ValueError('field:reason') on any validation problem.
    """
    schema = KIND_SCHEMAS.get(kind)
    if schema is None:
        raise ValueError('kind:invalid')

    max_len_cap = int(settings.get_static('BOUNCER_PUBLIC_MESSAGE_MAX_LENGTH', 4000))

    common = {}
    metadata = {}

    for field in schema['fields']:
        name = field['name']
        raw = data.get(name)
        if raw is None:
            raw = ''
        if isinstance(raw, str):
            value = raw.strip()
        else:
            value = str(raw).strip()

        if not value:
            if field['required']:
                raise ValueError(f'{name}:required')
            continue

        # Length cap — use the per-field max_length, but clamp textareas
        # to the system-wide cap in settings so an admin can tighten it.
        max_length = field.get('max_length') or max_len_cap
        if name == 'message':
            max_length = min(max_length, max_len_cap)
        if len(value) > max_length:
            raise ValueError(f'{name}:too_long')

        if field.get('input') == 'email':
            if not _EMAIL_RE.match(value):
                raise ValueError(f'{name}:invalid')

        choices = field.get('choices')
        if choices:
            allowed = {c[0] for c in choices}
            if value not in allowed:
                raise ValueError(f'{name}:invalid')

        if field.get('metadata') or name not in _COMMON_FIELDS:
            metadata[name] = value
        else:
            common[name] = value

    # Optional content moderation — fail-open on exceptions.
    _run_content_guard(common)

    return common, metadata


def _run_content_guard(common):
    """Run content_guard.check_text on free-form fields. Fail-open on errors."""
    try:
        from mojo.helpers import content_guard
    except Exception as err:  # pragma: no cover — defensive
        logger.warning(f"public_message: content_guard import failed: {err}")
        return

    for field_name in ('name', 'subject', 'message'):
        value = common.get(field_name)
        if not value:
            continue
        try:
            result = content_guard.check_text(value, surface='contact_form')
        except Exception as err:
            logger.warning(
                f"public_message: content_guard failed on {field_name}: {err}"
            )
            continue
        if getattr(result, 'decision', None) == 'block':
            raise ValueError(f'{field_name}:blocked')


# ---------------------------------------------------------------------------
# Notification fan-out
# ---------------------------------------------------------------------------

def notify_admins(message):
    """
    Send a notification email to every User flagged with
    metadata.protected.notify_public_messages=True.

    When the message is scoped to a group, only members of that group (active)
    receive the email. Without a group, every flagged user across the system
    is notified.

    Wrapped in per-recipient try/except so a single failing mailbox doesn't
    short-circuit the batch. Returns the number of emails dispatched.
    """
    from mojo.apps.account.models import User

    flagged_qs = User.objects.filter(
        is_active=True,
        metadata__contains={"protected": {"notify_public_messages": True}},
    )

    recipients = flagged_qs
    if message.group_id:
        member_user_ids = list(
            message.group.members.filter(is_active=True).values_list('user_id', flat=True)
        )
        if not member_user_ids:
            return 0
        recipients = flagged_qs.filter(pk__in=member_user_ids)

    template_name = settings.get_static(
        'PUBLIC_MESSAGE_NOTIFY_TEMPLATE', 'public_message_notify'
    )
    subject_template = settings.get_static(
        'PUBLIC_MESSAGE_NOTIFY_SUBJECT', 'New {kind} message'
    )
    subject = subject_template.format(kind=message.kind)

    context = {
        'message': {
            'id': message.id,
            'kind': message.kind,
            'name': message.name,
            'email': message.email,
            'subject': message.subject,
            'message': message.message,
            'metadata': message.metadata or {},
            'created': str(message.created),
        },
        'group_name': message.group.name if message.group_id else '',
    }

    sent = 0
    for user in recipients:
        try:
            user.send_template_email(
                template_name,
                context=context,
                subject=subject,
                group=message.group,
                fail_silently=True,
            )
            sent += 1
        except Exception as err:
            logger.warning(
                f"public_message: notify failed user={user.id} err={err}"
            )
    return sent
