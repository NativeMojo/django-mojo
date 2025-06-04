from locale import currency
from testit import helpers as th
from testit import faker
import datetime
import os
from objict import objict, nobjict


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
    expected = {'rule_id': 5710, 'level': 5, 'title': 'Attempt to login using a non-existent user'}
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
        assert expected[sec_alert.alert_id] == alert, f"does not match {sec_alert.alert_id}{alert}"


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
