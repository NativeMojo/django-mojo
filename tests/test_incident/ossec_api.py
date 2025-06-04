from locale import currency
from testit import helpers as th
from objict import objict, nobjict
import os
import time

current_dir = os.path.dirname(__file__)
batch_file_path = os.path.join(current_dir, "ossec_tests.json")


@th.unit_test()
def test_ossec_batch_post(opts):
    batch = objict.from_file(batch_file_path)

    data = {"batch": []}
    opts.alert_ids = []
    alert_id = int(time.time()) + 0.01
    for event in batch.tests:
        alert_id += 0.01
        opts.alert_ids.append(alert_id)
        event["alert_id"] = alert_id
        data["batch"].append(event)

    resp = opts.client.post(f"/api/incident/ossec/alert/batch", data)
    assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"


@th.django_unit_test()
def test_validate_batch_post(opts):
    from mojo.apps.incident.models import Event

    for alert_id in opts.alert_ids:
        event = Event.objects.filter(metadata__alert_id=alert_id).last()
        assert event is not None, f"Event with alert_id {alert_id} not found"
