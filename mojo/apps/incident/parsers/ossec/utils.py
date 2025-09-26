import re
from objict import objict

# -------------------------------
# JSON Parsing & Normalization
# -------------------------------

def remove_non_ascii(input_str, replacement=''):
    """
    Replace all non-ASCII characters and escaped byte sequences with a replacement.
    """
    # Replace escaped byte sequences like \xHH
    cleaned_str = re.sub(r'\\x[0-9a-fA-F]{2}', replacement, input_str)
    return ''.join(
        char if (32 <= ord(char) < 128 or char in '\n\r\t')
        else f"<r{ord(char)}>"
        for char in cleaned_str
    )

def parse_alert_json(data):
    """
    Convert JSON (string or obj) into objict, removing non-ASCII content and normalizing fields.
    """
    try:
        if isinstance(data, str):
            data = objict.from_json(remove_non_ascii(data.replace('\n', '\\n')))
    except Exception:
        data = objict.from_json(remove_non_ascii(data))

    for key in data:
        if isinstance(data[key], str):
            data[key] = data[key].strip()

    if hasattr(data, 'text'):
        data.text = remove_non_ascii(data.text)

    return data

# -------------------------------
# Rule Details Parsing
# -------------------------------

def parse_alert_id(details):
    """
    Extract the alert ID (float string) from a log message.
    """
    match = re.search(r"Alert (\d+\.\d+):", details)
    return match.group(1) if match else ""

def parse_rule_details(details):
    """
    Extract rule_id, level, title, and source_ip from OSSEC alert text.
    """
    # alert_id = parse_alert_id(details)
    rule_pattern = r"Rule: (\d+) \(level (\d+)\) -> '([^']+)'"
    match = re.search(rule_pattern, details)

    result = objict()

    if match:
        result.rule_id = int(match.group(1))
        result.level = int(match.group(2))
        result.title = match.group(3)

    # Extract source IP from "Src IP: x.x.x.x" line
    src_ip_pattern = r"Src IP: (\d+\.\d+\.\d+\.\d+)"
    src_match = re.search(src_ip_pattern, details)
    if src_match:
        result.source_ip = src_match.group(1)

    return result

# -------------------------------
# Generic Pattern Matching
# -------------------------------

DEFAULT_META_PATTERNS = {
    "source_ip": re.compile(r"Src IP: (\S+)"),
    "source_port": re.compile(r"Src Port: (\S+)"),
    "user": re.compile(r"User: (\S+)"),
    "http_path": re.compile(r"request: (\S+ \S+)"),
    "http_server": re.compile(r"server: (\S+),"),
    "http_host": re.compile(r"host: (\S+)"),
    "http_referrer": re.compile(r"referrer: (\S+),"),
    "client": re.compile(r"client: (\S+),"),
    "upstream": re.compile(r"upstream: (\S+),")
}

def match_patterns(patterns, text):
    """
    Apply regex patterns to a given string and return matched named groups.
    """
    return {
        key: match.group(1)
        for key, pattern in patterns.items()
        if (match := pattern.search(text))
    }

# -------------------------------
# NGINX Log Parsing
# -------------------------------

NGINX_PARSE_PATTERN = re.compile(
    r'(?P<source_ip>\d+\.\d+\.\d+\.\d+) - - \[(?P<http_time>.+?)\] '
    r'(?P<http_method>\w+) (?P<http_url>.+?) (?P<http_protocol>[\w/.]+) '
    r'(?P<http_status>\d+) (?P<http_bytes>\d+) (?P<http_referer>.+?) '
    r'(?P<user_agent>.+?) (?P<http_elapsed>\d\.\d{3})'
)

def parse_nginx_line(line):
    """
    Attempt to parse an NGINX access log line into a structured dict.
    Supports multiline input by splitting and scanning each.
    """
    lines = line.splitlines()
    for l in lines:
        match = NGINX_PARSE_PATTERN.match(l)
        if match:
            return match.groupdict()
    return None

def extract_url(text):
    """
    Extracts a full URL from a line that includes an HTTP method and version.
    Example match: 'GET https://example.com/path HTTP/1.1'
    """
    match = re.search(r"(GET|POST|DELETE|PUT)\s+(https?://[^\s]+)\s+HTTP/\d\.\d", text)
    return match.group(2) if match else None

def extract_domain(text):
    """
    Extracts the domain from a full URL (e.g. 'https://example.com/path' → 'example.com').
    """
    match = re.search(r"https?://([^/:]+)", text)
    return match.group(1) if match else None

def extract_ip(text):
    """
    Extracts the first IPv4 address found in a string.
    """
    match = re.search(r"\b((?:[0-9]{1,3}\.){3}[0-9]{1,3})\b", text)
    return match.group(1) if match else None

def extract_url_path(text):
    """
    Extracts the path component from a URL (e.g. 'https://example.com/foo/bar?q=1' → '/foo/bar').
    """
    match = re.search(r"https?://[^/]+(/[^?]*)", text)
    return match.group(1) if match else None

def extract_user_agent(text):
    """
    Attempts to extract a user agent string following a NGINX-style referrer '-' marker.
    Only works for logs like: '... '-' <user agent> 1.234 200'
    """
    match = re.search(r"' - (.+?) \d+\.\d+ \d+'", text)
    return match.group(1) if match else None


IGNORE_RULES = {
    "100020",
}

def ignore_alert(alert):
    """
    Returns True if this alert should be ignored based on known rule exclusions
    or content patterns.
    """
    rule_id = str(alert.rule_id)

    if rule_id in IGNORE_RULES:
        return True

    if rule_id == "510" and "/dev/.mount/utab" in alert.text:
        return True

    return False
