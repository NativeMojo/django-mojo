"""
Webhook job handler for Django-MOJO Jobs System.

Specialized handler for sending webhooks (HTTP POST requests) with proper
retry logic, timeout handling, and comprehensive logging.
"""
import json
import requests
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from urllib.parse import urlparse

from django.conf import settings

from mojo.helpers import logit
from mojo.apps.jobs.models import Job


def post_webhook(job: Job) -> str:
    """
    Send a webhook POST request with comprehensive error handling and logging.

    Expected payload:
        url: Target webhook URL (required)
        data: Data to POST as JSON (required)
        headers: HTTP headers dict (optional)
        timeout: Request timeout in seconds (default: 30)
        webhook_id: Optional webhook identifier for tracking

    Returns:
        str: 'success', 'failed', or 'cancelled'

    The job will be automatically retried based on the job's retry configuration
    when certain errors occur (network errors, 5xx responses, timeouts).
    """
    # Extract payload
    url = job.payload.get('url')
    data = job.payload.get('data')
    headers = job.payload.get('headers', {})
    timeout = job.payload.get('timeout', getattr(settings, 'JOBS_WEBHOOK_DEFAULT_TIMEOUT', 30))
    webhook_id = job.payload.get('webhook_id')

    # Validate required fields
    if not url:
        job.metadata['error'] = 'Missing required field: url'
        job.metadata['failed_at'] = datetime.now(timezone.utc).isoformat()
        return 'failed'

    if data is None:
        job.metadata['error'] = 'Missing required field: data'
        job.metadata['failed_at'] = datetime.now(timezone.utc).isoformat()
        return 'failed'

    # Check for cancellation
    if job.cancel_requested:
        job.metadata['cancelled'] = True
        job.metadata['cancelled_at'] = datetime.now(timezone.utc).isoformat()
        logit.info(f"Webhook job {job.id} cancelled before execution")
        return 'cancelled'

    # Initialize tracking metadata
    start_time = datetime.now(timezone.utc)
    job.metadata.update({
        'webhook_started_at': start_time.isoformat(),
        'url': url,
        'webhook_id': webhook_id,
        'attempt': job.attempt,
        'timeout_seconds': timeout,
        'headers_sent': _sanitize_headers(headers)
    })

    try:
        # Parse URL for validation and logging
        parsed_url = urlparse(url)
        job.metadata['parsed_url'] = {
            'scheme': parsed_url.scheme,
            'netloc': parsed_url.netloc,
            'path': parsed_url.path
        }

        logit.info(f"Sending webhook {job.id} to {parsed_url.netloc}{parsed_url.path} "
                  f"(attempt {job.attempt})")

        # Make the request
        response = requests.post(
            url=url,
            json=data,  # Auto JSON encoding and Content-Type header
            headers=headers,
            timeout=timeout,
            allow_redirects=True  # Follow redirects for webhooks
        )

        # Calculate duration
        end_time = datetime.now(timezone.utc)
        duration_ms = int((end_time - start_time).total_seconds() * 1000)

        # Store response metadata
        job.metadata.update({
            'response_status_code': response.status_code,
            'response_headers': dict(response.headers),
            'response_size_bytes': len(response.content),
            'duration_ms': duration_ms,
            'webhook_completed_at': end_time.isoformat()
        })

        # Store response body sample (first 1KB for debugging)
        if response.content:
            try:
                # Try to parse as JSON first
                response_json = response.json()
                if isinstance(response_json, dict):
                    # Store first 10 keys for debugging
                    sample = {k: v for k, v in list(response_json.items())[:10]}
                    job.metadata['response_sample'] = sample
                elif isinstance(response_json, list):
                    job.metadata['response_count'] = len(response_json)
                    if response_json:
                        job.metadata['response_sample'] = response_json[0]
            except:
                # Not JSON, store text sample
                text_sample = response.text[:1000]
                job.metadata['response_text_sample'] = text_sample

        # Check response status
        try:
            response.raise_for_status()

            # Success!
            logit.info(f"Webhook {job.id} delivered successfully to {parsed_url.netloc} "
                      f"(status {response.status_code}, {duration_ms}ms)")

            # Emit success metrics
            _emit_webhook_metrics('success', duration_ms, parsed_url.netloc)

            return 'success'

        except requests.exceptions.HTTPError as e:
            # HTTP error response
            status_code = response.status_code
            job.metadata.update({
                'error_type': 'http_error',
                'error_status_code': status_code,
                'error_message': f"HTTP {status_code}: {response.reason}"
            })

            # Determine if we should retry based on status code
            retriable_codes = [408, 429, 502, 503, 504, 520, 521, 522, 523, 524]

            if status_code in retriable_codes:
                # Server/network error - retry
                logit.warn(f"Webhook {job.id} received retriable HTTP {status_code}, will retry")
                _emit_webhook_metrics('error_retriable', duration_ms, parsed_url.netloc)
                raise  # This will trigger retry logic

            elif 400 <= status_code < 500:
                # Client error - don't retry
                logit.error(f"Webhook {job.id} failed with client error HTTP {status_code}")
                _emit_webhook_metrics('error_client', duration_ms, parsed_url.netloc)
                return 'failed'

            else:
                # Other error - retry
                logit.warn(f"Webhook {job.id} received HTTP {status_code}, will retry")
                _emit_webhook_metrics('error_retriable', duration_ms, parsed_url.netloc)
                raise

    except requests.exceptions.Timeout:
        job.metadata.update({
            'error_type': 'timeout',
            'error_message': f'Request timed out after {timeout} seconds'
        })
        logit.warn(f"Webhook {job.id} timed out after {timeout}s, will retry")
        _emit_webhook_metrics('timeout', None, parsed_url.netloc if 'parsed_url' in locals() else 'unknown')
        raise  # Retry on timeout

    except requests.exceptions.ConnectionError as e:
        job.metadata.update({
            'error_type': 'connection_error',
            'error_message': f'Connection failed: {str(e)}'
        })
        logit.warn(f"Webhook {job.id} connection failed, will retry: {e}")
        _emit_webhook_metrics('connection_error', None, parsed_url.netloc if 'parsed_url' in locals() else 'unknown')
        raise  # Retry on connection errors

    except requests.exceptions.RequestException as e:
        # Other requests errors - retry
        job.metadata.update({
            'error_type': 'request_error',
            'error_message': str(e)
        })
        logit.warn(f"Webhook {job.id} request failed, will retry: {e}")
        _emit_webhook_metrics('request_error', None, parsed_url.netloc if 'parsed_url' in locals() else 'unknown')
        raise

    except Exception as e:
        # Unexpected error - don't retry
        job.metadata.update({
            'error_type': 'unexpected_error',
            'error_message': str(e),
            'failed_at': datetime.now(timezone.utc).isoformat()
        })
        logit.error(f"Webhook {job.id} failed with unexpected error: {e}")
        _emit_webhook_metrics('unexpected_error', None, parsed_url.netloc if 'parsed_url' in locals() else 'unknown')
        return 'failed'


def _sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """
    Sanitize headers for logging by masking sensitive values.

    Args:
        headers: Original headers dict

    Returns:
        dict: Headers with sensitive values masked
    """
    if not headers:
        return {}

    # Headers to mask
    sensitive_headers = {
        'authorization', 'x-api-key', 'x-auth-token', 'cookie',
        'x-webhook-secret', 'x-hub-signature', 'x-signature'
    }

    sanitized = {}
    for key, value in headers.items():
        if key.lower() in sensitive_headers:
            # Show just the first few characters
            if isinstance(value, str) and len(value) > 8:
                sanitized[key] = f"{value[:4]}...{value[-4:]}"
            else:
                sanitized[key] = "***masked***"
        else:
            sanitized[key] = value

    return sanitized


def _emit_webhook_metrics(outcome: str, duration_ms: Optional[int], host: str):
    """
    Emit webhook metrics for monitoring.

    Args:
        outcome: Outcome type (success, timeout, error_*, etc.)
        duration_ms: Request duration in milliseconds (if available)
        host: Target hostname
    """
    try:
        from mojo.apps import metrics

        now = datetime.now(timezone.utc)

        # Emit outcome metric
        metrics.record(
            slug=f"webhooks.{outcome}",
            when=now,
            count=1,
            category="webhooks"
        )

        # Emit per-host metric
        safe_host = host.replace('.', '_').replace('-', '_')[:50]  # Safe metric name
        metrics.record(
            slug=f"webhooks.host.{safe_host}.{outcome}",
            when=now,
            count=1,
            category="webhooks"
        )

        # Emit duration metric if available
        if duration_ms is not None:
            metrics.record(
                slug="webhooks.duration_ms",
                when=now,
                count=duration_ms,
                category="webhooks"
            )

    except Exception as e:
        # Don't fail the job if metrics fail
        logit.debug(f"Failed to emit webhook metrics: {e}")


def validate_webhook_payload(payload: Dict[str, Any]) -> Optional[str]:
    """
    Validate webhook job payload.

    Args:
        payload: Job payload to validate

    Returns:
        str: Error message if invalid, None if valid
    """
    if not isinstance(payload, dict):
        return "Payload must be a dictionary"

    if 'url' not in payload:
        return "Missing required field: url"

    if 'data' not in payload:
        return "Missing required field: data"

    url = payload['url']
    if not isinstance(url, str) or not url.strip():
        return "URL must be a non-empty string"

    if not url.startswith(('http://', 'https://')):
        return "URL must start with http:// or https://"

    # Validate data is JSON serializable
    try:
        json.dumps(payload['data'])
    except (TypeError, ValueError):
        return "Data field must be JSON serializable"

    return None
