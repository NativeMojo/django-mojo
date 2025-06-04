# parser_core.py
from . import rules, utils
from .parsed import ParsedAlert
from objict import objict

def parse_incoming_alert(data):
    if "batch" in data:
        alerts = []
        for item in data["batch"]:
            alerts.append(parse_incoming_alert(item))
        return alerts

    alert = ParsedAlert(parse_alert_json(data))
    if utils.ignore_alert(alert):
        return None

    details = utils.parse_rule_details(alert.text)
    for key, value in details.items():
        if key not in alert:
            alert[key] = value
    alert.update(details)
    if not alert.title:
        return None

    alert.update(parse_alert_metadata(alert))
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

    if hasattr(data, 'text'):
        data.text = utils.remove_non_ascii(data.text) # .replace('\\/', '/')
    return data
