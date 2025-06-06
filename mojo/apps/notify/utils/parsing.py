from io import StringIO
import email
import re
from email.utils import parseaddr, parsedate_to_datetime, getaddresses
from email.header import decode_header
from objict import objict


def decode_payload(part):
    """
    Decode the email part payload.

    Args:
        part (email.message.Part): The email part.

    Returns:
        str: The decoded payload as a string.
    """
    charset = part.get_content_charset() or 'utf-8'
    return str(part.get_payload(decode=True), charset, 'replace')


def parse_attachment(message_part):
    """
    Parse an email message part for attachments.

    Args:
        message_part (email.message.Part): The email message part.

    Returns:
        objict: An object representing the attachment, or None if not applicable.
    """
    content_disposition = message_part.get("Content-Disposition")
    content_type = message_part.get_content_type()

    if not content_disposition and content_type == "multipart/alternative":
        return None

    attachment = objict({
        'dispositions': [],
        'disposition': None,
        'payload': message_part.get_payload(decode=False),
        'charset': message_part.get_content_charset(),
        'content_type': content_type,
        'encoding': message_part.get("Content-Transfer-Encoding", "utf8"),
        'content': None,
        'name': None,
        'create_date': None,
        'mod_date': None,
        'read_date': None,
    })

    dispositions = []
    if content_disposition:
        dispositions = content_disposition.strip().split(";")
        attachment['dispositions'] = dispositions
        attachment['disposition'] = dispositions[0]

    if attachment['disposition'] in [None, "inline"] and attachment['content_type'] in ["text/plain", "text/html"]:
        attachment.content = decode_payload(message_part)

    if content_disposition:
        for param in dispositions[1:]:
            name, value = param.split("=")
            name, value = name.strip().lower(), value.strip().strip('"\'')
            if name == "filename":
                attachment.name = value
            elif name in ["create-date", "creation-date"]:
                attachment.create_date = value
            elif name == "modification-date":
                attachment.mod_date = value
            elif name == "read-date":
                attachment.read_date = value

    return attachment


def to_file_object(attachment):
    """
    Convert an attachment to a file-like object.

    Args:
        attachment (objict): The attachment object.

    Returns:
        StringIO: A StringIO object representing the attachment payload.
    """
    obj = StringIO(to_string(attachment['payload']))
    obj.name = attachment['name']
    obj.size = len(attachment['payload'])  # 'size' is not typically an attribute of StringIO, but is set here for compatibility
    return obj


def parse_addresses(input_string, force_name=False, emails_only=False):
    """
    Parse email addresses from a string.

    Args:
        input_string (str): The input string containing email addresses.
        force_name (bool): Force inclusion of the domain as the name if no name is present.
        emails_only (bool): Return only email addresses if True, otherwise return detailed information.

    Returns:
        list: A list of parsed email information or email addresses.
    """
    if not input_string:
        return []

    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(email_pattern, input_string)

    parsed_emails = []
    for addr in emails:
        addr = addr.strip().lower()
        name_match = re.search(r'([a-zA-Z\s]+)?\s*<{}>'.format(re.escape(addr)), input_string)
        name = name_match.group(1).strip() if name_match and name_match.group(1) else (addr.split('@')[1] if force_name else None)
        full_email = f"{name} <{addr}>" if name else addr
        parsed_emails.append(objict(name=name, email=addr, full_email=full_email))

    return [email.email for email in parsed_emails] if emails_only else parsed_emails


def to_string(value):
    """
    Convert various types of values to a string.

    Args:
        value: The value to be converted.

    Returns:
        str: The string representation of the input value.
    """
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, bytearray):
        return value.decode("utf-8")
    if isinstance(value, (int, float)):
        return str(value)
    return value


def parse_raw_message(msg_obj):
    """
    Parse an email message and return a dictionary with its components.

    Args:
        msg_obj (str or email.message.Message): The email message object or its string representation.

    Returns:
        objict: A dictionary-like object containing email components.
    """
    if isinstance(msg_obj, str):
        msg_obj = email.message_from_string(msg_obj)

    subject, message, body, html = None, None, None, None
    attachments, body_parts, html_parts = [], [], []

    if msg_obj.get('Subject'):
        decoded_fragments = decode_header(msg_obj['Subject'])
        subject = ''.join(
            str(s, enc or 'utf-8', 'replace')
            for s, enc in decoded_fragments
        )

    for part in msg_obj.walk():
        attachment = parse_attachment(part)
        if attachment:
            if attachment.get('content'):
                (html_parts if attachment['content_type'] == "text/html" else body_parts).append(attachment.content)
            else:
                attachments.append(attachment)

    if body_parts:
        body = ''.join(body_parts).strip()
        message = '\n'.join(
            line.strip() for line in body.split('\n') if not line.startswith('>') or (blocks := 0) < 3
        ).strip()

    html = ''.join(html_parts) if html_parts else None

    from_addr = parseaddr(msg_obj.get('From', ''))

    date_time = None
    if msg_obj.get('Date'):
        date_time = parsedate_to_datetime(msg_obj['Date'])
        if date_time:
            date_time = date_time.replace(tzinfo=None)

    return objict({
        'subject': subject.strip() if subject else '',
        'body': body,
        'sent_at': date_time,
        'message': message,
        'html': html,
        'from_email': from_addr[1],
        'from_name': from_addr[0],
        'to': msg_obj.get("To"),
        'to_addrs': getaddresses(msg_obj.get_all("To", [])),
        'cc': msg_obj.get("Cc"),
        'cc_addrs': getaddresses(msg_obj.get_all("Cc", [])),
        'attachments': attachments,
    })
