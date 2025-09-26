import re
from objict import objict
from .parsed import ParsedAlert

def parse_delimited_ossec_batch(text):
    """
    Parse OSSEC alerts from delimited text format (=START= ... =END=).

    Args:
        text: String containing one or more delimited OSSEC alerts

    Returns:
        List of ParsedAlert objects
    """
    alerts = []

    # Split by =START= delimiter and process each section
    sections = text.split('=START=')

    for section in sections:
        if not section.strip():
            continue

        # Remove =END= delimiter if present
        clean_section = section.split('=END=')[0].strip()

        if clean_section:
            parsed_alert = parse_clean_ossec_alert(clean_section)
            if parsed_alert:
                alerts.append(parsed_alert)

    return alerts

def parse_clean_ossec_alert(text):
    """
    Parse a single clean OSSEC alert text into structured data.

    Args:
        text: Clean OSSEC alert text (no escaping, clean newlines)

    Returns:
        objict with parsed alert data or None if parsing fails
    """
    if not text or not text.strip():
        return None

    lines = text.strip().split('\n')

    if not lines or not lines[0].startswith('** Alert '):
        return None

    # Parse the first line: ** Alert 1758225773.31821: - ossec,syscheck,
    alert_line = lines[0]
    alert_match = re.match(r'\*\* Alert ([\d.]+): ?(.*?) ?- ?(.*),?', alert_line)

    if not alert_match:
        return None

    alert_id = alert_match.group(1)
    classification = alert_match.group(2).strip()
    categories_str = alert_match.group(3).strip().rstrip(',')
    categories = [cat.strip() for cat in categories_str.split(',') if cat.strip()]

    # Initialize alert data
    alert_data = objict({
        'alert_id': alert_id,
        'classification': classification,
        'categories': categories,
        'text': text
    })

    current_line_idx = 1

    # Parse timestamp and hostname line
    if current_line_idx < len(lines):
        timestamp_line = lines[current_line_idx]

        # Single pattern to handle both: hostname->service or hostname->/path/to/log
        timestamp_match = re.match(r'(\d{4} \w{3} \d{1,2} \d{2}:\d{2}:\d{2}) (.*?)->(.*)', timestamp_line)

        if timestamp_match:
            alert_data.timestamp = timestamp_match.group(1)
            alert_data.hostname = timestamp_match.group(2)
            target = timestamp_match.group(3).strip()

            # Determine if target is a log file path or service name
            if target.startswith('/'):
                alert_data.log_file = target
            else:
                alert_data.service = target
            current_line_idx += 1

    # Parse rule line: Rule: 554 (level 5) -> 'File added to the system.'
    if current_line_idx < len(lines):
        rule_line = lines[current_line_idx]
        rule_match = re.match(r'Rule: (\d+) \(level (\d+)\) -> [\'"]([^\'"]+)[\'"]', rule_line)
        if rule_match:
            alert_data.rule_id = int(rule_match.group(1))
            alert_data.level = int(rule_match.group(2))
            alert_data.title = rule_match.group(3)
            current_line_idx += 1

    # Parse additional field lines (Src IP:, User:, etc.)
    while current_line_idx < len(lines):
        line = lines[current_line_idx]

        # Check for field patterns like "Src IP: 1.2.3.4" or "User: username"
        field_match = re.match(r'([A-Za-z][A-Za-z ]+): (.+)', line)
        if field_match:
            field_name = field_match.group(1).strip()
            field_value = field_match.group(2).strip()

            # Map common field names to standardized keys
            field_mapping = {
                'Src IP': 'source_ip',
                'Source IP': 'source_ip',
                'Src Port': 'source_port',
                'User': 'username',
                'Dst IP': 'dest_ip',
                'New file': 'filename',
                'File': 'filename'
            }

            key = field_mapping.get(field_name, field_name.lower().replace(' ', '_'))
            alert_data[key] = field_value
            current_line_idx += 1
        else:
            # This line doesn't match field pattern, it's probably the log message
            break

    # Remaining lines are the log message
    if current_line_idx < len(lines):
        log_message_lines = lines[current_line_idx:]
        alert_data.log_message = '\n'.join(log_message_lines)

        # Parse additional data from log message
        _parse_log_message_content(alert_data)

    return alert_data

def _parse_log_message_content(alert_data):
    """
    Extract additional fields from log message content.
    """
    if not hasattr(alert_data, 'log_message') or not alert_data.log_message:
        return

    log_msg = alert_data.log_message

    # Parse NGINX access log format
    nginx_match = re.search(
        r'(\d+\.\d+\.\d+\.\d+) - - \[([^\]]+)\] "(\w+) ([^"]+) ([^"]+)" (\d+) (\d+) "([^"]*)" "([^"]*)"(?:\s+([\d.]+))?(?:\s+(\d+))?',
        log_msg
    )

    if nginx_match:
        alert_data.source_ip = nginx_match.group(1)
        alert_data.timestamp_log = nginx_match.group(2)
        alert_data.http_method = nginx_match.group(3)
        alert_data.http_url = nginx_match.group(4)
        alert_data.http_protocol = nginx_match.group(5)
        alert_data.http_status = int(nginx_match.group(6))
        alert_data.http_bytes = int(nginx_match.group(7))
        alert_data.http_referrer = nginx_match.group(8) if nginx_match.group(8) != '-' else None
        alert_data.user_agent = nginx_match.group(9) if nginx_match.group(9) != '-' else None

        if nginx_match.group(10):  # response time
            alert_data.http_response_time = float(nginx_match.group(10))
        if nginx_match.group(11):  # port
            alert_data.http_port = int(nginx_match.group(11))
        return

    # Parse sudo command format
    sudo_match = re.search(
        r'(\w+) : (?:TTY=(\S+) ; )?PWD=(\S+) ; USER=(\w+) ; COMMAND=(.+)',
        log_msg
    )

    if sudo_match:
        alert_data.username = sudo_match.group(1)
        if sudo_match.group(2):
            alert_data.tty = sudo_match.group(2)
        alert_data.pwd = sudo_match.group(3)
        alert_data.target_user = sudo_match.group(4)
        alert_data.command = sudo_match.group(5)
        return

    # Parse PAM session logs
    pam_match = re.search(
        r'pam_unix\(([^)]+)\): session (opened|closed) for user (\w+)(?:\(uid=(\d+)\))?',
        log_msg
    )

    if pam_match:
        alert_data.pam_service = pam_match.group(1)
        alert_data.session_action = pam_match.group(2)
        alert_data.username = pam_match.group(3)
        if pam_match.group(4):
            alert_data.uid = int(pam_match.group(4))
        return

    # Parse SSH logs
    ssh_match = re.search(
        r'Accepted (\w+) for (\w+) from ([\d.]+) port (\d+)',
        log_msg
    )

    if ssh_match:
        alert_data.auth_method = ssh_match.group(1)
        alert_data.username = ssh_match.group(2)
        alert_data.source_ip = ssh_match.group(3)
        alert_data.source_port = int(ssh_match.group(4))
        return

    # Parse file checksums (for syscheck)
    if 'syscheck' in alert_data.get('categories', []):
        # Parse MD5 checksums
        md5_match = re.search(r'md5sum.*?: [\'"]([a-f0-9]{32})[\'"]', log_msg)
        if md5_match:
            alert_data.md5sum = md5_match.group(1)

        # Parse SHA1 checksums
        sha1_match = re.search(r'sha1sum.*?: [\'"]([a-f0-9]{40})[\'"]', log_msg)
        if sha1_match:
            alert_data.sha1sum = sha1_match.group(1)

        # Parse file path if not already set
        if not hasattr(alert_data, 'filename'):
            file_match = re.search(r"'(/[^']+)'", log_msg)
            if file_match:
                alert_data.filename = file_match.group(1)

def parse_incoming_clean_alert(data):
    """
    Main entry point for parsing clean OSSEC data.

    Args:
        data: String or list of strings containing clean OSSEC alerts

    Returns:
        ParsedAlert object or list of ParsedAlert objects
    """
    if isinstance(data, list):
        # Handle list of clean alert strings
        alerts = []
        for item in data:
            if isinstance(item, str):
                parsed_alerts = parse_delimited_ossec_batch(item)
                alerts.extend(parsed_alerts)

        # Convert to ParsedAlert objects and apply processing
        processed_alerts = []
        for alert_data in alerts:
            if alert_data:
                parsed_alert = ParsedAlert(alert_data)
                processed_alert = _apply_alert_processing(parsed_alert)
                if processed_alert:
                    processed_alerts.append(processed_alert)

        return processed_alerts

    elif isinstance(data, str):
        # Handle single string (may contain multiple delimited alerts)
        alert_data_list = parse_delimited_ossec_batch(data)

        if len(alert_data_list) == 1:
            # Single alert
            alert_data = alert_data_list[0]
            parsed_alert = ParsedAlert(alert_data)
            return _apply_alert_processing(parsed_alert)

        elif len(alert_data_list) > 1:
            # Multiple alerts
            processed_alerts = []
            for alert_data in alert_data_list:
                parsed_alert = ParsedAlert(alert_data)
                processed_alert = _apply_alert_processing(parsed_alert)
                if processed_alert:
                    processed_alerts.append(processed_alert)
            return processed_alerts

    return None

def _apply_alert_processing(alert):
    """
    Apply the same processing pipeline as other parsers.
    """
    if not alert:
        return None

    # Import here to avoid circular imports
    from . import utils
    from .core import parse_alert_metadata, update_by_rule

    # Check if alert should be ignored
    if utils.ignore_alert(alert):
        return None

    # Normalize fields
    alert.normalize_fields()

    # Apply rule-specific metadata parsing
    metadata = parse_alert_metadata(alert)
    if metadata:
        alert.update(metadata)

    # Check title requirement
    if not getattr(alert, 'title', None):
        return None

    # Apply rule-specific updates
    update_by_rule(alert)

    return alert
