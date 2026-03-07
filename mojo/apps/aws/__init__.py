from mojo import errors as merrors


def send_email(to, subject=None, body_text=None, body_html=None,
               cc=None, bcc=None, reply_to=None, mailbox=None, **kwargs):
    """
    Send a plain email using the system default mailbox.

    Args:
        to (str or list): Recipient address or list of addresses.
        subject (str): Email subject line.
        body_text (str): Plain text body (optional but recommended alongside body_html).
        body_html (str): HTML body.
        cc (str or list): CC address or list of addresses.
        bcc (str or list): BCC address or list of addresses.
        reply_to (str or list): Reply-to address or list of addresses.
        mailbox (Mailbox): Override the sending mailbox. Defaults to the system default.
        **kwargs: Additional arguments passed through to the underlying email service.

    Returns:
        SentMessage: The sent message record.

    Raises:
        ValueException: If no mailbox is configured and no override is provided.
        OutboundNotAllowed: If the resolved mailbox has outbound sending disabled.

    Example:
        from mojo.apps import aws

        aws.send_email(
            to="alice@example.com",
            subject="Hello",
            body_html="<p>Hello Alice</p>",
            body_text="Hello Alice"
        )
    """
    from mojo.apps.aws.models import Mailbox

    if mailbox is None:
        mailbox = Mailbox.get_system_default()
    if mailbox is None:
        raise merrors.ValueException("No mailbox configured. Set up a system default mailbox to send email.")

    return mailbox.send_email(
        to=to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        cc=cc,
        bcc=bcc,
        reply_to=reply_to,
        **kwargs
    )


def send_template_email(to, template_name, context=None,
                        cc=None, bcc=None, reply_to=None, mailbox=None, **kwargs):
    """
    Send an email using a named EmailTemplate stored in the database.

    The template subject and body are rendered with the supplied context dict using
    {{variable}} syntax. If the resolved mailbox belongs to a domain, a domain-specific
    template override ("{domain_name}.{template_name}") is used automatically when it exists.

    Args:
        to (str or list): Recipient address or list of addresses.
        template_name (str): Name of the EmailTemplate record in the database.
        context (dict): Template variables substituted into the subject and body.
        cc (str or list): CC address or list of addresses.
        bcc (str or list): BCC address or list of addresses.
        reply_to (str or list): Reply-to address or list of addresses.
        mailbox (Mailbox): Override the sending mailbox. Defaults to the system default.
        **kwargs: Additional arguments passed through to the underlying email service.

    Returns:
        SentMessage: The sent message record.

    Raises:
        ValueException: If no mailbox is configured and no override is provided.
        OutboundNotAllowed: If the resolved mailbox has outbound sending disabled.
        ValueError: If the named template does not exist in the database.

    Example:
        from mojo.apps import aws

        aws.send_template_email(
            to="alice@example.com",
            template_name="welcome",
            context={"display_name": "Alice", "app_name": "MyApp"}
        )
    """
    from mojo.apps.aws.models import Mailbox

    if mailbox is None:
        mailbox = Mailbox.get_system_default()
    if mailbox is None:
        raise merrors.ValueException("No mailbox configured. Set up a system default mailbox to send email.")

    return mailbox.send_template_email(
        to=to,
        template_name=template_name,
        context=context,
        cc=cc,
        bcc=bcc,
        reply_to=reply_to,
        **kwargs
    )