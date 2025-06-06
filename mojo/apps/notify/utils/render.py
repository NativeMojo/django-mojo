from django.template.loader import render_to_string
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from mojo.helpers.settings import settings
import os
from objict import objict
from mailman.models.template import MailTemplate
import csv
from io import StringIO
import re


def create_message(sender, recipients, subject, text=None, html=None, attachments=None, replyto=None):
    """
    Prepares an email message with given parameters.

    :param sender: The sender of the email.
    :param recipients: Can be a single email (string), or multiple emails (list or tuple).
    :param subject: Subject of the email.
    :param text: Plain text content of the email.
    :param html: HTML content of the email.
    :param attachments: List of attachments, can be strings or bytes.
    :param replyto: Email for reply-to address.
    :return: An object containing email details.
    """
    recipients = _parse_recipients(recipients)
    attachments = attachments or []

    message = objict(sender=sender, recipients=recipients)
    message.msg = create_multipart_message(sender, recipients, subject, text, html, attachments, replyto)
    return message

def _parse_recipients(recipients):
    """
    Parses recipients input into a list of emails.

    :param recipients: Can be a single email (string) or delimited string (comma/semicolon), or a list/tuple.
    :return: List of email strings.
    """
    if isinstance(recipients, str):
        if ',' in recipients:
            recipients = [t.strip() for t in recipients.split(',')]
        elif ';' in recipients:
            recipients = [t.strip() for t in recipients.split(';')]
        else:
            recipients = [recipients]
    elif not isinstance(recipients, (tuple, list)):
        recipients = [recipients]
    return recipients

def create_multipart_message(sender, recipients, subject, text=None, html=None, attachments=None, replyto=None):
    """
    Helper function to create a MIME multipart message with text, HTML, and attachments.

    :param sender: Email sender.
    :param recipients: List of recipient emails.
    :param subject: Subject of the email.
    :param text: Text content of the email.
    :param html: HTML content of the email.
    :param attachments: List of attachments.
    :param replyto: Reply-to email address.
    :return: A prepared MIMEMultipart message.
    """
    multipart_content_subtype = 'alternative' if text and html else 'mixed'
    msg = MIMEMultipart(multipart_content_subtype)
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = ', '.join(recipients)
    if replyto:
        msg.add_header('reply-to', replyto)

    if text:
        msg.attach(MIMEText(text, 'plain'))
    if html:
        # Remove non-ASCII characters to prevent encoding issues
        html = html.encode('ascii', 'ignore').decode('ascii')
        msg.attach(MIMEText(html, 'html'))

    for index, atch in enumerate(attachments or [], start=1):
        if isinstance(atch, (str, bytes)):
            atch = objict(name=f"attachment{index}.txt", data=atch, mimetype="text/plain")
        part = MIMEApplication(atch.data)
        part.add_header('Content-Type', atch.mimetype)
        part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(atch.name))
        msg.attach(part)

    return msg

def render_template(template_name, context, group=None):
    """
    Renders an email template with a provided context.

    :param template_name: Name of the template.
    :param context: Context to render the template.
    :param group: Template group filter (optional).
    :return: Rendered template as string or None.
    """
    context.update({
        "SITE_LABEL": settings.SITE_LABEL,
        "BASE_URL": settings.BASE_URL,
        "SITE_LOGO": settings.SITE_LOGO,
        "SERVER_NAME": settings.SERVER_NAME,
        "UNSUBSCRIBE_URL": settings.get("UNSUBSCRIBE_URL", f"{settings.BASE_URL}/api/account/unsubscribe"),
        "version": settings.VERSION,
        "COMPANY_NAME": settings.get("COMPANY_NAME", context.get("COMPANY_NAME", ""))
    })

    if template_name.endswith(("html", ".txt")):
        return render_to_string(template_name, context)

    qset = MailTemplate.objects.filter(name=template_name)
    if group is not None:
        qset = qset.filter(group=group)

    mtemp = qset.last()
    return mtemp.render(context) if mtemp else None

def generate_csv(qset, fields, name):
    """
    Generates a CSV from a queryset.

    :param qset: Queryset containing data.
    :param fields: Fields to include in CSV.
    :param name: Name for the CSV file.
    :return: An object with CSV data and metadata.
    """
    csv_io = StringIO()
    csvwriter = csv.writer(csv_io)

    csvwriter.writerow(fields)
    for row in qset.values_list(*fields):
        csvwriter.writerow(map(str, row))

    return objict(name=name, file=csv_io, data=csv_io.getvalue(), mimetype="text/csv")

def is_html(text):
    """
    Determine if the provided string contains HTML.

    :param text: The text to be checked.
    :return: True if HTML tags are found, False otherwise.
    """
    return bool(re.search(r'<[a-zA-Z0-9]+>.*?<\/[a-zA-Z0-9]+>', text))
