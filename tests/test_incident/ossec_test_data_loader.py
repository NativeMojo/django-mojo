"""
Utility module for loading and parsing delimited OSSEC test data.

This module provides functions to read the ossec_raw.txt file which contains
real OSSEC alerts in delimited format (=START= ... =END=) and convert them
into structured data for testing.
"""

import os
from pathlib import Path
from typing import List, Dict, Optional


def get_test_data_path() -> Path:
    """Get the path to the ossec_raw.txt test data file."""
    current_dir = Path(__file__).parent
    return current_dir / "ossec_raw.txt"


def load_delimited_alerts() -> List[str]:
    """
    Load all delimited alerts from the ossec_raw.txt file.

    Returns:
        List of clean OSSEC alert strings (without delimiters)
    """
    test_file = get_test_data_path()

    if not test_file.exists():
        raise FileNotFoundError(f"Test data file not found: {test_file}")

    with open(test_file, 'r') as f:
        content = f.read()

    return parse_delimited_content(content)


def parse_delimited_content(content: str) -> List[str]:
    """
    Parse delimited content and extract individual alerts.

    Args:
        content: Raw content from ossec_raw.txt with =START=/=END= delimiters

    Returns:
        List of clean alert strings
    """
    alerts = []

    # Split by =START= and process each section
    sections = content.split('=START=')

    for section in sections:
        if not section.strip():
            continue

        # Remove =END= delimiter and clean up
        clean_section = section.split('=END=')[0].strip()

        if clean_section:
            alerts.append(clean_section)

    return alerts


def load_alerts_by_rule_id() -> Dict[int, List[str]]:
    """
    Load alerts grouped by rule ID.

    Returns:
        Dictionary mapping rule_id -> list of alert strings
    """
    alerts = load_delimited_alerts()
    rule_groups = {}

    for alert in alerts:
        rule_id = extract_rule_id(alert)
        if rule_id:
            if rule_id not in rule_groups:
                rule_groups[rule_id] = []
            rule_groups[rule_id].append(alert)

    return rule_groups


def extract_rule_id(alert_text: str) -> Optional[int]:
    """
    Extract rule ID from an alert text.

    Args:
        alert_text: Clean OSSEC alert text

    Returns:
        Rule ID as integer, or None if not found
    """
    import re

    lines = alert_text.split('\n')
    for line in lines:
        match = re.match(r'Rule: (\d+) \(level \d+\)', line)
        if match:
            return int(match.group(1))

    return None


def get_sample_alert_by_rule(rule_id: int) -> Optional[str]:
    """
    Get a sample alert for a specific rule ID.

    Args:
        rule_id: OSSEC rule ID

    Returns:
        Sample alert text or None if not found
    """
    rule_groups = load_alerts_by_rule_id()
    alerts = rule_groups.get(rule_id, [])
    return alerts[0] if alerts else None


def get_alert_categories() -> Dict[str, List[str]]:
    """
    Get alerts grouped by category.

    Returns:
        Dictionary mapping category -> list of alert strings
    """
    alerts = load_delimited_alerts()
    category_groups = {}

    for alert in alerts:
        categories = extract_categories(alert)
        for category in categories:
            if category not in category_groups:
                category_groups[category] = []
            category_groups[category].append(alert)

    return category_groups


def extract_categories(alert_text: str) -> List[str]:
    """
    Extract categories from alert text.

    Args:
        alert_text: Clean OSSEC alert text

    Returns:
        List of categories
    """
    import re

    lines = alert_text.split('\n')
    if lines:
        # First line format: ** Alert ID: classification - categories,
        match = re.match(r'\*\* Alert [\d.]+: ?(.*?) ?- ?(.*),?', lines[0])
        if match:
            categories_str = match.group(2).strip().rstrip(',')
            return [cat.strip() for cat in categories_str.split(',') if cat.strip()]

    return []


def get_sample_alerts_by_type() -> Dict[str, str]:
    """
    Get sample alerts for different types (web, sudo, syscheck, etc.).

    Returns:
        Dictionary mapping alert_type -> sample alert string
    """
    category_groups = get_alert_categories()
    samples = {}

    # Map categories to types
    type_mappings = {
        'web': ['web', 'accesslog'],
        'sudo': ['sudo'],
        'syscheck': ['syscheck'],
        'pam': ['pam'],
        'ssh': ['sshd', 'ssh'],
        'ossec': ['ossec']
    }

    for alert_type, categories in type_mappings.items():
        for category in categories:
            if category in category_groups and category_groups[category]:
                samples[alert_type] = category_groups[category][0]
                break

    return samples


def get_test_data_stats() -> Dict[str, int]:
    """
    Get statistics about the test data.

    Returns:
        Dictionary with test data statistics
    """
    alerts = load_delimited_alerts()
    rule_groups = load_alerts_by_rule_id()
    category_groups = get_alert_categories()

    return {
        'total_alerts': len(alerts),
        'unique_rules': len(rule_groups),
        'unique_categories': len(category_groups),
        'rule_ids': sorted(rule_groups.keys()),
        'categories': sorted(category_groups.keys())
    }


def create_test_batch(max_alerts: int = 10, rule_filter: Optional[List[int]] = None) -> List[str]:
    """
    Create a test batch of alerts for testing.

    Args:
        max_alerts: Maximum number of alerts to include
        rule_filter: Optional list of rule IDs to filter by

    Returns:
        List of alert strings for testing
    """
    alerts = load_delimited_alerts()

    if rule_filter:
        filtered_alerts = []
        for alert in alerts:
            rule_id = extract_rule_id(alert)
            if rule_id in rule_filter:
                filtered_alerts.append(alert)
        alerts = filtered_alerts

    return alerts[:max_alerts]


# Common test data sets for easy access
def get_web_alerts() -> List[str]:
    """Get all web-related alerts."""
    category_groups = get_alert_categories()
    web_alerts = []
    for category in ['web', 'accesslog']:
        web_alerts.extend(category_groups.get(category, []))
    return web_alerts


def get_sudo_alerts() -> List[str]:
    """Get all sudo-related alerts."""
    category_groups = get_alert_categories()
    return category_groups.get('sudo', [])


def get_syscheck_alerts() -> List[str]:
    """Get all syscheck/file integrity alerts."""
    category_groups = get_alert_categories()
    return category_groups.get('syscheck', [])


def get_pam_alerts() -> List[str]:
    """Get all PAM session alerts."""
    category_groups = get_alert_categories()
    return category_groups.get('pam', [])


# Validation functions
def validate_test_data() -> bool:
    """
    Validate that test data is properly formatted and accessible.

    Returns:
        True if validation passes, False otherwise
    """
    try:
        alerts = load_delimited_alerts()
        if not alerts:
            print("No alerts found in test data")
            return False

        # Check that we have diverse rule types
        rule_groups = load_alerts_by_rule_id()
        if len(rule_groups) < 5:
            print(f"Warning: Only {len(rule_groups)} unique rule types found")

        # Check that basic categories are present
        category_groups = get_alert_categories()
        expected_categories = ['sudo', 'pam', 'syscheck']
        for category in expected_categories:
            if category not in category_groups:
                print(f"Warning: No alerts found for category '{category}'")

        return True

    except Exception as e:
        print(f"Test data validation failed: {e}")
        return False


if __name__ == "__main__":
    # Print test data statistics when run directly
    if validate_test_data():
        stats = get_test_data_stats()
        print("OSSEC Test Data Statistics:")
        print("=" * 30)
        for key, value in stats.items():
            if isinstance(value, list):
                print(f"{key}: {len(value)} items")
                if len(value) <= 20:  # Only show full list if reasonable size
                    print(f"  {value}")
            else:
                print(f"{key}: {value}")
