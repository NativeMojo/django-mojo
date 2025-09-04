"""
Webhook Examples for Django-MOJO Jobs System

Examples showing how to use the new publish_webhook() function for
sending HTTP POST webhooks with proper retry logic and error handling.
"""
from datetime import datetime, timedelta
from mojo.apps.jobs import publish_webhook


def example_basic_webhook():
    """Basic webhook example - send user signup data to external API."""

    job_id = publish_webhook(
        url="https://api.example.com/webhooks/user-signup",
        data={
            "user_id": 123,
            "email": "user@example.com",
            "event": "signup",
            "timestamp": datetime.now().isoformat()
        }
    )

    print(f"Webhook job {job_id} queued to webhooks channel")
    return job_id


def example_webhook_with_auth():
    """Webhook with authentication headers."""

    job_id = publish_webhook(
        url="https://secure-api.example.com/webhooks/payment",
        data={
            "payment_id": "pay_123456",
            "amount": 29.99,
            "currency": "USD",
            "status": "completed",
            "customer_id": "cust_789"
        },
        headers={
            "Authorization": "Bearer sk_live_abc123...",
            "X-API-Version": "2023-01-01",
            "X-Idempotency-Key": "payment_123456_completed"
        },
        webhook_id="payment_notification",
        max_retries=3
    )

    print(f"Secure webhook job {job_id} queued")
    return job_id


def example_delayed_webhook():
    """Webhook scheduled for future delivery."""

    # Send reminder webhook 1 hour from now
    job_id = publish_webhook(
        url="https://notifications.example.com/webhooks/reminder",
        data={
            "user_id": 456,
            "reminder_type": "trial_ending",
            "trial_ends_at": (datetime.now() + timedelta(days=1)).isoformat(),
            "message": "Your free trial ends tomorrow!"
        },
        delay=3600,  # 1 hour delay
        expires_in=86400,  # Expire after 24 hours
        webhook_id="trial_reminder_456"
    )

    print(f"Delayed webhook job {job_id} scheduled for 1 hour from now")
    return job_id


def example_webhook_with_custom_retry():
    """Webhook with custom retry configuration for critical notifications."""

    job_id = publish_webhook(
        url="https://critical-alerts.example.com/webhooks/system-alert",
        data={
            "alert_id": "alert_789",
            "severity": "critical",
            "service": "payment_processor",
            "message": "Payment processor is experiencing issues",
            "timestamp": datetime.now().isoformat(),
            "affected_users": 1250
        },
        headers={
            "X-Alert-Priority": "critical",
            "Content-Type": "application/json"
        },
        max_retries=10,  # Retry up to 10 times for critical alerts
        backoff_base=1.5,  # Slower backoff (1.5^attempt)
        backoff_max=3600,  # Max 1 hour between retries
        timeout=60,  # Longer timeout for critical notifications
        webhook_id="critical_alert_789"
    )

    print(f"Critical alert webhook job {job_id} queued with aggressive retry policy")
    return job_id


def example_webhook_batch():
    """Send multiple webhooks for batch processing."""

    job_ids = []

    # Send order confirmations for multiple orders
    orders = [
        {"order_id": "ord_001", "customer": "Alice", "total": 45.99},
        {"order_id": "ord_002", "customer": "Bob", "total": 123.45},
        {"order_id": "ord_003", "customer": "Carol", "total": 67.89}
    ]

    for order in orders:
        job_id = publish_webhook(
            url="https://fulfillment.example.com/webhooks/new-order",
            data={
                "event": "order_created",
                "order": order,
                "timestamp": datetime.now().isoformat()
            },
            headers={
                "Authorization": "Bearer fulfillment_token_123"
            },
            webhook_id=f"order_confirmation_{order['order_id']}",
            idempotency_key=f"order_{order['order_id']}_created"  # Prevent duplicates
        )
        job_ids.append(job_id)

    print(f"Queued {len(job_ids)} order confirmation webhooks")
    return job_ids


def example_webhook_integration_test():
    """Example webhook for testing integration with external services."""

    # Test webhook with httpbin.org (useful for debugging)
    job_id = publish_webhook(
        url="https://httpbin.org/post",  # Echo service for testing
        data={
            "test": True,
            "service": "django-mojo-jobs",
            "timestamp": datetime.now().isoformat(),
            "environment": "development"
        },
        headers={
            "X-Test-Header": "webhook-test",
            "X-Source": "django-mojo"
        },
        webhook_id="integration_test",
        max_retries=1  # Only retry once for tests
    )

    print(f"Test webhook job {job_id} sent to httpbin.org")
    print("Check the job metadata after completion to see the response")
    return job_id


# Usage examples that would be in your application code:

def handle_user_signup(user_id, email):
    """Example: Send webhook when user signs up."""
    return publish_webhook(
        url="https://analytics.yoursite.com/webhooks/signup",
        data={"user_id": user_id, "email": email, "event": "signup"}
    )


def handle_payment_success(payment_id, amount, customer_id):
    """Example: Send webhook when payment succeeds."""
    return publish_webhook(
        url="https://fulfillment.yoursite.com/webhooks/payment",
        data={
            "payment_id": payment_id,
            "amount": amount,
            "customer_id": customer_id,
            "status": "success"
        },
        headers={"Authorization": "Bearer your_webhook_secret"},
        max_retries=5  # Important for payment notifications
    )


def handle_system_alert(alert_data):
    """Example: Send critical system alerts."""
    return publish_webhook(
        url="https://alerts.yoursite.com/webhooks/system",
        data=alert_data,
        max_retries=10,
        timeout=120,  # Longer timeout for critical alerts
        webhook_id=f"alert_{alert_data.get('alert_id')}"
    )


if __name__ == "__main__":
    # Run examples (uncomment to test)
    # example_basic_webhook()
    # example_webhook_with_auth()
    # example_delayed_webhook()
    # example_webhook_with_custom_retry()
    # example_webhook_batch()
    # example_webhook_integration_test()
    pass
