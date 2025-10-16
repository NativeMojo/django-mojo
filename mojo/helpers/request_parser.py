import ujson
from typing import Any, Dict, List
from django.http import QueryDict
from objict import objict, nobjict


class RequestDataParser:
    """
    A robust parser for Django request data that consolidates GET, POST, JSON, and FILES
    into a single objict with support for dotted notation and array handling.
    """

    def __init__(self, use_objict=True):
        """
        Initialize parser.

        Args:
            use_objict: Whether to use objict() or regular dict() for nested structures
        """
        self.dict_factory = objict if use_objict else dict

    def parse(self, request) -> 'objict':
        """
        Main entry point - parses all request data into a single objict.

        Args:
            request: Django HttpRequest object

        Returns:
            objict containing all parsed request data
        """
        result = self.dict_factory()

        # Process in order of precedence (later sources can override earlier ones)
        self._process_query_params(request.GET, result)
        self._process_form_data(request, result)
        self._process_json_data(request, result)
        self._process_files(request.FILES, result)

        return result

    def _process_query_params(self, query_dict: QueryDict, target: 'objict') -> None:
        """Process GET parameters from QueryDict."""
        if not query_dict:
            return

        for key in query_dict.keys():
            values = query_dict.getlist(key)
            normalized_key = self._normalize_key(key)
            self._set_nested_value(target, normalized_key, values)

    def _process_form_data(self, request, target: 'objict') -> None:
        """Process POST form data (non-JSON requests)."""
        if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            return

        # Skip if this is a JSON request
        content_type = getattr(request, 'content_type', '').lower()
        if 'application/json' in content_type:
            return

        if not request.POST:
            return

        for key in request.POST.keys():
            values = request.POST.getlist(key)
            normalized_key = self._normalize_key(key)
            self._set_nested_value(target, normalized_key, values)

    def _process_json_data(self, request, target: 'objict') -> None:
        """Process JSON request body."""
        if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            return

        content_type = getattr(request, 'content_type', '').lower()
        if 'application/json' not in content_type:
            return

        try:
            if hasattr(request, 'body') and request.body:
                json_data = ujson.loads(request.body.decode('utf-8'))
                if isinstance(json_data, dict):
                    self._merge_dict_data(json_data, target)
                else:
                    # Handle case where JSON root is not an object
                    target['data'] = json_data
        except Exception as e:
            # Store error info for debugging
            target['_json_parse_error'] = str(e)

    def _process_files(self, files_dict, target: 'objict') -> None:
        """Process uploaded files."""
        if not files_dict:
            return

        files_obj = nobjict()

        for key in files_dict.keys():
            files = files_dict.getlist(key)
            normalized_key = self._normalize_key(key)

            # Store single file directly, multiple files as list
            file_value = files[0] if len(files) == 1 else files
            self._set_nested_value(files_obj, normalized_key, [file_value])

        if files_obj:
            target['files'] = files_obj

    def _normalize_key(self, key: str) -> str:
        """
        Normalize keys by removing array notation brackets.

        Examples:
            'tags[]' -> 'tags'
            'user[name][]' -> 'user.name'
            'items[0][name]' -> 'items.0.name'
        """
        # Remove trailing []
        key = key.rstrip('[]')

        # Convert bracket notation to dot notation
        # user[name] -> user.name
        # items[0][name] -> items.0.name
        import re
        key = re.sub(r'\[([^\]]+)\]', r'.\1', key)

        return key

    def _merge_dict_data(self, source: Dict[str, Any], target: 'objict') -> None:
        """Recursively merge dictionary data into target objict."""
        for key, value in source.items():
            self._set_nested_value(target, key, [value])

    def _set_nested_value(self, target: 'objict', dotted_key: str, values: List[Any]) -> None:
        """
        Set a value in nested objict structure using dotted key notation.

        Args:
            target: Target objict to modify
            dotted_key: Key like 'user.profile.name' or 'tags'
            values: List of values (handles single values and arrays)
        """
        if not dotted_key:
            return

        parts = dotted_key.split('.')
        current = target

        # Navigate to the parent of the final key
        for part in parts[:-1]:
            if not part:  # Skip empty parts from double dots
                continue

            if part not in current:
                current[part] = self.dict_factory()
            elif not isinstance(current[part], (dict, objict, nobjict)):
                # Convert existing non-dict value to dict to avoid conflicts
                current[part] = self.dict_factory()

            current = current[part]

        final_key = parts[-1]
        if not final_key:  # Handle edge case of trailing dot
            return

        # Determine final value based on number of values
        if len(values) == 0:
            final_value = None
        elif len(values) == 1:
            final_value = values[0]
        else:
            final_value = values

        # Handle existing values - merge into arrays if needed
        if final_key in current:
            existing = current[final_key]
            if isinstance(existing, list):
                if isinstance(final_value, list):
                    existing.extend(final_value)
                else:
                    existing.append(final_value)
            else:
                if isinstance(final_value, list):
                    current[final_key] = [existing] + final_value
                else:
                    current[final_key] = [existing, final_value]
        else:
            current[final_key] = final_value


# Convenience functions for backward compatibility
def parse_request_data(request) -> 'objict':
    """
    Parse Django request data into a single objict.

    Args:
        request: Django HttpRequest object

    Returns:
        objict containing all parsed request data
    """
    parser = RequestDataParser()
    return parser.parse(request)


def parse_request_data_debug(request) -> 'objict':
    """
    Parse request data with debug information printed.
    """
    print("=== REQUEST PARSING DEBUG ===")
    print(f"Method: {request.method}")
    print(f"Content-Type: {getattr(request, 'content_type', 'Not set')}")
    print(f"GET keys: {list(request.GET.keys())}")
    print(f"POST keys: {list(request.POST.keys())}")
    print(f"FILES keys: {list(request.FILES.keys())}")

    if hasattr(request, 'body'):
        body_preview = request.body[:200] if request.body else b''
        print(f"Body preview: {body_preview}")

    result = parse_request_data(request)
    print(f"Parsed result keys: {list(result.keys())}")
    print("=== END DEBUG ===")

    return result


# Example usage and test cases
def test_parser():
    """
    Test function to demonstrate parser capabilities.
    """
    from django.http import QueryDict
    from io import StringIO
    import sys

    # Capture output for testing
    old_stdout = sys.stdout
    sys.stdout = buffer = StringIO()

    try:
        # Mock request object for testing
        class MockRequest:
            def __init__(self):
                self.method = 'POST'
                self.content_type = 'application/json'
                self.GET = QueryDict('user.name=John&tags[]=python&tags[]=django&user.age=30')
                self.POST = QueryDict()
                self.FILES = QueryDict()
                self.body = b'{"api_key": "secret", "nested": {"value": 42}}'

        request = MockRequest()
        result = parse_request_data(request)

        print("Test Results:")
        print(f"User name: {result.get('user', {}).get('name')}")
        print(f"User age: {result.get('user', {}).get('age')}")
        print(f"Tags: {result.get('tags')}")
        print(f"API Key: {result.get('api_key')}")
        print(f"Nested value: {result.get('nested', {}).get('value')}")

    finally:
        sys.stdout = old_stdout
        output = buffer.getvalue()
        print(output)


if __name__ == "__main__":
    test_parser()
