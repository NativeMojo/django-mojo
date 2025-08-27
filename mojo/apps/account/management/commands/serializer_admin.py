"""
Django management command for serializer administration and benchmarking.

Provides comprehensive serializer management capabilities including:
- Performance benchmarking and comparison
- Serializer registration and configuration
- Cache management and statistics
- Health checks and diagnostics

Usage:
    # List all registered serializers
    python manage.py serializer_admin list

    # Benchmark serializers
    python manage.py serializer_admin benchmark --model MyModel --count 1000

    # Set default serializer
    python manage.py serializer_admin set-default optimized

    # Get performance statistics
    python manage.py serializer_admin stats

    # Clear caches
    python manage.py serializer_admin clear-cache
"""

import time
import json
from django.core.management.base import BaseCommand, CommandError
from django.apps import apps
from django.db import models
from django.core.management import color

from mojo.serializers import (
    get_serializer_manager,
    get_performance_stats,
    clear_serializer_caches,
    benchmark_serializers,
    list_serializers,
    set_default_serializer,
    HAS_UJSON,
    UJSON_VERSION
)

# Import cache system for enhanced functionality
try:
    from mojo.serializers.core.cache import (
        get_cache_backend,
        get_cache_stats,
        get_available_backends,
        test_backend_connectivity,
        get_cache_health
    )
    HAS_CACHE_SYSTEM = True
except ImportError:
    HAS_CACHE_SYSTEM = False


class Command(BaseCommand):
    help = 'Manage MOJO serializers: benchmark, configure, and monitor performance'

    def add_arguments(self, parser):
        """Add command line arguments."""

        subparsers = parser.add_subparsers(dest='action', help='Available actions')

        # List serializers
        list_parser = subparsers.add_parser('list', help='List all registered serializers')
        list_parser.add_argument(
            '--detail',
            action='store_true',
            help='Show detailed information about each serializer'
        )

        # Benchmark serializers
        bench_parser = subparsers.add_parser('benchmark', help='Benchmark serializer performance')
        bench_parser.add_argument(
            '--model',
            type=str,
            required=True,
            help='Model name to benchmark (format: app.ModelName or ModelName)'
        )
        bench_parser.add_argument(
            '--count',
            type=int,
            default=100,
            help='Number of objects to serialize (default: 100)'
        )
        bench_parser.add_argument(
            '--graph',
            type=str,
            default='default',
            help='Graph configuration to use (default: "default")'
        )
        bench_parser.add_argument(
            '--iterations',
            type=int,
            default=5,
            help='Number of benchmark iterations (default: 5)'
        )
        bench_parser.add_argument(
            '--serializers',
            nargs='+',
            help='Specific serializers to benchmark (default: all)'
        )
        bench_parser.add_argument(
            '--output-json',
            type=str,
            help='Save results to JSON file'
        )

        # Set default serializer
        default_parser = subparsers.add_parser('set-default', help='Set default serializer')
        default_parser.add_argument(
            'serializer_name',
            type=str,
            help='Name of serializer to set as default'
        )

        # Performance statistics
        stats_parser = subparsers.add_parser('stats', help='Show performance statistics')
        stats_parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear statistics after showing them'
        )
        stats_parser.add_argument(
            '--json',
            action='store_true',
            help='Output statistics as JSON'
        )

        # Cache management
        cache_parser = subparsers.add_parser('clear-cache', help='Clear serializer caches')
        cache_parser.add_argument(
            '--serializer',
            type=str,
            help='Clear cache for specific serializer (default: all)'
        )

        # Health check
        health_parser = subparsers.add_parser('health', help='Run serializer health checks')
        health_parser.add_argument(
            '--model',
            type=str,
            help='Test specific model (format: app.ModelName or ModelName)'
        )

        # Test serializers
        test_parser = subparsers.add_parser('test', help='Test serializer functionality')
        test_parser.add_argument(
            '--model',
            type=str,
            required=True,
            help='Model to test (format: app.ModelName or ModelName)'
        )
        test_parser.add_argument(
            '--graph',
            type=str,
            default='default',
            help='Graph configuration to test'
        )

        # Cache information
        cache_parser = subparsers.add_parser('cache-info', help='Show detailed cache information')
        cache_parser.add_argument(
            '--test-connectivity',
            action='store_true',
            help='Test cache backend connectivity'
        )

    def handle(self, *args, **options):
        """Handle command execution."""
        action = options.get('action')

        if not action:
            self.print_help()
            return

        # Route to appropriate handler
        handler_map = {
            'list': self.handle_list,
            'benchmark': self.handle_benchmark,
            'set-default': self.handle_set_default,
            'stats': self.handle_stats,
            'clear-cache': self.handle_clear_cache,
            'health': self.handle_health,
            'test': self.handle_test,
            'cache-info': self.handle_cache_info,
        }

        handler = handler_map.get(action)
        if handler:
            try:
                handler(options)
            except Exception as e:
                raise CommandError(f"Error executing {action}: {str(e)}")
        else:
            raise CommandError(f"Unknown action: {action}")

    def handle_list(self, options):
        """List all registered serializers."""
        serializers = list_serializers()

        if not serializers:
            self.stdout.write(
                self.style.WARNING('No serializers registered')
            )
            return

        self.stdout.write(
            self.style.SUCCESS('Registered Serializers:')
        )

        for name, info in serializers.items():
            status = " (DEFAULT)" if info['is_default'] else ""
            self.stdout.write(f"  • {name}{status}")

            if options.get('detail'):
                self.stdout.write(f"    Class: {info['class_name']}")
                self.stdout.write(f"    Description: {info['description']}")

        # Show ujson information
        self.stdout.write(f"\nPerformance Information:")
        if HAS_UJSON:
            self.stdout.write(f"  ✓ ujson {UJSON_VERSION} available - optimal JSON performance")
        else:
            self.stdout.write(f"  ⚠ ujson not available - using standard json (slower)")
            self.stdout.write(f"    Install with: pip install ujson")

        # Show cache backend information
        if HAS_CACHE_SYSTEM:
            try:
                cache_backend = get_cache_backend()
                cache_stats = cache_backend.stats()
                backend_type = cache_stats.get('backend', 'unknown')
                self.stdout.write(f"  ✓ Cache backend: {backend_type}")
            except Exception as e:
                self.stdout.write(f"  ⚠ Cache backend error: {e}")

    def handle_benchmark(self, options):
        """Benchmark serializer performance."""
        model_class = self.get_model_class(options['model'])
        count = options['count']
        graph = options['graph']
        iterations = options['iterations']
        serializer_types = options.get('serializers')

        # Check if model has enough instances
        total_instances = model_class.objects.count()
        if total_instances < count:
            self.stdout.write(
                self.style.WARNING(
                    f"Model {model_class.__name__} only has {total_instances} instances, "
                    f"but {count} requested. Using available instances."
                )
            )
            count = min(count, total_instances)

        if count == 0:
            raise CommandError(f"No instances found for model {model_class.__name__}")

        # Get test queryset
        test_queryset = model_class.objects.all()[:count]

        self.stdout.write(
            self.style.SUCCESS(
                f"Benchmarking serializers with {count} {model_class.__name__} instances"
            )
        )
        self.stdout.write(f"Graph: {graph}")
        self.stdout.write(f"Iterations: {iterations}")

        # Run benchmark
        try:
            results = benchmark_serializers(
                instance=test_queryset,
                graph=graph,
                serializer_types=serializer_types,
                iterations=iterations
            )

            # Display results
            self.display_benchmark_results(results)

            # Save to JSON if requested
            if options.get('output_json'):
                self.save_benchmark_results(results, options['output_json'])

        except Exception as e:
            raise CommandError(f"Benchmark failed: {str(e)}")

    def handle_set_default(self, options):
        """Set default serializer."""
        serializer_name = options['serializer_name']

        if set_default_serializer(serializer_name):
            self.stdout.write(
                self.style.SUCCESS(
                    f"Default serializer set to: {serializer_name}"
                )
            )
        else:
            raise CommandError(f"Failed to set default serializer to {serializer_name}")

    def handle_stats(self, options):
        """Show performance statistics."""
        stats = get_performance_stats()

        if options.get('json'):
            self.stdout.write(json.dumps(stats, indent=2))
        else:
            self.display_stats(stats)

        if options.get('clear'):
            # Clear statistics if requested
            manager = get_serializer_manager()
            if hasattr(manager, 'reset_performance_stats'):
                manager.reset_performance_stats()
            self.stdout.write(
                self.style.SUCCESS("Performance statistics cleared")
            )

    def handle_cache_info(self, options):
        """Show detailed cache information."""
        if not HAS_CACHE_SYSTEM:
            self.stdout.write(
                self.style.ERROR("Cache system not available")
            )
            return

        self.stdout.write(self.style.SUCCESS("Cache System Information:"))

        try:
            # Show available backends
            backends = get_available_backends()
            self.stdout.write(f"\nAvailable Backends:")
            for name, info in backends.items():
                status = "✓" if info['available'] else "✗"
                self.stdout.write(f"  {status} {name}: {info['description']}")
                if info.get('ujson_available'):
                    self.stdout.write(f"    ujson: {info.get('ujson_version', 'available')}")
                if info.get('error'):
                    self.stdout.write(f"    Error: {info['error']}")

            # Show current backend health
            health = get_cache_health()
            self.stdout.write(f"\nCache Health: {health['status'].upper()}")
            if health.get('issues'):
                for issue in health['issues']:
                    self.stdout.write(f"  ⚠ {issue}")

            # Show recommendations
            if health.get('recommendations'):
                self.stdout.write(f"\nRecommendations:")
                for rec in health['recommendations']:
                    self.stdout.write(f"  • {rec}")

            # Test connectivity if requested
            if options.get('test_connectivity'):
                self.stdout.write(f"\nTesting Cache Connectivity:")
                backend_type = health.get('backend_type', 'unknown')
                test_result = test_backend_connectivity(backend_type)

                if test_result['connectivity']:
                    self.stdout.write(f"  ✓ {backend_type} backend connectivity OK")
                    if test_result['functionality']:
                        perf = test_result.get('performance', {})
                        self.stdout.write(f"  ✓ Functionality test passed")
                        if perf:
                            self.stdout.write(f"    Set time: {perf.get('set_time_ms', 0)}ms")
                            self.stdout.write(f"    Get time: {perf.get('get_time_ms', 0)}ms")
                    else:
                        self.stdout.write(f"  ✗ Functionality test failed")
                else:
                    self.stdout.write(f"  ✗ {backend_type} backend connectivity failed")

                for error in test_result.get('errors', []):
                    self.stdout.write(f"    Error: {error}")

        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"Error getting cache information: {e}")
            )

    def handle_clear_cache(self, options):
        """Clear serializer caches."""
        serializer_name = options.get('serializer')

        if serializer_name:
            clear_serializer_caches(serializer_name)
            self.stdout.write(
                self.style.SUCCESS(f"Cache cleared for {serializer_name}")
            )
        else:
            clear_serializer_caches()
            self.stdout.write(
                self.style.SUCCESS("All serializer caches cleared")
            )

    def handle_health(self, options):
        """Run serializer health checks."""
        model_name = options.get('model')

        if model_name:
            # Test specific model
            model_class = self.get_model_class(model_name)
            self.run_model_health_check(model_class)
        else:
            # Test all MojoModel subclasses
            self.run_full_health_check()

    def handle_test(self, options):
        """Test serializer functionality."""
        model_class = self.get_model_class(options['model'])
        graph = options['graph']

        # Get a test instance
        instance = model_class.objects.first()
        if not instance:
            raise CommandError(f"No instances found for model {model_class.__name__}")

        self.stdout.write(
            self.style.SUCCESS(f"Testing {model_class.__name__} serialization")
        )

        # Test each registered serializer
        serializers = list_serializers()
        manager = get_serializer_manager()

        for serializer_name in serializers.keys():
            self.stdout.write(f"\nTesting {serializer_name}:")

            try:
                # Test single instance
                serializer = manager.get_serializer(instance, graph=graph, serializer_type=serializer_name)
                data = serializer.serialize()
                json_output = serializer.to_json()

                self.stdout.write(
                    self.style.SUCCESS(f"  ✓ Single instance: {len(str(data))} chars")
                )

                # Test queryset
                queryset = model_class.objects.all()[:5]  # Test with 5 instances
                serializer = manager.get_serializer(queryset, graph=graph, serializer_type=serializer_name, many=True)
                list_data = serializer.serialize()
                list_json = serializer.to_json()

                self.stdout.write(
                    self.style.SUCCESS(f"  ✓ QuerySet ({len(list_data)} items): {len(str(list_json))} chars")
                )

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"  ✗ Failed: {str(e)}")
                )

    def get_model_class(self, model_string):
        """Get model class from string."""
        try:
            if '.' in model_string:
                app_label, model_name = model_string.split('.', 1)
                return apps.get_model(app_label, model_name)
            else:
                # Try to find model in any app
                for app_config in apps.get_app_configs():
                    try:
                        return app_config.get_model(model_string)
                    except LookupError:
                        continue
                raise CommandError(f"Model '{model_string}' not found")
        except Exception as e:
            raise CommandError(f"Invalid model specification '{model_string}': {str(e)}")

    def display_benchmark_results(self, results):
        """Display benchmark results in a formatted table."""
        if not results:
            self.stdout.write(self.style.WARNING("No benchmark results"))
            return

        self.stdout.write("\nBenchmark Results:")
        self.stdout.write("=" * 80)

        # Table header
        header = f"{'Serializer':<15} {'Avg Time':<12} {'Min Time':<12} {'Max Time':<12} {'Obj/Sec':<10}"
        self.stdout.write(header)
        self.stdout.write("-" * 80)

        # Sort by average time
        sorted_results = sorted(
            results.items(),
            key=lambda x: x[1].get('avg_time', float('inf'))
        )

        for name, stats in sorted_results:
            if 'error' in stats:
                row = f"{name:<15} {stats['error']:<50}"
                self.stdout.write(self.style.ERROR(row))
            else:
                avg_time = f"{stats['avg_time']:.4f}s"
                min_time = f"{stats['min_time']:.4f}s"
                max_time = f"{stats['max_time']:.4f}s"
                obj_per_sec = f"{stats.get('objects_per_second', 0):.1f}"

                row = f"{name:<15} {avg_time:<12} {min_time:<12} {max_time:<12} {obj_per_sec:<10}"
                self.stdout.write(row)

                if stats.get('errors', 0) > 0:
                    self.stdout.write(
                        self.style.WARNING(f"  └─ {stats['errors']} errors occurred")
                    )

    def display_stats(self, stats):
        """Display performance statistics."""
        self.stdout.write(self.style.SUCCESS("Serializer Performance Statistics:"))

        # Default serializer
        default_serializer = stats.get('default_serializer')
        if default_serializer:
            self.stdout.write(f"Default Serializer: {default_serializer}")

        # Registered serializers
        registered = stats.get('registered_serializers', {})
        if registered:
            self.stdout.write(f"Registered Serializers: {len(registered)}")
            for name, info in registered.items():
                status = " (default)" if info['is_default'] else ""
                self.stdout.write(f"  • {name}{status}")

        # Usage statistics
        usage_stats = stats.get('usage_stats', {})
        if usage_stats:
            self.stdout.write("\nUsage Statistics:")
            for serializer, data in usage_stats.items():
                self.stdout.write(
                    f"  {serializer}: {data['count']} uses, "
                    f"{data['total_objects']} objects serialized"
                )

        # Cache statistics
        if HAS_CACHE_SYSTEM:
            try:
                cache_stats = get_cache_stats()
                self.stdout.write(f"\nCache Statistics:")
                self.stdout.write(f"  Backend: {cache_stats.get('backend', 'unknown')}")
                self.stdout.write(f"  Hit Rate: {cache_stats.get('hit_rate', 0):.1%}")
                self.stdout.write(f"  Total Requests: {cache_stats.get('total_requests', 0)}")
                self.stdout.write(f"  Cache Size: {cache_stats.get('current_size', 0)}")
                if cache_stats.get('max_size'):
                    utilization = cache_stats.get('current_size', 0) / cache_stats.get('max_size', 1)
                    self.stdout.write(f"  Utilization: {utilization:.1%}")
            except Exception as e:
                self.stdout.write(f"\nCache Stats Error: {e}")

        # Individual serializer stats
        for key, value in stats.items():
            if key.endswith('_stats') and isinstance(value, dict):
                serializer_name = key.replace('_stats', '')
                self.stdout.write(f"\n{serializer_name.title()} Stats:")
                for stat_key, stat_value in value.items():
                    self.stdout.write(f"  {stat_key}: {stat_value}")

    def save_benchmark_results(self, results, filename):
        """Save benchmark results to JSON file."""
        try:
            with open(filename, 'w') as f:
                json.dump({
                    'timestamp': time.time(),
                    'results': results
                }, f, indent=2)
            self.stdout.write(
                self.style.SUCCESS(f"Results saved to {filename}")
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"Failed to save results: {str(e)}")
            )

    def run_model_health_check(self, model_class):
        """Run health check for specific model."""
        self.stdout.write(f"Health check for {model_class.__name__}:")

        # Check if model has RestMeta
        if hasattr(model_class, 'RestMeta'):
            self.stdout.write(self.style.SUCCESS("  ✓ RestMeta found"))

            # Check graphs
            if hasattr(model_class.RestMeta, 'GRAPHS'):
                graphs = model_class.RestMeta.GRAPHS
                self.stdout.write(f"  ✓ {len(graphs)} graphs configured: {list(graphs.keys())}")
            else:
                self.stdout.write(self.style.WARNING("  ⚠ No GRAPHS configured"))
        else:
            self.stdout.write(self.style.WARNING("  ⚠ No RestMeta found"))

        # Test serialization if instances exist
        if model_class.objects.exists():
            instance = model_class.objects.first()
            manager = get_serializer_manager()

            try:
                serializer = manager.get_serializer(instance)
                data = serializer.serialize()
                self.stdout.write(self.style.SUCCESS("  ✓ Serialization successful"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ✗ Serialization failed: {str(e)}"))
        else:
            self.stdout.write(self.style.WARNING("  ⚠ No instances available for testing"))

    def run_full_health_check(self):
        """Run health check for all models."""
        self.stdout.write("Running full serializer health check...")

        # Import MojoModel to check for subclasses
        try:
            from mojo.models.rest import MojoModel

            # Find all MojoModel subclasses
            mojo_models = []
            for app_config in apps.get_app_configs():
                for model in app_config.get_models():
                    if hasattr(model, 'RestMeta') or 'MojoModel' in [c.__name__ for c in model.__mro__]:
                        mojo_models.append(model)

            if not mojo_models:
                self.stdout.write(self.style.WARNING("No MojoModel subclasses found"))
                return

            self.stdout.write(f"Found {len(mojo_models)} models to check\n")

            for model in mojo_models:
                self.run_model_health_check(model)
                self.stdout.write("")  # Empty line

        except ImportError:
            self.stdout.write(self.style.ERROR("Could not import MojoModel"))

    def print_help(self):
        """Print command help."""
        self.stdout.write("MOJO Serializer Administration Command")
        self.stdout.write("\nAvailable actions:")
        self.stdout.write("  list        - List registered serializers")
        self.stdout.write("  benchmark   - Benchmark serializer performance")
        self.stdout.write("  set-default - Set default serializer")
        self.stdout.write("  stats       - Show performance statistics")
        self.stdout.write("  clear-cache - Clear serializer caches")
        self.stdout.write("  health      - Run health checks")
        self.stdout.write("  test        - Test serializer functionality")
        self.stdout.write("  cache-info  - Show detailed cache information")
        self.stdout.write("\nUse 'python manage.py serializer_admin <action> --help' for more details")
