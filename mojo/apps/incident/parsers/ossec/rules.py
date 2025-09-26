import re
from .utils import parse_nginx_line, match_patterns, DEFAULT_META_PATTERNS

# ---- Rule-specific handlers ----

def parse_rule_2501(alert):
    match = re.search(r'user (\w+) (\d+\.\d+\.\d+\.\d+) port (\d+)', alert.text)
    if match:
        return dict(username=match.group(1), source_ip=match.group(2), source_port=match.group(3))
    return {}

def parse_rule_31301(alert):
    match = re.search(r'\((?P<error_code>\d+): (?P<error_message>.*?)\)', alert.text)
    data = match_patterns(DEFAULT_META_PATTERNS, alert.text)
    data["action"] = "error"
    if match:
        data.update(match.groupdict())
    return data

def parse_rule_31302(alert):
    return _parse_nginx_error(alert)

def parse_rule_31303(alert):
    return _parse_nginx_error(alert)

def _parse_nginx_error(alert):
    match = re.search(r'\[(warn|crit|error)\].*?: (.*?),', alert.text)
    data = match_patterns(DEFAULT_META_PATTERNS, alert.text)
    if match:
        data["action"] = match.group(1)
        emsg = match.group(2)
        if emsg.startswith("*"):
            emsg = emsg.split(" ", 1)[-1]
        data["error_message"] = emsg
    return data

def parse_rule_551(alert):
    match = re.search(r"Integrity checksum changed for: '(\S+)'", alert.text)
    if match:
        return dict(filename=match.group(1), action="changed")
    return {}

def parse_rule_554(alert):
    match = re.search(r"New file '(\S+)' added", alert.text)
    if match:
        return dict(filename=match.group(1), action="added")
    return {}

def parse_rule_5402(alert):
    match = re.search(r'(?P<username>[\w-]+) : PWD=(?P<pwd>\S+) ; USER=(?P<user>\w+) ; COMMAND=(?P<command>.+)', alert.text)
    if match:
        return match.groupdict()
    match = re.search(r'(?P<username>[\w-]+) : TTY=(?P<tty>\S+) ; PWD=(?P<pwd>\S+) ; USER=(?P<user>\w+) ; COMMAND=(?P<command>.+)', alert.text)
    if match:
        return match.groupdict()
    return {}

def parse_rule_5501(alert):
    return _parse_session_action(alert)

def parse_rule_5502(alert):
    return _parse_session_action(alert)

def _parse_session_action(alert):
    match = re.search(r"session (?P<action>\S+) for user (?P<username>\S+)*", alert.text)
    if match:
        return match.groupdict()
    return {}

def parse_rule_5704(alert):
    return _parse_src_ip_port(alert)

def parse_rule_5705(alert):
    return _parse_src_ip_port(alert)

def _parse_src_ip_port(alert):
    match = re.search(r"(?P<source_ip>\d{1,3}(?:\.\d{1,3}){3}) port (?P<source_port>\d+)", alert.text)
    if match:
        return match.groupdict()
    return {}

def parse_rule_5715(alert):
    match = re.search(r'Accepted publickey for (?P<username>\S+) from (?P<source_ip>\d+\.\d+\.\d+\.\d+) .*: (?P<ssh_key_type>\S+) (?P<ssh_signature>\S+)', alert.text)
    if match:
        return match.groupdict()
    return {}

def parse_rule_2932(alert):
    match = re.search(r"Installed: (\S+)", alert.text)
    if match:
        return dict(package=match.group(1))
    return {}

# ---- Prefix-based default handlers ----

def parse_rule_311_default(alert):
    data = parse_nginx_line(alert.text)
    return data if data else {}



def update_rule_2501(alert, geoip=None):
    alert.title = f"SSH Auth Attempt {alert.username}@{alert.hostname} from {alert.source_ip}"

def update_rule_2503(alert, geoip=None):
    alert.title = f"SSH Auth Blocked from {alert.source_ip}"

def update_rule_31101(alert, geoip=None):
    # Extract HTTP details if not already present
    if not hasattr(alert, 'http_status') or not alert.http_status:
        # Extract from NGINX log format in the text
        nginx_match = re.search(
            r'(\d+\.\d+\.\d+\.\d+) - - \[([^\]]+)\] "(\w+) ([^"]+) ([^"]+)" (\d+) (\d+)',
            alert.text
        )
        if nginx_match:
            alert.http_method = nginx_match.group(3)
            alert.http_url = nginx_match.group(4)
            alert.http_status = int(nginx_match.group(6))

    # Use source_ip consistently
    source_ip = getattr(alert, 'source_ip', getattr(alert, 'src_ip', 'Unknown'))
    http_status = getattr(alert, 'http_status', '???')
    http_method = getattr(alert, 'http_method', '???')
    http_url = getattr(alert, 'http_url', '???')

    # Truncate URL if too long
    if hasattr(alert, 'truncate_str') and len(str(http_url)) > 50:
        http_url = alert.truncate_str(str(http_url), 50)

    alert.title = f"Web {http_status} {http_method} {http_url} from {source_ip}"

def update_rule_31104(alert, geoip=None):
    # Extract HTTP details if not already present (same logic as 31101)
    if not hasattr(alert, 'http_status') or not alert.http_status:
        nginx_match = re.search(
            r'(\d+\.\d+\.\d+\.\d+) - - \[([^\]]+)\] "(\w+) ([^"]+) ([^"]+)" (\d+) (\d+)',
            alert.text
        )
        if nginx_match:
            alert.http_method = nginx_match.group(3)
            alert.http_url = nginx_match.group(4)
            alert.http_status = int(nginx_match.group(6))

    source_ip = getattr(alert, 'source_ip', getattr(alert, 'src_ip', 'Unknown'))
    http_status = getattr(alert, 'http_status', '???')
    http_method = getattr(alert, 'http_method', '???')
    http_url = getattr(alert, 'http_url', '???')

    if hasattr(alert, 'truncate_str') and len(str(http_url)) > 50:
        http_url = alert.truncate_str(str(http_url), 50)

    alert.title = f"Web Attack {http_status} {http_method} {http_url} from {source_ip}"

def update_rule_31111(alert, geoip=None):
    # Extract HTTP details if not already present
    if not hasattr(alert, 'http_status') or not alert.http_status:
        nginx_match = re.search(
            r'(\d+\.\d+\.\d+\.\d+) - - \[([^\]]+)\] "(\w+) ([^"]+) ([^"]+)" (\d+) (\d+)',
            alert.text
        )
        if nginx_match:
            alert.http_method = nginx_match.group(3)
            alert.http_url = nginx_match.group(4)
            alert.http_status = int(nginx_match.group(6))

    source_ip = getattr(alert, 'source_ip', getattr(alert, 'src_ip', 'Unknown'))
    http_status = getattr(alert, 'http_status', '???')
    http_method = getattr(alert, 'http_method', '???')
    http_url = getattr(alert, 'http_url', '???')

    if hasattr(alert, 'truncate_str') and len(str(http_url)) > 50:
        http_url = alert.truncate_str(str(http_url), 50)

    if geoip and geoip.isp:
        alert.title = f"No referrer for .js - {http_status} {http_method} {http_url} from {source_ip}({geoip.isp})"
    else:
        alert.title = f"No referrer for .js - {http_status} {http_method} {http_url} from {source_ip}"

def update_rule_311_default(alert, geoip=None):
    # Extract HTTP details if not already present
    if not hasattr(alert, 'http_status') or not alert.http_status:
        nginx_match = re.search(
            r'(\d+\.\d+\.\d+\.\d+) - - \[([^\]]+)\] "(\w+) ([^"]+) ([^"]+)" (\d+) (\d+)',
            alert.text
        )
        if nginx_match:
            alert.http_method = nginx_match.group(3)
            alert.http_url = nginx_match.group(4)
            alert.http_status = int(nginx_match.group(6))

    # Check if we have required fields
    if not hasattr(alert, 'http_status') or not alert.http_status:
        return

    source_ip = getattr(alert, 'source_ip', getattr(alert, 'src_ip', 'Unknown'))
    http_status = getattr(alert, 'http_status', '???')
    http_method = getattr(alert, 'http_method', '???')
    http_url = getattr(alert, 'http_url', '???')

    url = alert.truncate_str(str(http_url), 50) if hasattr(alert, 'truncate_str') else str(http_url)[:50]
    alert.title = f"Web {http_status} {http_method} {url} from {source_ip}"
