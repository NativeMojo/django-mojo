# parser_core.py
from . import rules, utils
from .parsed import ParsedAlert

from .clean_parser import parse_incoming_clean_alert
from objict import objict

def parse_incoming_alert(data):
    # Detect format: clean OSSEC text or JSON
    if isinstance(data, str):
        data_stripped = data.strip()

        # Check for clean format (delimited or direct)
        if "=START=" in data_stripped or data_stripped.startswith("** Alert"):
            return parse_incoming_clean_alert(data)

    if isinstance(data, list):
        # List of clean OSSEC strings
        if data and isinstance(data[0], str):
            return parse_incoming_clean_alert(data)

    # Original JSON format handling
    if isinstance(data, dict) and "batch" in data:
        alerts = []
        for item in data["batch"]:
            alerts.append(parse_incoming_alert(item))
        return alerts

    # Handle JSON format - use clean parser on the text field for detailed extraction
    json_alert = parse_alert_json(data)

    # If the JSON has a text field with OSSEC alert content, parse it with clean parser
    if hasattr(json_alert, 'text') and json_alert.text and json_alert.text.strip().startswith('** Alert'):
        # Parse the text content using clean parser for detailed field extraction
        from .clean_parser import parse_clean_ossec_alert
        clean_parsed = parse_clean_ossec_alert(json_alert.text)

        if clean_parsed:
            # Start with clean parser results (more detailed)
            alert = ParsedAlert(clean_parsed)

            # Override with any JSON fields that aren't null/empty
            for key, value in json_alert.items():
                if value is not None and value != "" and value != "-":
                    alert[key] = value
        else:
            # Fallback to JSON-only parsing if clean parser fails
            alert = ParsedAlert(json_alert)
    else:
        # No text field or doesn't look like OSSEC format, use JSON-only parsing
        alert = ParsedAlert(json_alert)

        # Extract what we can from text field if it exists
        if hasattr(alert, 'text') and alert.text:
            details = utils.parse_rule_details(alert.text)
            alert.update(details)

    if utils.ignore_alert(alert):
        return None

    if not getattr(alert, 'title', None):
        return None

    # Apply rule-specific processing
    metadata = parse_alert_metadata(alert)
    if metadata:
        alert.update(metadata)

    alert.normalize_fields()
    update_by_rule(alert)

    return alert


def parse_alert_metadata(alert):
    rule_id = str(alert.rule_id)

    # Try exact match first: parse_rule_2501
    func_name = f"parse_rule_{rule_id}"
    if hasattr(rules, func_name):
        return getattr(rules, func_name)(alert)

    # Optional: try prefix-based match
    for i in range(len(rule_id), 1, -1):  # e.g., 31151 → 3115 → 311 → 31
        fallback_func = f"parse_rule_{rule_id[:i]}_default"
        if hasattr(rules, fallback_func):
            return getattr(rules, fallback_func)(alert)

    # Fallback to generic matching
    return utils.match_patterns(utils.DEFAULT_META_PATTERNS, alert.text)


def update_by_rule(alert, geoip=None):
    rule_id = str(alert.rule_id)
    func_name = f"update_rule_{rule_id}"
    if hasattr(rules, func_name):
        getattr(rules, func_name)(alert, geoip)
        return

    for i in range(len(rule_id), 1, -1):
        fallback_func = f"update_rule_{rule_id[:i]}_default"
        if hasattr(rules, fallback_func):
            getattr(rules, fallback_func)(alert, geoip)
            return

    if hasattr(alert, 'source_ip') and alert.source_ip and alert.source_ip not in getattr(alert, 'title', ''):
        alert.title = f"{alert.title} Source IP: {alert.source_ip}"

    if hasattr(alert, 'title'):
        alert.truncate('title')



def parse_alert_json(data):
    if isinstance(data, str):
        data = objict.from_json(utils.remove_non_ascii(data.replace('\n', '\\n')))

    for key in data:
        if isinstance(data[key], str):
            data[key] = data[key].strip() # .replace('\\/', '/')

    if hasattr(data, 'text') and data.text:
        data.text = utils.remove_non_ascii(data.text) # .replace('\\/', '/')
    return data
