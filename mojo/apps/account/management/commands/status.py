"""
Django management command for system status checks.

Provides quick connectivity tests for critical system components:
- Database connection with timeout
- Redis connection with timeout
- Basic health checks for core services

Usage:
    # Basic status check
    python manage.py status

    # Verbose output with detailed information
    python manage.py status --verbose

    # JSON output for automation
    python manage.py status --json

    # Check specific components only
    python manage.py status --db-only
    python manage.py status --redis-only

    # Custom timeout settings
    python manage.py status --timeout 5
"""

import time
import json
import sys
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

# Import Redis helper
try:
    from mojo.helpers.redis.client import get_connection as get_redis_connection
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False


class Command(BaseCommand):
    help = 'Check system status: database and Redis connectivity with timeout tests'

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed information about each check'
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Output results as JSON'
        )
        parser.add_argument(
            '--timeout',
            type=int,
            default=5,
            help='Timeout in seconds for connectivity tests (default: 5)'
        )
        parser.add_argument(
            '--db-only',
            action='store_true',
            help='Check database connectivity only'
        )
        parser.add_argument(
            '--redis-only',
            action='store_true',
            help='Check Redis connectivity only'
        )
        parser.add_argument(
            '--exit-code',
            action='store_true',
            help='Exit with non-zero code if any checks fail'
        )

    def handle(self, *args, **options):
        """Handle command execution."""
        timeout = options['timeout']
        verbose = options['verbose']
        json_output = options['json']
        db_only = options['db_only']
        redis_only = options['redis_only']
        exit_on_failure = options['exit_code']

        # Collect all results
        results = {
            'timestamp': timezone.now().isoformat(),
            'checks': {},
            'summary': {
                'total_checks': 0,
                'passed': 0,
                'failed': 0,
                'overall_status': 'unknown'
            }
        }

        # Run checks based on options
        if not redis_only:
            db_result = self.check_database(timeout, verbose)
            results['checks']['database'] = db_result
            results['summary']['total_checks'] += 1

        if not db_only and HAS_REDIS:
            redis_result = self.check_redis(timeout, verbose)
            results['checks']['redis'] = redis_result
            results['summary']['total_checks'] += 1
        elif not db_only:
            results['checks']['redis'] = {
                'status': 'skipped',
                'message': 'Redis helper not available',
                'error': 'mojo.helpers.redis.client not importable'
            }
            results['summary']['total_checks'] += 1

        # Calculate summary
        for check_name, check_result in results['checks'].items():
            if check_result['status'] == 'ok':
                results['summary']['passed'] += 1
            elif check_result['status'] == 'error':
                results['summary']['failed'] += 1

        # Determine overall status
        if results['summary']['failed'] == 0:
            results['summary']['overall_status'] = 'healthy'
        elif results['summary']['passed'] > 0:
            results['summary']['overall_status'] = 'degraded'
        else:
            results['summary']['overall_status'] = 'critical'

        # Output results
        if json_output:
            self.stdout.write(json.dumps(results, indent=2))
        else:
            self.display_results(results, verbose)

        # Exit with appropriate code if requested
        if exit_on_failure and results['summary']['failed'] > 0:
            sys.exit(1)

    def check_database(self, timeout, verbose):
        """Check database connectivity with timeout."""
        result = {
            'status': 'unknown',
            'message': '',
            'details': {},
            'timing': {}
        }

        start_time = time.time()

        try:
            # Set query timeout
            with connection.cursor() as cursor:
                # Use a simple query that should work on all databases
                cursor.execute("SELECT 1")
                row = cursor.fetchone()

            end_time = time.time()
            query_time = end_time - start_time

            if row and row[0] == 1:
                result['status'] = 'ok'
                result['message'] = 'Database connection successful'
                result['timing']['query_time_ms'] = round(query_time * 1000, 2)

                if verbose:
                    # Get database info
                    db_info = connection.get_connection_params()
                    result['details'] = {
                        'engine': connection.vendor,
                        'host': db_info.get('HOST', 'unknown'),
                        'port': db_info.get('PORT', 'unknown'),
                        'database': db_info.get('NAME', 'unknown'),
                        'user': db_info.get('USER', 'unknown')
                    }
            else:
                result['status'] = 'error'
                result['message'] = 'Database query returned unexpected result'

        except Exception as e:
            end_time = time.time()
            result['status'] = 'error'
            result['message'] = f'Database connection failed: {str(e)}'
            result['timing']['error_time_ms'] = round((end_time - start_time) * 1000, 2)
            result['error'] = str(e)

            if verbose:
                result['details'] = {
                    'error_type': type(e).__name__,
                    'error_module': getattr(e, '__module__', 'unknown')
                }

        return result

    def check_redis(self, timeout, verbose):
        """Check Redis connectivity with timeout."""
        result = {
            'status': 'unknown',
            'message': '',
            'details': {},
            'timing': {}
        }

        if not HAS_REDIS:
            result['status'] = 'skipped'
            result['message'] = 'Redis helper not available'
            return result

        start_time = time.time()

        try:
            # Get Redis connection
            redis_client = get_redis_connection()

            # Test basic connectivity with ping
            ping_start = time.time()
            ping_result = redis_client.ping()
            ping_end = time.time()

            if ping_result:
                result['status'] = 'ok'
                result['message'] = 'Redis connection successful'
                result['timing']['ping_time_ms'] = round((ping_end - ping_start) * 1000, 2)

                if verbose:
                    # Get Redis info
                    try:
                        info = redis_client.info()
                        result['details'] = {
                            'redis_version': info.get('redis_version', 'unknown'),
                            'redis_mode': info.get('redis_mode', 'standalone'),
                            'connected_clients': info.get('connected_clients', 'unknown'),
                            'used_memory_human': info.get('used_memory_human', 'unknown'),
                            'role': info.get('role', 'unknown')
                        }

                        # Check if cluster mode
                        if hasattr(redis_client, 'cluster_nodes'):
                            result['details']['cluster_mode'] = True
                            try:
                                nodes = redis_client.cluster_nodes()
                                result['details']['cluster_nodes'] = len(nodes)
                            except Exception:
                                result['details']['cluster_nodes'] = 'unknown'
                        else:
                            result['details']['cluster_mode'] = False

                    except Exception as info_error:
                        result['details']['info_error'] = str(info_error)

                # Test basic operations
                try:
                    test_key = f"mojo:status:test:{int(time.time())}"

                    # Test SET
                    set_start = time.time()
                    redis_client.set(test_key, "test_value", ex=10)  # 10 second expiry
                    set_end = time.time()

                    # Test GET
                    get_start = time.time()
                    value = redis_client.get(test_key)
                    get_end = time.time()

                    # Clean up
                    redis_client.delete(test_key)

                    if value == "test_value":
                        result['timing']['set_time_ms'] = round((set_end - set_start) * 1000, 2)
                        result['timing']['get_time_ms'] = round((get_end - get_start) * 1000, 2)
                        result['message'] += ' (operations tested)'
                    else:
                        result['message'] += ' (ping only - operation test failed)'

                except Exception as op_error:
                    result['timing']['operation_error'] = str(op_error)
                    result['message'] += ' (ping only - operations failed)'

            else:
                result['status'] = 'error'
                result['message'] = 'Redis ping failed'

        except Exception as e:
            end_time = time.time()
            result['status'] = 'error'
            result['message'] = f'Redis connection failed: {str(e)}'
            result['timing']['error_time_ms'] = round((end_time - start_time) * 1000, 2)
            result['error'] = str(e)

            if verbose:
                result['details'] = {
                    'error_type': type(e).__name__,
                    'error_module': getattr(e, '__module__', 'unknown')
                }

        return result

    def display_results(self, results, verbose):
        """Display results in human-readable format."""

        # Header
        self.stdout.write(
            self.style.SUCCESS("=== MOJO System Status Check ===")
        )
        self.stdout.write(f"Timestamp: {results['timestamp']}")
        self.stdout.write("")

        # Individual check results
        for check_name, check_result in results['checks'].items():
            status = check_result['status']
            message = check_result['message']

            if status == 'ok':
                status_display = self.style.SUCCESS("✓ PASS")
            elif status == 'error':
                status_display = self.style.ERROR("✗ FAIL")
            elif status == 'skipped':
                status_display = self.style.WARNING("- SKIP")
            else:
                status_display = self.style.WARNING("? UNKNOWN")

            self.stdout.write(f"{check_name.upper():>10}: {status_display} - {message}")

            # Show timing information
            timing = check_result.get('timing', {})
            if timing:
                timing_info = []
                for key, value in timing.items():
                    if key.endswith('_ms'):
                        timing_info.append(f"{key.replace('_ms', '')}: {value}ms")
                    else:
                        timing_info.append(f"{key}: {value}")

                if timing_info:
                    self.stdout.write(f"           Timing: {', '.join(timing_info)}")

            # Show detailed information in verbose mode
            if verbose and check_result.get('details'):
                self.stdout.write(f"           Details:")
                for key, value in check_result['details'].items():
                    self.stdout.write(f"             {key}: {value}")

            # Show errors
            if check_result.get('error'):
                self.stdout.write(f"           Error: {check_result['error']}")

            self.stdout.write("")

        # Summary
        summary = results['summary']
        overall_status = summary['overall_status']

        if overall_status == 'healthy':
            status_style = self.style.SUCCESS
            status_symbol = "✓"
        elif overall_status == 'degraded':
            status_style = self.style.WARNING
            status_symbol = "⚠"
        else:
            status_style = self.style.ERROR
            status_symbol = "✗"

        self.stdout.write("=== SUMMARY ===")
        self.stdout.write(
            f"Overall Status: {status_style(f'{status_symbol} {overall_status.upper()}')}"
        )
        self.stdout.write(
            f"Checks: {summary['passed']}/{summary['total_checks']} passed"
        )

        if summary['failed'] > 0:
            self.stdout.write(
                self.style.ERROR(f"Failed checks: {summary['failed']}")
            )
