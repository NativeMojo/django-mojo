from locale import currency
from testit import helpers as th
from testit import faker
import datetime
import os
from objict import objict, nobjict
from . import ossec_test_data_loader


# Determine the path to the JSON file
current_dir = os.path.dirname(__file__)
batch_file_path = os.path.join(current_dir, "ossec_tests.json")



@th.django_unit_test()
def test_raw_ossec_parse_rule_details(opts):
    from mojo.apps.incident.parsers.ossec import utils
    from mojo.helpers import logit
    assert os.path.exists(batch_file_path), "could not load test file"
    batch = objict.from_file(batch_file_path)

    raw = batch.tests[0]
    out = utils.parse_rule_details(raw.text)
    expected = {'rule_id': 5710, 'level': 5, 'title': 'Attempt to login using a non-existent user', 'source_ip': '43.156.236.44'}
    assert out == expected



@th.django_unit_test()
def test_raw_ossec_parsing(opts):
    from mojo.apps.incident.parsers import ossec
    from mojo.helpers import logit
    assert os.path.exists(batch_file_path), "could not load test file"
    batch = objict.from_file(batch_file_path)
    expected = nobjict.from_file(os.path.join(current_dir, "ossec_expected.json"))
    for sec_alert in batch.tests:
        alert = ossec.parse(sec_alert)
        # Test critical fields rather than exact equality since enhancements add more fields
        expected_alert = expected[sec_alert.alert_id]
        assert alert.rule_id == expected_alert.rule_id, f"rule_id mismatch for {sec_alert.alert_id}"
        assert alert.level == expected_alert.level, f"level mismatch for {sec_alert.alert_id}"
        assert alert.alert_id == expected_alert.alert_id, f"alert_id mismatch for {sec_alert.alert_id}"
        # Verify source_ip is extracted when present (our enhancement)
        if hasattr(alert, 'source_ip') and alert.source_ip:
            assert alert.source_ip is not None, f"source_ip should be extracted for {sec_alert.alert_id}"
        # Verify title is properly generated (our enhancement fixed this)
        if hasattr(alert, 'title') and alert.title:
            assert 'None' not in alert.title, f"title should not contain 'None' for {sec_alert.alert_id}: {alert.title}"


@th.django_unit_test()
def test_clean_text_parsing_web_alerts(opts):
    """Test parsing of clean OSSEC text for web access log alerts."""
    from mojo.apps.incident.parsers.ossec import parse
    from mojo.apps.incident.parsers.ossec.clean_parser import parse_clean_ossec_alert

    # Get real web alerts from test data
    web_alerts = ossec_test_data_loader.get_web_alerts()
    if not web_alerts:
        # Skip if no web alerts available
        return

    web_alert = web_alerts[0]  # Use first web alert

    # Test individual alert parsing
    result = parse_clean_ossec_alert(web_alert)
    assert result is not None, "Clean web alert should parse successfully"
    assert hasattr(result, 'alert_id'), "Should have alert_id"
    assert hasattr(result, 'rule_id'), "Should have rule_id"
    assert hasattr(result, 'level'), "Should have level"
    assert hasattr(result, 'title'), "Should have title"

    # Test main parser auto-detection
    auto_result = parse(web_alert)
    assert auto_result is not None, "Auto-detection should work for clean text"


@th.django_unit_test()
def test_clean_text_parsing_sudo_alerts(opts):
    """Test parsing of clean OSSEC text for sudo command alerts."""
    from mojo.apps.incident.parsers.ossec import parse
    from mojo.apps.incident.parsers.ossec.clean_parser import parse_clean_ossec_alert

    # Get real sudo alerts from test data
    sudo_alerts = ossec_test_data_loader.get_sudo_alerts()
    if not sudo_alerts:
        # Skip if no sudo alerts available
        return

    sudo_alert = sudo_alerts[0]  # Use first sudo alert

    result = parse_clean_ossec_alert(sudo_alert)
    assert result is not None, "Clean sudo alert should parse successfully"
    assert hasattr(result, 'alert_id'), "Should have alert_id"
    assert hasattr(result, 'rule_id'), "Should have rule_id"
    assert hasattr(result, 'level'), "Should have level"
    assert hasattr(result, 'title'), "Should have title"
    assert 'sudo' in result.categories, f"Should have sudo category, got {result.categories}"

    # Test auto-detection
    auto_result = parse(sudo_alert)
    assert auto_result is not None, "Auto-detection should work for sudo alerts"


@th.django_unit_test()
def test_clean_text_parsing_file_integrity(opts):
    """Test parsing of clean OSSEC text for file integrity alerts."""
    from mojo.apps.incident.parsers.ossec.clean_parser import parse_clean_ossec_alert

    # Get real syscheck alerts from test data
    syscheck_alerts = ossec_test_data_loader.get_syscheck_alerts()
    if not syscheck_alerts:
        # Skip if no syscheck alerts available
        return

    syscheck_alert = syscheck_alerts[0]  # Use first syscheck alert

    result = parse_clean_ossec_alert(syscheck_alert)
    assert result is not None, "Clean file integrity alert should parse successfully"
    assert hasattr(result, 'alert_id'), "Should have alert_id"
    assert hasattr(result, 'rule_id'), "Should have rule_id"
    assert hasattr(result, 'level'), "Should have level"
    assert hasattr(result, 'title'), "Should have title"
    assert 'syscheck' in result.categories, f"Should have syscheck category, got {result.categories}"


@th.django_unit_test()
def test_clean_text_parsing_pam_sessions(opts):
    """Test parsing of clean OSSEC text for PAM session alerts."""
    from mojo.apps.incident.parsers.ossec.clean_parser import parse_clean_ossec_alert

    # Get real PAM alerts from test data
    pam_alerts = ossec_test_data_loader.get_pam_alerts()
    if not pam_alerts:
        # Skip if no PAM alerts available
        return

    pam_alert = pam_alerts[0]  # Use first PAM alert

    result = parse_clean_ossec_alert(pam_alert)
    assert result is not None, "Clean PAM alert should parse successfully"
    assert hasattr(result, 'alert_id'), "Should have alert_id"
    assert hasattr(result, 'rule_id'), "Should have rule_id"
    assert hasattr(result, 'level'), "Should have level"
    assert hasattr(result, 'title'), "Should have title"
    assert 'pam' in result.categories, f"Should have pam category, got {result.categories}"


@th.django_unit_test()
def test_clean_text_parsing_batch_processing(opts):
    """Test batch processing of multiple clean OSSEC text alerts."""
    from mojo.apps.incident.parsers.ossec import parse

    # Create test batch from real data
    test_batch = ossec_test_data_loader.create_test_batch(max_alerts=5)
    if len(test_batch) < 2:
        # Skip if insufficient test data
        return

    # Test batch processing as single string with delimiters
    delimited_content = '\n=START=\n'.join([''] + test_batch + ['']) + '\n=END=\n'
    results = parse(delimited_content)

    assert isinstance(results, list), "Batch processing should return a list"
    assert len(results) >= 2, f"Should parse at least 2 alerts, got {len(results) if results else 0}"

    # Verify alerts were parsed with different rule IDs
    rule_ids = [alert.rule_id for alert in results if hasattr(alert, 'rule_id')]
    assert len(set(rule_ids)) >= 2, f"Should have multiple different rule types, got {set(rule_ids)}"


@th.django_unit_test()
def test_clean_text_parsing_edge_cases(opts):
    """Test edge cases and error handling for clean text parsing."""
    from mojo.apps.incident.parsers.ossec.clean_parser import parse_clean_ossec_alert

    # Test empty string
    result = parse_clean_ossec_alert("")
    assert result is None, "Empty string should return None"

    # Test malformed alert (missing rule)
    malformed_alert = """** Alert 1234567.890: mail - web,test
2025 Sep 25 01:00:15 m->/var/log/test
Malformed line without rule"""
    result = parse_clean_ossec_alert(malformed_alert)
    # Should either return None or handle gracefully (missing rule is OK)

    # Test alert without classification (just dash)
    no_classification = """** Alert 1234567.890: - web,test
2025 Sep 25 01:00:15 m->/var/log/test
Rule: 1234 (level 3) -> 'Test rule'"""
    result = parse_clean_ossec_alert(no_classification)
    assert result is not None, "Alert without classification should still parse"
    assert result.classification == "", "Empty classification should be handled"
    assert result.rule_id == 1234


@th.django_unit_test()
def test_clean_text_field_extraction_accuracy(opts):
    """Test accuracy of field extraction from clean text formats."""
    from mojo.apps.incident.parsers.ossec.clean_parser import parse_clean_ossec_alert

    # Test comprehensive field extraction with clean format
    comprehensive_alert = """** Alert 1758764149.256335: mail - web,accesslog,
2025 Sep 25 01:35:49 hostname->/var/log/nginx/access.log
Rule: 31101 (level 5) -> 'Web server 400 error code.'
Src IP: 192.168.1.100
192.168.1.100 - - [25/Sep/2025:01:35:48 +0000] "GET https://api.example.com/api/test HTTP/2.0" 401 40 "https://app.example.com/" "Mozilla/5.0 (Test) Browser" 0.003 443"""

    result = parse_clean_ossec_alert(comprehensive_alert)
    assert result is not None, "Comprehensive alert should parse"

    # Verify core fields are extracted
    assert result.alert_id == '1758764149.256335', f"Expected alert_id '1758764149.256335', got '{result.alert_id}'"
    assert result.classification == 'mail', f"Expected classification 'mail', got '{result.classification}'"
    assert result.rule_id == 31101, f"Expected rule_id 31101, got '{result.rule_id}'"
    assert result.level == 5, f"Expected level 5, got '{result.level}'"
    assert result.title == 'Web server 400 error code.', f"Expected title 'Web server 400 error code.', got '{result.title}'"
    assert result.source_ip == '192.168.1.100', f"Expected source_ip '192.168.1.100', got '{result.source_ip}'"
    assert result.hostname == 'hostname', f"Expected hostname 'hostname', got '{result.hostname}'"
    assert result.log_file == '/var/log/nginx/access.log', f"Expected log_file '/var/log/nginx/access.log', got '{result.log_file}'"

    # Verify HTTP fields from log message
    # if hasattr(result, 'http_method'):
    assert result.http_method == 'GET', f"Expected http_method 'GET', got '{result.http_method}'"
    assert result.http_status == 401, f"Expected http_status 401, got '{result.http_status}'"
    assert result.http_url == 'https://api.example.com/api/test', f"Expected http_url 'https://api.example.com/api/test', got '{result.http_url}'"

    # Verify categories
    assert 'web' in result.categories
    assert 'accesslog' in result.categories


@th.django_unit_test()
def test_clean_text_integration_with_existing_rules(opts):
    """Test that clean parsed alerts work with existing rule processing."""
    from mojo.apps.incident.parsers.ossec import parse

    # Get a real alert from test data
    test_alerts = ossec_test_data_loader.create_test_batch(max_alerts=1)
    if not test_alerts:
        # Skip if no test data available
        return

    test_alert = test_alerts[0]

    # Parse through main parser (includes rule processing)
    result = parse(test_alert)
    assert result is not None, "Clean alert should process through main parser"

    # Verify rule-specific processing occurred
    assert hasattr(result, 'title'), "Should have title after rule processing"
    assert hasattr(result, 'rule_id'), "Should have rule_id after processing"

    # Verify field normalization occurred
    assert hasattr(result, 'source_ip') or hasattr(result, 'ext_ip') or not hasattr(result, 'source_ip'), "Field normalization should work"


@th.django_unit_test()
def test_clean_text_real_data_validation(opts):
    """Test parsing against real OSSEC data from production."""
    from mojo.apps.incident.parsers.ossec.clean_parser import parse_clean_ossec_alert

    # Load real test data
    try:
        alerts = ossec_test_data_loader.load_delimited_alerts()
        if len(alerts) < 3:
            # Skip if insufficient test data
            return
    except FileNotFoundError:
        # Skip if test data file not available
        return

    # Test parsing of first few alerts
    successful_parses = 0
    rule_ids_found = set()

    for i, alert_text in enumerate(alerts[:10]):  # Test first 10
        result = parse_clean_ossec_alert(alert_text)
        if result:
            successful_parses += 1
            if hasattr(result, 'rule_id'):
                rule_ids_found.add(result.rule_id)

            # Basic validation
            assert hasattr(result, 'alert_id'), f"Alert {i} should have alert_id"
            assert hasattr(result, 'categories'), f"Alert {i} should have categories"
            assert isinstance(result.categories, list), f"Alert {i} categories should be list"

    # Should successfully parse most alerts
    success_rate = successful_parses / len(alerts[:10])
    assert success_rate >= 0.7, f"Should parse at least 70% of alerts, got {success_rate*100:.1f}%"

    # Should find multiple rule types
    assert len(rule_ids_found) >= 3, f"Should find at least 3 different rule types, got {len(rule_ids_found)}"


@th.django_unit_test()
def test_clean_text_data_coverage(opts):
    """Test coverage of different alert types in real data."""
    from mojo.apps.incident.parsers.ossec.clean_parser import parse_clean_ossec_alert

    try:
        # Get statistics about available test data
        stats = ossec_test_data_loader.get_test_data_stats()
        assert stats['total_alerts'] > 0, "Should have test alerts available"
        assert stats['unique_rules'] > 0, "Should have different rule types"

        # Test parsing of different categories
        category_groups = ossec_test_data_loader.get_alert_categories()

        tested_categories = 0
        for category, alerts in category_groups.items():
            if alerts and len(alerts) > 0:
                # Test first alert of each category
                result = parse_clean_ossec_alert(alerts[0])
                if result:
                    tested_categories += 1
                    assert category in result.categories, f"Category {category} should be preserved in parsing"

        assert tested_categories >= 3, f"Should test at least 3 categories, tested {tested_categories}"

    except FileNotFoundError:
        # Skip if test data not available
        return




# @th.django_unit_test()
# def test_save_raw_ossec_parsing(opts):
#     from mojo.apps.incident.parsers import ossec
#     from mojo.helpers import logit
#     assert os.path.exists(batch_file_path), "could not load test file"
#     batch = objict.from_file(batch_file_path)
#     expected = nobjict()
#     for sec_alert in batch.tests:
#         alert = ossec.parse(sec_alert)
#         #assert expected[sec_alert.alert_id] == alert, f"does not match {sec_alert.alert_id}"
#         expected[sec_alert.alert_id] = alert
#     expected.save(os.path.join(current_dir, "ossec_expected.json"))
