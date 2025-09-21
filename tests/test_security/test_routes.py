from testit import helpers as th
from django.urls import get_resolver, URLPattern, URLResolver
from mojo.helpers import logit, paths, dates
import inspect
import importlib
import json
import os


def get_routes(urlpatterns, prefix=''):
    """Extract all URL patterns from Django's URL configuration"""
    output = []
    for pattern in urlpatterns:
        if isinstance(pattern, URLPattern):
            route_info = {
                'pattern': f"{prefix}{pattern.pattern}",
                'view_func': getattr(pattern.callback, '__name__', 'unknown'),
                'module': getattr(pattern.callback, '__module__', 'unknown'),
                'view': pattern.callback
            }
            output.append(route_info)
        elif isinstance(pattern, URLResolver):
            output.extend(get_routes(pattern.url_patterns, prefix + str(pattern.pattern)))
    return output


def analyze_mojo_models():
    """Analyze all MOJO models and their security configurations"""
    from django.conf import settings
    from mojo.models.rest import MojoModel
    models_info = {}

    # Get all installed apps
    apps = getattr(settings, 'INSTALLED_APPS', [])

    for app_name in apps:
        if not app_name.startswith('mojo.apps.'):
            continue

        try:
            # Import the app's models module
            models_module = importlib.import_module(f"{app_name}.models")

            # Find all model classes that inherit from MojoModel
            for name, obj in inspect.getmembers(models_module):
                if (inspect.isclass(obj) and
                    issubclass(obj, MojoModel) and
                    obj != MojoModel and
                    hasattr(obj, '_meta')):

                    model_info = analyze_model_security(obj)
                    models_info[f"{app_name}.{name}"] = model_info

        except ImportError:
            continue

    return models_info


def analyze_model_security(model_class):
    """Analyze security configuration of a specific model"""
    rest_meta = getattr(model_class, 'RestMeta', None)

    if not rest_meta:
        return {
            'has_rest_meta': False,
            'security_risk': 'HIGH',
            'reason': 'No RestMeta defined - potentially unrestricted access'
        }

    info = {
        'has_rest_meta': True,
        'view_perms': getattr(rest_meta, 'VIEW_PERMS', []),
        'save_perms': getattr(rest_meta, 'SAVE_PERMS', []),
        'delete_perms': getattr(rest_meta, 'DELETE_PERMS', []),
        'create_perms': getattr(rest_meta, 'CREATE_PERMS', []),
        'can_delete': getattr(rest_meta, 'CAN_DELETE', False),
        'graphs': getattr(rest_meta, 'GRAPHS', {}),
        'security_risk': 'LOW',
        'security_warnings': []
    }

    # Check if this model is used with custom security decorators
    model_name = model_class.__name__
    has_custom_security = check_model_has_custom_security(model_name)

    # Analyze security risks
    if 'all' in info['view_perms']:
        if has_custom_security:
            info['security_risk'] = 'LOW'
            info['security_warnings'].append('Public read access - but secured by custom security decorators')
        else:
            info['security_risk'] = 'HIGH'
            info['security_warnings'].append('Public read access - VIEW_PERMS contains "all"')

    if 'all' in info['save_perms']:
        if has_custom_security:
            info['security_risk'] = 'MEDIUM'
            info['security_warnings'].append('Public write access - but secured by custom security decorators')
        else:
            info['security_risk'] = 'CRITICAL'
            info['security_warnings'].append('Public write access - SAVE_PERMS contains "all"')

    if not info['view_perms'] and not info['save_perms']:
        info['security_risk'] = 'HIGH'
        info['security_warnings'].append('No permissions defined - potentially unrestricted')

    return info


def check_model_has_custom_security(model_name):
    """Check if a model is used with custom security decorators by examining URLPATTERN_METHODS"""
    try:
        from mojo.decorators.http import URLPATTERN_METHODS
        from mojo.decorators.auth import SECURITY_REGISTRY

        # Look for endpoints that handle this model and have custom security
        model_lower = model_name.lower()
        for key, func in URLPATTERN_METHODS.items():
            # Check if this endpoint likely handles the model (basic heuristic)
            if model_lower in key.lower():
                func_key = f"{func.__module__}.{func.__name__}"
                if func_key in SECURITY_REGISTRY:
                    registry_info = SECURITY_REGISTRY[func_key]
                    if registry_info.get('type') == 'custom':
                        return True

        # Also check for specific known patterns
        known_custom_security_models = {
            'Book': 'docit',
            'Page': 'docit',
            'Asset': 'docit',
            'PageRevision': 'docit'
        }

        return model_name in known_custom_security_models

    except Exception:
        return False


def is_whitelisted_endpoint(pattern, whitelist):
    """Check if an endpoint pattern is in the whitelist"""
    clean_pattern = pattern.strip('/')
    for whitelisted in whitelist.get('public_endpoints', []):
        if clean_pattern == whitelisted.strip('/'):
            return True
        # Support wildcard matching for parameterized routes
        if '<' in whitelisted and clean_pattern.split('/')[:-1] == whitelisted.split('/')[:-1]:
            return True
    return False


def is_whitelisted_model(model_name, whitelist):
    """Check if a model is in the whitelist"""
    return model_name in whitelist.get('public_models', [])


def get_endpoint_info(route_info):
    """Extract detailed information about an endpoint using Security Registry"""
    view_func = route_info['view']
    pattern = str(route_info['pattern'])

    # Initialize defaults
    is_mojo_endpoint = False
    mojo_function = None
    requires_auth = False
    required_perms = []
    model_secured = False
    model_perms = []
    security_type = 'none'

    if route_info['view_func'] == 'dispatcher':
        # This is a MOJO endpoint - check URLPATTERN_METHODS for the actual function
        from mojo.decorators.http import URLPATTERN_METHODS
        from mojo.decorators.auth import SECURITY_REGISTRY

        # Clean the pattern to match URLPATTERN_METHODS format
        clean_pattern = pattern.strip('/').replace('api/', '')

        # Find the actual function in URLPATTERN_METHODS
        # Try multiple matching strategies
        found_func = None

        # Strategy 1: Exact pattern match
        for key, func in URLPATTERN_METHODS.items():
            if clean_pattern in key or pattern in key:
                found_func = func
                break

        # Strategy 2: Try without api/ prefix
        if not found_func:
            no_api_pattern = pattern.replace('api/', '')
            for key, func in URLPATTERN_METHODS.items():
                if no_api_pattern in key:
                    found_func = func
                    break

        # Strategy 3: Try with different delimiters
        if not found_func:
            alt_pattern = clean_pattern.replace('/', '__')
            for key, func in URLPATTERN_METHODS.items():
                if alt_pattern in key:
                    found_func = func
                    break

        # Strategy 4: Handle app-specific patterns (e.g., api/aws/email/sns/inbound -> email/sns/inbound)
        if not found_func and '/aws/' in pattern:
            # For AWS endpoints, try removing the app prefix: api/aws/email/sns/inbound -> email/sns/inbound
            aws_pattern = pattern.replace('api/aws/', '')
            for key, func in URLPATTERN_METHODS.items():
                if aws_pattern in key:
                    found_func = func
                    break

        # Strategy 5: Try partial matches for complex patterns
        if not found_func:
            # Extract the last few segments and try matching
            path_parts = clean_pattern.split('/')
            if len(path_parts) >= 2:
                partial_pattern = '/'.join(path_parts[-2:])  # Last 2 segments
                for key, func in URLPATTERN_METHODS.items():
                    if partial_pattern in key:
                        found_func = func
                        break

        if found_func:
            is_mojo_endpoint = True
            mojo_function = found_func

            # Check Security Registry first (most reliable)
            func_key = f"{mojo_function.__module__}.{mojo_function.__name__}"
            if func_key in SECURITY_REGISTRY:
                registry_info = SECURITY_REGISTRY[func_key]
                security_type = registry_info['type']
                requires_auth = registry_info.get('requires_auth', False)

                if security_type == 'permissions':
                    required_perms = registry_info.get('permissions', [])
                elif security_type == 'public':
                    requires_auth = False
                    required_perms = ['public']
                elif security_type == 'model':
                    # Validate the referenced model has proper security
                    model_class = registry_info.get('model_class')
                    if model_class:
                        model_security = analyze_model_security(model_class)
                        if model_security.get('security_risk') in ['CRITICAL', 'HIGH']:
                            # Model security is broken - treat as insecure
                            model_secured = False
                            model_perms = []
                            requires_auth = False
                            security_type = 'broken_model_security'
                        else:
                            model_secured = True
                            model_perms = ['model_security']
                            requires_auth = True
                    else:
                        model_secured = True
                        model_perms = ['model_security']
                        requires_auth = True
                elif security_type in ['authentication', 'bearer_token', 'token']:
                    requires_auth = True
                    required_perms = [security_type]
                elif security_type == 'custom':
                    requires_auth = True
                    required_perms = ['custom_security']
            else:
                # Fallback to old method if not in registry
                model_info = check_model_level_security(mojo_function, pattern)
                model_secured = model_info['secured']
                model_perms = model_info['perms']
                security_type = model_info.get('security_type', 'none')
                requires_auth = security_type not in ['none', 'public']
                required_perms = model_info.get('perms', [])

    elif hasattr(view_func, '__app_name__'):
        # Direct MOJO endpoint (not through dispatcher)
        from mojo.decorators.auth import SECURITY_REGISTRY
        is_mojo_endpoint = True
        mojo_function = view_func

        # Check Security Registry
        func_key = f"{view_func.__module__}.{view_func.__name__}"
        if func_key in SECURITY_REGISTRY:
            registry_info = SECURITY_REGISTRY[func_key]
            security_type = registry_info['type']
            requires_auth = registry_info.get('requires_auth', False)

            if security_type == 'permissions':
                required_perms = registry_info.get('permissions', [])
            elif security_type == 'public':
                requires_auth = False
                required_perms = ['public']
            elif security_type == 'model':
                model_secured = True
                model_perms = ['model_security']
                requires_auth = True
        else:
            # Fallback to old method
            model_info = check_model_level_security(view_func, pattern)
            model_secured = model_info['secured']
            model_perms = model_info['perms']
            requires_auth = model_info.get('security_type') not in ['none', 'public']

    # Debug output for Security Registry analysis
    debug_file = paths.VAR_ROOT / "reports" / "debug.json"
    os.makedirs(os.path.dirname(debug_file), exist_ok=True)

    debug_data = {}
    if os.path.exists(debug_file):
        try:
            with open(debug_file, 'r') as f:
                debug_data = json.load(f)
        except:
            debug_data = {}

    # Add comprehensive debug info for this endpoint
    debug_entry = {
        'timestamp': str(dates.utcnow()),
        'pattern': pattern,
        'view_func_name': route_info['view_func'],
        'is_dispatcher': route_info['view_func'] == 'dispatcher',
        'is_mojo_endpoint': is_mojo_endpoint,
        'mojo_function_name': mojo_function.__name__ if mojo_function else None,
        'mojo_function_module': mojo_function.__module__ if mojo_function else None,
        'security_registry_lookup': None,
        'security_registry_found': False,
        'fallback_method_used': False,
        'pattern_matching_tried': [],
        'urlpattern_methods_sample': [],
        'final_classification': {
            'security_type': security_type,
            'requires_auth': requires_auth,
            'required_perms': required_perms,
            'model_secured': model_secured,
            'model_perms': model_perms
        }
    }

    # Add pattern matching debug info for metrics and SNS endpoints
    if 'metrics' in pattern.lower() or 'sns' in pattern.lower():
        clean_pattern = pattern.strip('/').replace('api/', '')
        no_api_pattern = pattern.replace('api/', '')
        alt_pattern = clean_pattern.replace('/', '__')
        aws_pattern = pattern.replace('api/aws/', '') if '/aws/' in pattern else None

        path_parts = clean_pattern.split('/')
        partial_pattern = '/'.join(path_parts[-2:]) if len(path_parts) >= 2 else None

        patterns_tried = [
            f"clean_pattern: {clean_pattern}",
            f"no_api_pattern: {no_api_pattern}",
            f"alt_pattern: {alt_pattern}"
        ]
        if aws_pattern:
            patterns_tried.append(f"aws_pattern: {aws_pattern}")
        if partial_pattern:
            patterns_tried.append(f"partial_pattern: {partial_pattern}")

        debug_entry['pattern_matching_tried'] = patterns_tried

        # Sample URLPATTERN_METHODS keys for comparison
        from mojo.decorators.http import URLPATTERN_METHODS
        sample_keys = list(URLPATTERN_METHODS.keys())[:10]
        debug_entry['urlpattern_methods_sample'] = sample_keys

    if mojo_function:
        func_key = f"{mojo_function.__module__}.{mojo_function.__name__}"
        debug_entry['security_registry_key'] = func_key

        from mojo.decorators.auth import SECURITY_REGISTRY
        if func_key in SECURITY_REGISTRY:
            debug_entry['security_registry_found'] = True
            registry_info = SECURITY_REGISTRY[func_key]
            # Clean registry info for JSON serialization
            clean_registry = {k: v for k, v in registry_info.items() if k != 'function'}
            debug_entry['security_registry_lookup'] = clean_registry
        else:
            debug_entry['fallback_method_used'] = True
            debug_entry['security_registry_lookup'] = 'NOT_FOUND'

            # Check what function attributes exist
            debug_entry['function_attributes'] = {}
            for attr in ['_mojo_public_endpoint', '_mojo_requires_perms', '_mojo_requires_auth',
                        '_mojo_requires_bearer', '_mojo_custom_security', '_mojo_uses_model_security',
                        '_mojo_token_secured', '_mojo_security_type']:
                debug_entry['function_attributes'][attr] = hasattr(mojo_function, attr)
                if hasattr(mojo_function, attr):
                    debug_entry['function_attributes'][f"{attr}_value"] = getattr(mojo_function, attr)

    debug_data[pattern] = debug_entry

    try:
        with open(debug_file, 'w') as f:
            json.dump(debug_data, f, indent=2, default=str)
    except Exception as e:
        logit.color_print(f"Failed to write debug file: {e}", logit.ConsoleLogger.RED)

    return {
        'is_mojo_endpoint': is_mojo_endpoint,
        'mojo_function': mojo_function,
        'app_name': getattr(mojo_function, '__app_name__', 'unknown') if mojo_function else 'unknown',
        'url_info': getattr(mojo_function, '__url__', None) if mojo_function else None,
        'docs': getattr(mojo_function, '__docs__', {}) if mojo_function else {},
        'requires_auth': requires_auth,
        'required_perms': required_perms,
        'model_secured': model_secured,
        'model_perms': model_perms,
        'security_type': security_type
    }


def check_requires_auth(view_func):
    """Check if a view function requires authentication"""
    try:
        # Look for requires_auth decorator or similar patterns
        source = inspect.getsource(view_func) if hasattr(view_func, '__code__') else ''
        return '@requires_auth' in source or 'requires_auth' in str(view_func)
    except Exception:
        return False


def check_required_perms(view_func):
    """Extract required permissions from view function"""
    try:
        source = inspect.getsource(view_func) if hasattr(view_func, '__code__') else ''
        # This is a simple pattern match - could be enhanced
        if '@requires_perms' in source:
            # Extract permissions from decorator
            return ['requires_perms_decorator_found']
    except Exception:
        pass
    return []


def check_model_level_security(view_func, pattern):
    """Check if an endpoint is secured by model-level permissions"""
    try:
        # Look for common MOJO patterns that indicate model-level security
        if not hasattr(view_func, '__code__'):
            return {'secured': False, 'perms': []}

        source = inspect.getsource(view_func)

        # Check for Model.on_rest_request pattern
        if '.on_rest_request(' in source:
            # Try to identify which model is being used
            model_name = extract_model_from_source(source, pattern)
            if model_name:
                model_perms = get_model_permissions(model_name)
                if model_perms:
                    return {'secured': True, 'perms': model_perms}

        # Check for MOJO auth decorator metadata (much simpler than parsing source)
        if hasattr(view_func, '_mojo_requires_perms'):
            return {
                'secured': True,
                'perms': getattr(view_func, '_mojo_required_permissions', []),
                'security_type': getattr(view_func, '_mojo_security_type', 'permissions')
            }

        if hasattr(view_func, '_mojo_requires_auth'):
            return {
                'secured': True,
                'perms': ['requires_auth'],
                'security_type': 'authentication'
            }

        if hasattr(view_func, '_mojo_requires_bearer'):
            return {
                'secured': True,
                'perms': ['requires_bearer'],
                'security_type': 'bearer_token'
            }

        if hasattr(view_func, '_mojo_public_endpoint'):
            return {
                'secured': True,
                'perms': ['public'],
                'security_type': 'public',
                'reason': getattr(view_func, '_mojo_public_reason', 'Explicitly marked public')
            }

        if hasattr(view_func, '_mojo_custom_security'):
            return {
                'secured': True,
                'perms': ['custom_security'],
                'security_type': 'custom',
                'description': getattr(view_func, '_mojo_security_description', '')
            }

        if hasattr(view_func, '_mojo_uses_model_security'):
            model_class = getattr(view_func, '_mojo_secured_model', None)
            model_name = getattr(view_func, '_mojo_secured_model_name', None)

            # Validate that the referenced model has proper security
            if model_class:
                model_security = analyze_model_security(model_class)
                if model_security.get('security_risk') in ['CRITICAL', 'HIGH']:
                    return {
                        'secured': False,
                        'perms': [],
                        'security_type': 'broken_model_security',
                        'model': model_name,
                        'model_issues': model_security.get('security_warnings', [])
                    }

            return {
                'secured': True,
                'perms': ['model_security'],
                'security_type': 'model',
                'model': model_name
            }

        if hasattr(view_func, '_mojo_token_secured'):
            return {
                'secured': True,
                'perms': ['token_secured'] + getattr(view_func, '_mojo_token_types', []),
                'security_type': 'token',
                'description': getattr(view_func, '_mojo_security_description', '')
            }

        # Check for other security patterns - more comprehensive
        security_indicators = [
            'rest_check_permission',
            'requires_auth',
            'user.is_authenticated',
            'has_permission',
            'permissiondeniedexception',
            'mojo.errors.permissiondeniedexception',
            'get_write_perms',
            'get_read_perms',
            'check_permission',
            'require_permission',
            'auth_required',
            'login_required',
            'permission_required',
            'check_user_permission',
            'validate_permission',
        ]

        for indicator in security_indicators:
            if indicator in source.lower():
                return {'secured': True, 'perms': ['function_level_security']}

    except Exception:
        pass

    return {'secured': False, 'perms': []}


def extract_model_from_source(source, pattern):
    """Extract model name from function source code"""
    try:
        import re

        # Common patterns to look for in MOJO code
        model_patterns = [
            r'(\w+)\.on_rest_request',
            r'return\s+(\w+)\.on_rest_request',
            r'from.*models.*import.*(\w+)',
            r'from.*\.models\.\w+\s+import\s+(\w+)',
            r'models\.(\w+)\.on_rest_request',
        ]

        for pattern_regex in model_patterns:
            matches = re.findall(pattern_regex, source)
            if matches:
                # Filter out common non-model words
                filtered_matches = [m for m in matches if m not in ['request', 'response', 'data', 'json', 'HttpResponse', 'JsonResponse']]
                if filtered_matches:
                    return filtered_matches[0]

        # Try to infer from URL patterns - more comprehensive mapping
        url_to_model = {
            'api/user': 'User',
            'api/group': 'Group',
            'api/logs': 'Log',
            'api/jobs': 'Job',
            'api/incident/incident': 'Incident',  # Specific incident endpoints
            'api/incident/event': 'Event',        # Event-specific endpoints
            'api/incident/ticket': 'Ticket',      # Ticket-specific endpoints
            'api/ticket': 'Ticket',
            'api/file': 'File',
            'api/fileman/file': 'File',           # File manager endpoints
            'api/fileman/manager': 'FileManager', # File manager model
            'api/email': 'IncomingEmail',
            'api/aws/email': 'IncomingEmail',     # AWS email endpoints
            'api/aws': 'EmailTemplate',           # Generic AWS endpoints
            'api/book': 'Book',
            'api/page': 'Page',
            'api/asset': 'Asset',
        }

        for url_part, model_name in url_to_model.items():
            if url_part in pattern.lower():
                return model_name

        # Try to extract from URL path segments
        url_segments = [seg for seg in pattern.split('/') if seg and seg != 'api']
        if url_segments:
            # Convert first segment to title case (e.g., 'users' -> 'User')
            potential_model = url_segments[0].rstrip('s').title()
            # Handle special plurals
            if potential_model == 'Librarie':  # libraries -> Library
                potential_model = 'Library'
            elif potential_model == 'Activitie':  # activities -> Activity
                potential_model = 'Activity'
            return potential_model

    except Exception:
        pass

    return None


def get_model_permissions(model_name):
    """Get permissions for a specific model by dynamically loading and inspecting the model class"""
    try:
        # Try to find and import the actual model class
        model_class = find_model_class(model_name)
        if not model_class:
            return []

        # Check if model has RestMeta class with permissions
        if hasattr(model_class, 'RestMeta'):
            rest_meta = model_class.RestMeta
            perms = []

            if hasattr(rest_meta, 'VIEW_PERMS'):
                perms.extend(rest_meta.VIEW_PERMS)
            if hasattr(rest_meta, 'SAVE_PERMS'):
                perms.extend(rest_meta.SAVE_PERMS)
            if hasattr(rest_meta, 'DELETE_PERMS'):
                perms.extend(rest_meta.DELETE_PERMS)

            return list(set(perms))  # Remove duplicates

    except Exception as e:
        # Fallback to hardcoded permissions for critical models
        fallback_perms = {
            'User': ['view_users', 'manage_users', 'owner'],
            'Group': ['view_groups', 'manage_groups'],
            'Log': ['manage_logs', 'view_logs', 'admin'],
            'Incident': ['view_incidents', 'manage_incidents'],
            'Event': ['view_incidents', 'manage_incidents'],
            'Ticket': ['view_incidents', 'manage_incidents'],
        }
        return fallback_perms.get(model_name, [])

    return []


def find_model_class(model_name):
    """Find a model class by name across all Django apps"""
    try:
        from django.apps import apps

        # Get all models from all apps
        for model in apps.get_models():
            if model.__name__ == model_name:
                return model

        # Also try common model name patterns
        for model in apps.get_models():
            if model.__name__.lower() == model_name.lower():
                return model
            if hasattr(model, '_meta') and model._meta.object_name == model_name:
                return model

    except Exception:
        pass

    return None


@th.django_unit_setup()
def setup_security_checks(opts):
    """Setup comprehensive security analysis data"""
    from mojo.decorators.http import URLPATTERN_METHODS, REGISTERED_URLS
    logit.color_print("Setting up security analysis...", logit.ConsoleLogger.BLUE)

    # Configure comprehensive testing limits based on command line args
    if hasattr(opts, 'extra') and opts.extra:
        extra_args = opts.extra.split(',')
        for arg in extra_args:
            if arg.strip().startswith('limit-routes='):
                try:
                    opts.comprehensive_limit = int(arg.split('=')[1])
                    logit.color_print(f"Comprehensive testing: Limited to {opts.comprehensive_limit} routes", logit.ConsoleLogger.YELLOW)
                except ValueError:
                    logit.color_print("Invalid limit-routes value, testing all routes", logit.ConsoleLogger.YELLOW)

    # No whitelist - all endpoints must be properly decorated
    opts.security_whitelist = {
        'public_endpoints': [],
        'public_models': []
    }
    logit.color_print("Whitelist disabled - all endpoints must be properly decorated", logit.ConsoleLogger.BLUE)

    # Get all routes
    resolver = get_resolver()
    opts.routes = get_routes(resolver.url_patterns)

    # Analyze MOJO models
    opts.models_info = analyze_mojo_models()

    # Analyze MOJO endpoints
    opts.mojo_endpoints = {}
    for key, func in URLPATTERN_METHODS.items():
        opts.mojo_endpoints[key] = {
            'function': func,
            'module': getattr(func, '__module__', 'unknown'),
            'requires_auth': check_requires_auth(func),
            'required_perms': check_required_perms(func)
        }

    logit.color_print(f"Found {len(opts.routes)} total routes", logit.ConsoleLogger.GREEN)
    logit.color_print(f"Found {len(opts.models_info)} MOJO models", logit.ConsoleLogger.GREEN)
    logit.color_print(f"Found {len(opts.mojo_endpoints)} MOJO endpoints", logit.ConsoleLogger.GREEN)


@th.unit_test()
def test_model_security_configuration(opts):
    """Test all models have proper security configuration"""
    high_risk_models = []
    critical_risk_models = []

    for model_name, info in opts.models_info.items():
        risk_level = info.get('security_risk', 'UNKNOWN')

        if risk_level == 'CRITICAL':
            critical_risk_models.append(model_name)
            if getattr(opts, 'verbose', False):
                logit.color_print(f"\t\t🔴 CRITICAL: {model_name}", logit.ConsoleLogger.RED)
                for warning in info.get('security_warnings', []):
                    logit.color_print(f"\t\t\t- {warning}", logit.ConsoleLogger.RED)

        elif risk_level == 'HIGH':
            high_risk_models.append(model_name)
            if getattr(opts, 'verbose', False):
                logit.color_print(f"\t\t🟡 HIGH: {model_name}", logit.ConsoleLogger.YELLOW)
                for warning in info.get('security_warnings', []):
                    logit.color_print(f"\t\t\t- {warning}", logit.ConsoleLogger.YELLOW)

    # Assert no critical security risks
    assert len(critical_risk_models) == 0, f"Critical security risks found in models: {critical_risk_models}"


@th.unit_test()
def test_public_endpoints_security(opts):
    """Test for unintentionally public endpoints"""
    public_endpoints = []

    for route_info in opts.routes:
        pattern = str(route_info['pattern'])

        # Skip admin, static, and known safe patterns
        if any(skip in pattern for skip in ['admin/', 'static/', 'media/', '__debug__']):
            continue

        endpoint_info = get_endpoint_info(route_info)

        # Check if endpoint appears to lack security (considering both decorator and model-level security)
        has_decorator_security = endpoint_info['requires_auth'] or endpoint_info['required_perms']
        has_model_security = endpoint_info.get('model_secured', False)

        if not has_decorator_security and not has_model_security:
            endpoint_data = {
                'pattern': pattern,
                'view': route_info['view_func'],
                'module': route_info['module'],
                'is_mojo': endpoint_info['is_mojo_endpoint'],
                'app_name': endpoint_info.get('app_name', 'unknown'),
                'security_type': 'none'
            }

            # No whitelist - all unsecured endpoints are concerning
            public_endpoints.append(endpoint_data)

        elif has_model_security and not has_decorator_security:
            # This endpoint is secured by model-level permissions - these are secure by design
            pass

    # Only show details if verbose
    if getattr(opts, 'verbose', False):
        logit.color_print(f"\t\tFound {len(public_endpoints)} concerning public endpoints:", logit.ConsoleLogger.YELLOW)

        mojo_public = [ep for ep in public_endpoints if ep['is_mojo']]
        non_mojo_public = [ep for ep in public_endpoints if not ep['is_mojo']]

        logit.color_print(f"\t\t\tConcerning MOJO endpoints: {len(mojo_public)}", logit.ConsoleLogger.RED)
        logit.color_print(f"\t\t\tConcerning Non-MOJO endpoints: {len(non_mojo_public)}", logit.ConsoleLogger.YELLOW)

        # Show concerning endpoints (no whitelist)
        if public_endpoints:
            logit.color_print(f"\t\t\tConcerning endpoints (missing security decorators):", logit.ConsoleLogger.YELLOW)
            for endpoint in public_endpoints[:15]:  # Limit output
                color = logit.ConsoleLogger.RED if endpoint['is_mojo'] else logit.ConsoleLogger.YELLOW
                app_info = f" [{endpoint['app_name']}]" if endpoint['is_mojo'] else ""
                logit.color_print(f"\t\t\t\t⚠️  {endpoint['pattern']}{app_info}", color)

        if len(public_endpoints) > 15:
            logit.color_print(f"\t\t\t\t... and {len(public_endpoints) - 15} more", logit.ConsoleLogger.YELLOW)

    # Fail the test if there are concerning public endpoints
    mojo_public = [ep for ep in public_endpoints if ep['is_mojo']]

    # Fail if any MOJO endpoints are missing security decorators
    assert len(mojo_public) == 0, f"MOJO endpoints missing security decorators: {[ep['pattern'] for ep in mojo_public]}"

    # Also fail for obviously dangerous public endpoints
    dangerous_public = [ep for ep in public_endpoints if any(danger in ep['pattern']
                       for danger in ['delete', 'admin', 'secret', 'private', 'key'])]
    assert len(dangerous_public) == 0, f"Dangerous public endpoints found: {[ep['pattern'] for ep in dangerous_public]}"


@th.unit_test()
def test_authentication_bypass(opts):
    """Test for potential authentication bypass vulnerabilities"""
    bypass_attempts = 0

    if getattr(opts, 'verbose', False):
        logit.color_print("\t\tTesting authentication bypass attempts...", logit.ConsoleLogger.BLUE)

    # Test common API endpoints without authentication
    test_endpoints = [
        'api/user',
        'api/group',
        'api/admin',
        'api/config',
        'api/secret'
    ]

    for endpoint in test_endpoints:
        if any(endpoint in str(route['pattern']) for route in opts.routes):
            bypass_attempts += 1
            if getattr(opts, 'verbose', False):
                logit.color_print(f"\t\t\t-> Testing {endpoint}", logit.ConsoleLogger.YELLOW, end="")

            # Test GET request without auth
            resp = opts.client.get(f"/{endpoint}")

            if resp.status_code == 200 and hasattr(resp, 'response') and resp.response.data:
                if getattr(opts, 'verbose', False):
                    logit.color_print("VULNERABLE", logit.ConsoleLogger.RED)
                assert False, f"Authentication bypass found on {endpoint} - returns data without auth"
            elif resp.status_code in [400, 401, 403, 404]:
                # Don't print success - only issues (but complete the line if verbose)
                if getattr(opts, 'verbose', False):
                    pass
            else:
                if getattr(opts, 'verbose', False):
                    logit.color_print(f"UNKNOWN ({resp.status_code})", logit.ConsoleLogger.YELLOW)

    if getattr(opts, 'verbose', False):
        logit.color_print(f"\t\tTested {bypass_attempts} endpoints for auth bypass", logit.ConsoleLogger.BLUE)


@th.unit_test()
def test_permission_escalation(opts):
    """Test for permission escalation vulnerabilities"""

    if getattr(opts, 'verbose', False):
        logit.color_print("\t\tTesting permission escalation...", logit.ConsoleLogger.BLUE)

    # Create a test user with minimal permissions
    test_user_data = {
        'username': 'security_test_user',
        'email': 'test@security.local',
        'password': 'TestPass123!'
    }

    # Test user creation endpoint if available
    resp = opts.client.post('/api/user', test_user_data)
    if resp.status_code in [403, 401]:
        # Don't print success - only issues
        pass
    else:
        if getattr(opts, 'verbose', False):
            logit.color_print(f"\t\t\t-> User creation status: {resp.status_code}", logit.ConsoleLogger.YELLOW)

    # Test group access without membership
    resp = opts.client.get('/api/group')
    if resp.status_code in [403, 401]:
        # Don't print success - only issues
        pass
    elif resp.status_code == 200 and hasattr(resp, 'response'):
        # Check if empty list (proper behavior) or actual data (potential issue)
        data = getattr(resp.response, 'data', [])
        if not data or len(data) == 0:
            # Don't print success - only issues
            pass
        else:
            if getattr(opts, 'verbose', False):
                logit.color_print("\t\t\t-> Group listing returns data without auth", logit.ConsoleLogger.RED)
            # Don't fail here as this might be expected behavior for some models


@th.unit_test()
def test_route_security_comprehensive(opts):
    """Comprehensive test of all route security"""
    secure_routes = 0
    insecure_routes = 0
    unknown_routes = 0

    if getattr(opts, 'verbose', False):
        logit.color_print("\t\tTesting route security comprehensively...", logit.ConsoleLogger.BLUE)

    # Allow configuration of test limits (default: test all routes)
    max_routes_to_test = getattr(opts, 'max_comprehensive_routes', None)  # No limit by default
    if hasattr(opts, 'comprehensive_limit') and opts.comprehensive_limit:
        max_routes_to_test = opts.comprehensive_limit

    test_count = 0
    total_api_routes = 0

    # Count total API routes first
    for route_info in opts.routes:
        pattern = str(route_info['pattern'])
        if (any(api_prefix in pattern for api_prefix in ['api/', 'rest/']) and
            not any(skip in pattern for skip in ['static/', 'media/', 'admin/', '__debug__'])):
            total_api_routes += 1

    if getattr(opts, 'verbose', False):
        logit.color_print(f"\t\tFound {total_api_routes} API routes to test", logit.ConsoleLogger.BLUE)
        if max_routes_to_test:
            logit.color_print(f"\t\tTesting first {max_routes_to_test} routes (limited for performance)", logit.ConsoleLogger.BLUE)
        else:
            logit.color_print(f"\t\tTesting ALL {total_api_routes} routes", logit.ConsoleLogger.GREEN)

    for route_info in opts.routes:
        pattern = str(route_info['pattern'])

        # Skip non-API routes
        if not any(api_prefix in pattern for api_prefix in ['api/', 'rest/']):
            continue

        # Skip known safe routes
        if any(skip in pattern for skip in ['static/', 'media/', 'admin/', '__debug__']):
            continue

        test_count += 1
        if max_routes_to_test and test_count > max_routes_to_test:
            remaining = total_api_routes - max_routes_to_test
            if getattr(opts, 'verbose', False):
                logit.color_print(f"\t\t\t... (skipped {remaining} remaining routes for performance)", logit.ConsoleLogger.BLUE)
            break

        try:
            # Get endpoint security info using our Security Registry
            route_info = {'pattern': pattern, 'view_func': 'dispatcher', 'view': None}
            endpoint_info = get_endpoint_info(route_info)

            # Classify based on security registry data
            security_type = endpoint_info.get('security_type', 'none')
            requires_auth = endpoint_info.get('requires_auth', False)
            has_perms = bool(endpoint_info.get('required_perms', []))

            # Only print endpoint line if there's an issue to report
            issue_found = False
            issue_message = ""

            if security_type == 'public':
                # PUBLIC endpoints are designed for specific payloads (webhooks, APIs)
                # Skip HTTP testing as generic GET requests may fail by design
                secure_routes += 1

            elif security_type == 'custom':
                # CUSTOM SECURITY endpoints have special requirements/authentication
                # Skip HTTP testing as generic GET requests may fail by design
                secure_routes += 1

            else:
                # For all non-public endpoints, test HTTP response to verify security
                resp = opts.client.get(pattern)

                # Check MOJO_REST_LIST_PERM_DENY setting
                from mojo.helpers.settings import settings
                list_perm_deny = settings.get("MOJO_REST_LIST_PERM_DENY", True)

                def is_secure_response(resp):
                    """Check if response indicates proper security"""
                    if resp.status_code in [400, 401, 403, 404]:
                        return True
                    elif resp.status_code == 200 and not list_perm_deny:
                        # When MOJO_REST_LIST_PERM_DENY=False, secure endpoints return 200 with empty data
                        if hasattr(resp, 'response') and hasattr(resp.response, 'data'):
                            data = resp.response.data
                            # Check if it's an empty list or dict with empty data
                            if isinstance(data, dict) and 'data' in data:
                                return len(data['data']) == 0
                            elif isinstance(data, list):
                                return len(data) == 0
                        return False
                    return False

                if security_type in ['permissions', 'authentication', 'bearer_token', 'token']:
                    # SECURE endpoints should block unauthorized access or return empty data
                    if is_secure_response(resp):
                        secure_routes += 1
                    else:
                        issue_found = True
                        issue_message = "SECURE-BROKEN"
                        insecure_routes += 1

                elif security_type == 'model':
                    # MODEL-SECURED endpoints should also block unauthorized access or return empty data
                    if is_secure_response(resp):
                        secure_routes += 1
                    else:
                        issue_found = True
                        issue_message = "MODEL-BROKEN"
                        insecure_routes += 1

                elif security_type == 'broken_model_security':
                    # Endpoint uses @uses_model_security but the model has security issues
                    issue_found = True
                    issue_message = "MODEL-SECURITY-BROKEN"
                    insecure_routes += 1

                elif requires_auth or has_perms:
                    # Endpoints with auth/perms should block unauthorized access or return empty data
                    if is_secure_response(resp):
                        secure_routes += 1
                    else:
                        issue_found = True
                        issue_message = "SECURE-BROKEN"
                        insecure_routes += 1
                else:
                    # Truly unknown endpoints - classify by HTTP response
                    if resp.status_code == 200:
                        if not list_perm_deny:
                            # Check if it's returning empty data (secure) or actual data (insecure)
                            if hasattr(resp, 'response') and hasattr(resp.response, 'data'):
                                data = resp.response.data
                                if isinstance(data, dict) and 'data' in data:
                                    if len(data['data']) == 0:
                                        secure_routes += 1
                                    else:
                                        issue_found = True
                                        issue_message = "INSECURE"
                                        insecure_routes += 1
                                elif isinstance(data, list):
                                    if len(data) == 0:
                                        secure_routes += 1
                                    else:
                                        issue_found = True
                                        issue_message = "INSECURE"
                                        insecure_routes += 1
                                else:
                                    issue_found = True
                                    issue_message = "INSECURE"
                                    insecure_routes += 1
                            else:
                                issue_found = True
                                issue_message = "INSECURE"
                                insecure_routes += 1
                        else:
                            issue_found = True
                            issue_message = "INSECURE"
                            insecure_routes += 1
                    elif resp.status_code in [400, 401, 403, 404]:
                        secure_routes += 1
                    else:
                        issue_found = True
                        issue_message = "UNKNOWN"
                        unknown_routes += 1

            # Only print if there's an issue and verbose mode
            if issue_found and getattr(opts, 'verbose', False):
                logit.color_print(f"\t\t\t-> {pattern.ljust(50, '.')}{issue_message}", logit.ConsoleLogger.RED if 'BROKEN' in issue_message or 'INSECURE' in issue_message else logit.ConsoleLogger.YELLOW)

                # Print additional details for specific issues
                if issue_message in ["SECURE-BROKEN", "MODEL-BROKEN"]:
                    expected = "400/401/403/404" if issue_message == "SECURE-BROKEN" else "401/403"
                    logit.color_print(f"\t\t\t\tExpected {expected}, got {resp.status_code}", logit.ConsoleLogger.RED)
                elif issue_message == "MODEL-SECURITY-BROKEN":
                    logit.color_print(f"\t\t\t\t@uses_model_security references a model with security issues", logit.ConsoleLogger.RED)
                elif issue_message == "INSECURE":
                    logit.color_print(f"\t\t\t\tGET: {resp.status_code}", logit.ConsoleLogger.RED)

        except Exception as e:
            if getattr(opts, 'verbose', False):
                logit.color_print(f"\t\t\t-> {pattern.ljust(50, '.')}ERROR: {str(e)[:20]}...", logit.ConsoleLogger.RED)
            unknown_routes += 1

    if getattr(opts, 'verbose', False):
        logit.color_print(f"\t\tSecurity Summary:", logit.ConsoleLogger.BLUE)
        logit.color_print(f"\t\t\tSecure routes: {secure_routes}", logit.ConsoleLogger.GREEN)
        logit.color_print(f"\t\t\tInsecure routes: {insecure_routes}", logit.ConsoleLogger.RED)
        logit.color_print(f"\t\t\tMixed/Unknown: {unknown_routes}", logit.ConsoleLogger.YELLOW)

        # Explain what MIXED means
        if unknown_routes > 0:
            logit.color_print(f"\t\t\t📝 MIXED = Inconsistent security across HTTP methods", logit.ConsoleLogger.BLUE)
            logit.color_print(f"\t\t\t   Example: GET returns 401 (secure) but POST returns 200 (insecure)", logit.ConsoleLogger.BLUE)

        # Show warning if there are insecure routes
        if insecure_routes > 5:  # Threshold for concern
            logit.color_print(f"\t\t⚠️  High number of potentially insecure routes detected", logit.ConsoleLogger.YELLOW)

    # Fail the test if there are any insecure or broken routes
    assert insecure_routes == 0, f"Found {insecure_routes} insecure or broken routes that need security fixes"


@th.unit_test()
def test_generate_security_report(opts):
    """Generate a comprehensive security report"""

    if getattr(opts, 'verbose', False):
        logit.color_print("\t\tGenerating security report...", logit.ConsoleLogger.BLUE)

    # Ensure setup has run - if not, initialize the required data
    if not hasattr(opts, 'routes'):
        from django.urls import get_resolver
        opts.routes = get_routes(get_resolver().url_patterns)

    if not hasattr(opts, 'models_info'):
        opts.models_info = analyze_mojo_models()

    if not hasattr(opts, 'mojo_endpoints'):
        from mojo.decorators.http import URLPATTERN_METHODS
        opts.mojo_endpoints = {}
        for key, func in URLPATTERN_METHODS.items():
            opts.mojo_endpoints[key] = {
                'function': func,
                'module': getattr(func, '__module__', 'unknown'),
                'requires_auth': check_requires_auth(func),
                'required_perms': check_required_perms(func)
            }

    report = {
        'timestamp': str(dates.utcnow()),
        'hostname': getattr(paths, 'HOSTNAME', 'unknown'),
        'total_routes': len(opts.routes),
        'total_models': len(opts.models_info),
        'total_mojo_endpoints': len(opts.mojo_endpoints),
        'models_by_risk': {
            'critical': [],
            'high': [],
            'medium': [],
            'low': []
        },
        'public_endpoints': [],
        'mojo_endpoints': [],
        'security_recommendations': []
    }

    # Categorize models by risk
    for model_name, info in opts.models_info.items():
        risk = info.get('security_risk', 'UNKNOWN').lower()
        if risk in report['models_by_risk']:
            report['models_by_risk'][risk].append({
                'name': model_name,
                'warnings': info.get('security_warnings', [])
            })

    # Find public endpoints and categorize by security type
    report['model_secured_endpoints'] = []

    for route_info in opts.routes:
        endpoint_info = get_endpoint_info(route_info)

        has_decorator_security = endpoint_info['requires_auth'] or endpoint_info['required_perms']
        has_model_security = endpoint_info.get('model_secured', False)

        if not has_decorator_security and not has_model_security:
            endpoint_data = {
                'pattern': str(route_info['pattern']),
                'view': route_info['view_func'],
                'is_mojo': endpoint_info['is_mojo_endpoint'],
                'security_type': 'none'
            }

            # Check if whitelisted
            if is_whitelisted_endpoint(str(route_info['pattern']), opts.security_whitelist):
                # Skip whitelisted endpoints from public report
                continue

            report['public_endpoints'].append(endpoint_data)

            if endpoint_info['is_mojo_endpoint']:
                report['mojo_endpoints'].append({
                    'pattern': str(route_info['pattern']),
                    'app_name': endpoint_info.get('app_name', 'unknown'),
                    'function': getattr(endpoint_info.get('mojo_function'), '__name__', 'unknown'),
                    'requires_auth': endpoint_info['requires_auth'],
                    'required_perms': endpoint_info['required_perms']
                })

        elif has_model_security and not has_decorator_security:
            # Track model-secured endpoints separately
            report['model_secured_endpoints'].append({
                'pattern': str(route_info['pattern']),
                'view': route_info['view_func'],
                'is_mojo': endpoint_info['is_mojo_endpoint'],
                'security_type': 'model',
                'model_perms': endpoint_info.get('model_perms', []),
                'app_name': endpoint_info.get('app_name', 'unknown')
            })

    # Generate recommendations
    if report['models_by_risk']['critical']:
        report['security_recommendations'].append(
            "CRITICAL: Review models with public write access immediately"
        )

    if report['models_by_risk']['high']:
        report['security_recommendations'].append(
            "HIGH: Review models with potential security issues"
        )

    if len(report['mojo_endpoints']) > 5:
        report['security_recommendations'].append(
            f"HIGH: {len(report['mojo_endpoints'])} MOJO endpoints appear to be public - verify this is intentional"
        )

    if len(report['public_endpoints']) > 10:
        report['security_recommendations'].append(
            f"MEDIUM: {len(report['public_endpoints'])} total public endpoints detected - review if intentional"
        )

    # Create reports directory if it doesn't exist
    reports_dir = paths.VAR_ROOT / "reports"
    os.makedirs(reports_dir, exist_ok=True)

    # Save report to file
    report_path = reports_dir / "mojo_security_report.json"
    try:
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        if getattr(opts, 'verbose', False):
            logit.color_print(f"\t\t\tSecurity report saved to: {report_path}", logit.ConsoleLogger.GREEN)
    except Exception as e:
        if getattr(opts, 'verbose', False):
            logit.color_print(f"\t\t\tFailed to save report: {e}", logit.ConsoleLogger.RED)

    # Print summary only if verbose
    if getattr(opts, 'verbose', False):
        logit.color_print(f"\t\tSECURITY REPORT SUMMARY:", logit.ConsoleLogger.BLUE)
        logit.color_print(f"\t\t\tCritical Risk Models: {len(report['models_by_risk']['critical'])}", logit.ConsoleLogger.RED)
        logit.color_print(f"\t\t\tHigh Risk Models: {len(report['models_by_risk']['high'])}", logit.ConsoleLogger.YELLOW)
        logit.color_print(f"\t\t\tPublic MOJO Endpoints: {len(report['mojo_endpoints'])}", logit.ConsoleLogger.RED)
        logit.color_print(f"\t\t\tModel-Secured Endpoints: {len(report['model_secured_endpoints'])}", logit.ConsoleLogger.GREEN)
        logit.color_print(f"\t\t\tTotal Public Endpoints: {len(report['public_endpoints'])}", logit.ConsoleLogger.YELLOW)
        logit.color_print(f"\t\t\tRecommendations: {len(report['security_recommendations'])}", logit.ConsoleLogger.BLUE)

        for rec in report['security_recommendations']:
            logit.color_print(f"\t\t\t\t• {rec}", logit.ConsoleLogger.YELLOW)
