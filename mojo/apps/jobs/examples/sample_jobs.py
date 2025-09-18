"""
Example job functions showing the new Django-MOJO Jobs pattern.

No decorators or registration required - just plain functions that accept a Job model.
"""
from datetime import datetime, timezone
import time
import requests
from typing import Optional
from mojo.apps.jobs.models import Job


def send_email(job: Job) -> str:
    """
    Send email to recipients.

    Expected payload:
        recipients: List of email addresses
        subject: Email subject
        body: Email body
        template: Optional template name
    """
    recipients = job.payload.get('recipients', [])
    subject = job.payload.get('subject', 'No Subject')
    body = job.payload.get('body', '')
    template = job.payload.get('template')

    # Check for cancellation
    if job.cancel_requested:
        job.metadata['cancelled'] = True
        job.metadata['cancelled_at'] = datetime.now(timezone.utc).isoformat()
        return "cancelled"

    sent_count = 0
    failed_recipients = []

    for recipient in recipients:
        try:
            job.add_log(f"sent to {recipient} successfully")
            # Your actual email sending logic here
            # send_mail(recipient, subject, body, template)
            print(f"Sending email to {recipient}")
            sent_count += 1
            time.sleep(2)

            # Check cancellation periodically for long lists
            if sent_count % 10 == 0 and job.cancel_requested:
                job.metadata['cancelled_at_recipient'] = sent_count
                break

        except Exception as e:
            failed_recipients.append({'email': recipient, 'error': str(e)})

    # Update metadata with results
    job.metadata['sent_count'] = sent_count
    job.metadata['failed_count'] = len(failed_recipients)
    if failed_recipients:
        job.metadata['failed_recipients'] = failed_recipients[:10]  # Keep first 10 failures
    job.metadata['completed_at'] = datetime.now(timezone.utc).isoformat()

    return "completed"


def simulate_long_job(job: Job) -> str:
    """
    Simulate a long-running job.

    Expected payload:
        duration: Duration in seconds
    """
    duration = job.payload.get('duration', 10)

    # Simulate long-running task
    time.sleep(duration)

    job.add_log("Job completed")

def process_file_upload(job: Job) -> str:
    """
    Process an uploaded file in chunks.

    Expected payload:
        file_path: Path to uploaded file
        processing_type: Type of processing to perform
        options: Processing options dict
    """
    file_path = job.payload['file_path']
    processing_type = job.payload.get('processing_type', 'default')
    options = job.payload.get('options', {})

    # Initialize processing
    job.metadata['started_at'] = datetime.now(timezone.utc).isoformat()
    job.metadata['file_path'] = file_path
    job.metadata['processing_type'] = processing_type

    try:
        # Simulate file processing
        total_size = 1000  # In real code: os.path.getsize(file_path)
        chunk_size = 100
        processed = 0

        while processed < total_size:
            # Check for cancellation
            if job.cancel_requested:
                job.metadata['cancelled'] = True
                job.metadata['processed_bytes'] = processed
                job.metadata['cancelled_at'] = datetime.now(timezone.utc).isoformat()
                return "cancelled"

            # Process chunk (simulate work)
            time.sleep(0.3)  # Simulate processing time
            processed += chunk_size

            # Update progress
            progress = min(100, (processed / total_size) * 100)
            job.metadata['progress'] = f"{progress:.1f}%"
            job.metadata['processed_bytes'] = processed

            # Save progress periodically (optional - has DB overhead)
            if processed % 500 == 0:
                job.save(update_fields=['metadata'])

        job.metadata['completed_at'] = datetime.now(timezone.utc).isoformat()
        job.metadata['total_processed'] = processed
        return "completed"

    except Exception as e:
        job.metadata['error'] = str(e)
        job.metadata['failed_at'] = datetime.now(timezone.utc).isoformat()
        job.log(f"Error processing job: {e}")
        raise  # Re-raise to trigger retry logic


def fetch_external_api(job: Job) -> str:
    """
    Fetch data from an external API with retry logic.

    Expected payload:
        url: API endpoint URL
        method: HTTP method (GET, POST, etc.)
        headers: Optional headers dict
        data: Optional request data
        timeout: Request timeout in seconds
    """
    url = job.payload['url']
    method = job.payload.get('method', 'GET')
    headers = job.payload.get('headers', {})
    data = job.payload.get('data')
    timeout = job.payload.get('timeout', 20)

    job.metadata['request_started'] = datetime.now(timezone.utc).isoformat()
    job.metadata['attempt'] = job.attempt

    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=data if data else None,
            timeout=timeout
        )

        # Check response
        response.raise_for_status()

        # Store response metadata
        job.metadata['status_code'] = response.status_code
        job.metadata['response_size'] = len(response.content)
        job.metadata['response_headers'] = dict(response.headers)
        job.metadata['completed_at'] = datetime.now(timezone.utc).isoformat()

        # If response is JSON, store a sample
        try:
            response_data = response.json()
            if isinstance(response_data, dict):
                job.metadata['response_sample'] = {k: v for k, v in list(response_data.items())[:5]}
            elif isinstance(response_data, list):
                job.metadata['response_count'] = len(response_data)
        except:
            job.add_log("not a valid JSON response")

        return "success"

    except requests.exceptions.Timeout:
        job.metadata['error'] = 'Request timed out'
        job.metadata['timeout_seconds'] = timeout
        raise  # Will retry based on job.max_retries

    except requests.exceptions.HTTPError as e:
        job.metadata['error'] = f"HTTP {e.response.status_code}: {e.response.reason}"
        job.metadata['status_code'] = e.response.status_code

        # Only retry on specific status codes
        if e.response.status_code in [408, 429, 502, 503, 504]:
            raise  # Will retry
        else:
            return "failed"  # Don't retry client errors

    except Exception as e:
        job.metadata['error'] = str(e)
        raise  # Will retry


def cleanup_old_records(job: Job) -> str:
    """
    Clean up old database records in batches.

    Expected payload:
        model_name: Name of model to clean
        days_old: Delete records older than this many days
        batch_size: Number of records to delete per batch
        dry_run: If True, don't actually delete
    """
    model_name = job.payload['model_name']
    days_old = job.payload.get('days_old', 30)
    batch_size = job.payload.get('batch_size', 100)
    dry_run = job.payload.get('dry_run', False)

    from django.utils import timezone
    from datetime import timedelta

    cutoff_date = timezone.now() - timedelta(days=days_old)

    job.metadata['started_at'] = datetime.now(timezone.utc).isoformat()
    job.metadata['cutoff_date'] = cutoff_date.isoformat()
    job.metadata['dry_run'] = dry_run

    deleted_count = 0
    batch_count = 0

    # This is a simplified example - in real code you'd import the actual model
    # from myapp.models import MyModel
    # queryset = MyModel.objects.filter(created__lt=cutoff_date)

    while True:
        # Check for cancellation
        if job.check_cancel_requested():
            job.metadata['cancelled'] = True
            job.metadata['deleted_count'] = deleted_count
            return "cancelled"

        # Simulate batch deletion
        # batch = queryset[:batch_size]
        # if not batch.exists():
        #     break

        # Simulate work
        time.sleep(0.5)
        batch_count += 1

        if dry_run:
            # Count but don't delete
            # deleted_count += batch.count()
            deleted_count += batch_size
        else:
            # Actually delete
            # deleted_count += batch.delete()[0]
            deleted_count += batch_size

        # Update progress
        job.metadata['deleted_count'] = deleted_count
        job.metadata['batch_count'] = batch_count

        # Stop after a few batches for this example
        if batch_count >= 5:
            break

    job.metadata['completed_at'] = datetime.now(timezone.utc).isoformat()
    job.metadata['total_deleted'] = deleted_count

    return "completed"


def generate_report(job: Job) -> str:
    """
    Generate a report with progress updates.

    Expected payload:
        report_type: Type of report to generate
        start_date: Report start date
        end_date: Report end date
        format: Output format (pdf, csv, excel)
        email_to: Optional email to send report to
    """
    report_type = job.payload['report_type']
    start_date = job.payload['start_date']
    end_date = job.payload['end_date']
    output_format = job.payload.get('format', 'pdf')
    email_to = job.payload.get('email_to')

    job.metadata['report_type'] = report_type
    job.metadata['date_range'] = f"{start_date} to {end_date}"

    # Simulate report generation steps
    steps = [
        'Fetching data',
        'Processing records',
        'Calculating metrics',
        'Generating charts',
        'Creating output file'
    ]

    for i, step in enumerate(steps):
        # Check cancellation
        if job.check_cancel_requested():
            job.metadata['cancelled_at_step'] = step
            return "cancelled"

        job.metadata['current_step'] = step
        job.metadata['progress'] = f"{((i + 1) / len(steps)) * 100:.0f}%"

        # Save progress (optional)
        job.save(update_fields=['metadata'])

        # Simulate work
        time.sleep(1)

    # Generate report file
    report_file = f"/tmp/report_{job.id}.{output_format}"
    job.metadata['report_file'] = report_file

    # Send email if requested
    if email_to:
        # send_report_email(email_to, report_file)
        job.metadata['email_sent_to'] = email_to

    job.metadata['completed_at'] = datetime.now(timezone.utc).isoformat()

    return "completed"


# Publishing examples (would be in your application code, not here):
"""
from mojo.apps.jobs import publish

# Publish by module path (no import needed)
job_id = publish(
    "mojo.apps.jobs.examples.sample_jobs.send_email",
    payload={
        'recipients': ['user1@example.com', 'user2@example.com'],
        'subject': 'Newsletter',
        'body': 'Hello from our newsletter!'
    },
    channel='emails',
    max_retries=3
)

# Or if you have the function imported
from mojo.apps.jobs.examples.sample_jobs import process_file_upload

job_id = publish(
    process_file_upload,  # Callable - will extract module path
    payload={
        'file_path': '/uploads/data.csv',
        'processing_type': 'import',
        'options': {'skip_duplicates': True}
    },
    channel='uploads'
)

# Schedule a cleanup job for later
from datetime import datetime, timedelta

job_id = publish(
    "mojo.apps.jobs.examples.sample_jobs.cleanup_old_records",
    payload={
        'model_name': 'LogEntry',
        'days_old': 90,
        'batch_size': 500,
        'dry_run': False
    },
    channel='maintenance',
    run_at=datetime.now() + timedelta(hours=2)  # Run in 2 hours
)
"""
