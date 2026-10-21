"""Template-owned sender branding keeps the verified mailbox address intact."""

from contextlib import contextmanager
from email.header import decode_header, make_header
from email.utils import parseaddr
from unittest import mock

from testit import helpers as th


DOMAIN = "template-sender-name.example.com"
ADDRESS = "no-reply@" + DOMAIN
TEMPLATE = "test_sender_name_branding"


@th.django_unit_setup()
def setup_sender_name(opts):
    from mojo.apps.aws.models import EmailDomain, EmailTemplate, Mailbox, SentMessage

    SentMessage.objects.filter(mailbox__domain__name=DOMAIN).delete()
    Mailbox.objects.filter(domain__name=DOMAIN).delete()
    EmailDomain.objects.filter(name=DOMAIN).delete()
    EmailTemplate.objects.filter(name=TEMPLATE).delete()
    domain = EmailDomain.objects.create(name=DOMAIN, status="verified", can_send=True)
    Mailbox.objects.create(domain=domain, email=ADDRESS, allow_outbound=True)
    EmailTemplate.objects.create(
        name=TEMPLATE, subject_template="Sign in", text_template="Your link",
        metadata={"sender_name": "CAMACTIVE PORTAL"})


@contextmanager
def _transport():
    from mojo.apps.aws.services import email

    sender = mock.Mock()
    sender.send_email.return_value = {"MessageId": "sender-name-test"}
    with mock.patch.object(email, "_get_sender", return_value=sender):
        yield sender


def _send():
    from mojo.apps.aws.services import email

    return email.send_with_template(
        from_email=ADDRESS, to="recipient@example.org", template_name=TEMPLATE,
        context={"sender_name": "Untrusted request value"})


@th.django_unit_test("template sender branding changes only the display name")
def test_template_sender_name(opts):
    from mojo.apps.aws.models import EmailTemplate

    template = EmailTemplate.objects.get(name=TEMPLATE)
    with _transport() as sender:
        for name in ("CAMACTIVE PORTAL", 'Camactive, "Support"', "Café Portal"):
            template.metadata = {"sender_name": name}
            template.save(update_fields=["metadata"])
            sent = _send()
            kwargs = sender.send_email.call_args.kwargs
            display, address = parseaddr(kwargs["source"])
            display = str(make_header(decode_header(display)))
            assert (display, address) == (name, ADDRESS), \
                f"template display name/address changed unexpectedly: {kwargs['source']!r}"
            assert kwargs["reply_to_addresses"] == [ADDRESS], \
                "sender branding must preserve the default reply-to mailbox"
            assert sent.mailbox.email == ADDRESS and sent.ses_message_id, \
                "sender branding must retain mailbox attribution and successful delivery evidence"


@th.django_unit_test("templates without sender branding retain the exact mailbox source")
def test_unbranded_template_unchanged(opts):
    from mojo.apps.aws.models import EmailTemplate

    template = EmailTemplate.objects.get(name=TEMPLATE)
    with _transport() as sender:
        for metadata in ({}, {"sender_name": ""}, {"sender_name": "   "}, []):
            template.metadata = metadata
            template.save(update_fields=["metadata"])
            _send()
            assert sender.send_email.call_args.kwargs["source"] == ADDRESS, \
                "an unbranded template or request context must not change the sender"


@th.django_unit_test("unsafe sender names fail before any provider call or sent row")
def test_unsafe_sender_name_rejected(opts):
    from mojo.apps.aws.models import EmailTemplate, SentMessage

    template = EmailTemplate.objects.get(name=TEMPLATE)
    with _transport() as sender:
        before = SentMessage.objects.filter(mailbox__email=ADDRESS).count()
        for name in ("Portal\r\nBcc: victim@example.org", "Portal\x01", "Portal\x7f",
                     "Portal\tName", 123, ["Portal"], None, "x" * 256):
            template.metadata = {"sender_name": name}
            template.save(update_fields=["metadata"])
            try:
                _send()
            except ValueError:
                pass
            else:
                raise AssertionError(f"unsafe sender name was accepted: {name!r}")
        assert not sender.send_email.called, "unsafe branding reached the email provider"
        assert SentMessage.objects.filter(mailbox__email=ADDRESS).count() == before, \
            "invalid template branding created a misleading sent-message row"
