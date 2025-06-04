from testit import helpers as th
import datetime
from objict import objict


@th.django_unit_test()
def test_report_event_without_request(opts):
    from mojo.apps.incident import report_event
    from mojo.apps.incident.models import Event

    # Clear existing events
    Event.objects.filter(category="testing_category").delete()

    # Defined params
    details = "Test event"
    title = "Test Event Title"
    category = "testing_category"
    level = 2
    model_name = "TestModel"
    model_id = 1
    source_ip = "192.168.1.1"

    # Invoke report_event
    report_event(
        details,
        title=title,
        category=category,
        level=level,
        model_name=model_name,
        model_id=model_id,
        source_ip=source_ip
    )

    # Check if event is created
    assert Event.objects.filter(category="testing_category").count() == 1, "Event should be created"
    event = Event.objects.filter(category="testing_category").first()

    # Validate event attributes
    assert event.details == details, "Event details mismatch"
    assert event.title == title, "Event title mismatch"
    assert event.category == category, "Event category mismatch"
    assert event.level == level, "Event level mismatch"
    assert event.model_name == model_name, "Event model_name mismatch"
    assert event.model_id == model_id, "Event model_id mismatch"
    assert event.source_ip == source_ip, "Event source_ip mismatch"


@th.django_unit_test()
def test_report_event_with_request(opts):
    from mojo.apps.incident import report_event
    from mojo.apps.incident.models import Event

    # Clear existing events
    Event.objects.filter(category="testing_category").delete()

    # Create mock request using th.get_mock_request
    request = th.get_mock_request(
        ip="10.0.0.1",
        path="/test/path",
        method="GET",
        META={
            "SERVER_PROTOCOL": "HTTP/1.1",
            "QUERY_STRING": "param=value",
            "HTTP_USER_AGENT": "TestAgent",
            "HTTP_HOST": "localhost"
        }
    )

    # Define params
    details = "Test event with request"
    title = "Request Event Title"
    category = "testing_category"
    level = 3

    # Invoke report_event
    report_event(
        details,
        title=title,
        category=category,
        level=level,
        request=request
    )

    # Check if event is created
    assert Event.objects.filter(category="testing_category").count() == 1, "Event should be created"
    event = Event.objects.filter(category="testing_category").first()

    # Validate event attributes
    assert event.details == details, "Event details mismatch"
    assert event.title == title, "Event title mismatch"
    assert event.category == category, "Event category mismatch"
    assert event.level == level, "Event level mismatch"
    assert event.source_ip == "10.0.0.1", "Event source_ip mismatch"

    # Validate event metadata
    metadata = event.metadata
    assert metadata.get("request_ip") == "10.0.0.1", "Metadata request_ip mismatch"
    assert metadata.get("http_path") == "/test/path", "Metadata http_path mismatch"
    assert metadata.get("http_protocol") == "HTTP/1.1", "Metadata http_protocol mismatch"
    assert metadata.get("http_method") == "GET", "Metadata http_method mismatch"
    assert metadata.get("http_query_string") == "param=value", "Metadata http_query_string mismatch"
    assert metadata.get("http_user_agent") == "TestAgent", "Metadata http_user_agent mismatch"
    assert metadata.get("http_host") == "localhost", "Metadata http_host mismatch"


@th.django_unit_test()
def test_report_event_with_different_request(opts):
    from mojo.apps.incident import report_event
    from mojo.apps.incident.models import Event

    # Clear existing events
    Event.objects.filter(category="another_testing_category").delete()

    # Create different mock request
    request = th.get_mock_request(
        ip="192.168.1.100",
        path="/different/path",
        method="POST",
        META={
            "SERVER_PROTOCOL": "HTTP/2.0",
            "QUERY_STRING": "new_param=1",
            "HTTP_USER_AGENT": "DifferentAgent",
            "HTTP_HOST": "example.com"
        }
    )

    # Define params
    details = "Different test event with request"
    title = "Different Event Title"
    category = "another_testing_category"
    level = 1

    # Invoke report_event
    report_event(
        details,
        title=title,
        category=category,
        level=level,
        request=request
    )

    # Check if event is created
    assert Event.objects.filter(category="another_testing_category").count() == 1, "Event should be created"
    event = Event.objects.filter(category="another_testing_category").first()

    # Validate event attributes
    assert event.details == details, "Event details mismatch"
    assert event.title == title, "Event title mismatch"
    assert event.category == category, "Event category mismatch"
    assert event.level == level, "Event level mismatch"
    assert event.source_ip == "192.168.1.100", "Event source_ip mismatch"

    # Validate event metadata
    metadata = event.metadata
    assert metadata.get("request_ip") == "192.168.1.100", "Metadata request_ip mismatch"
    assert metadata.get("http_path") == "/different/path", "Metadata http_path mismatch"
    assert metadata.get("http_protocol") == "HTTP/2.0", "Metadata http_protocol mismatch"
    assert metadata.get("http_method") == "POST", "Metadata http_method mismatch"
    assert metadata.get("http_query_string") == "new_param=1", "Metadata http_query_string mismatch"
    assert metadata.get("http_user_agent") == "DifferentAgent", "Metadata http_user_agent mismatch"
    assert metadata.get("http_host") == "example.com", "Metadata http_host mismatch"
