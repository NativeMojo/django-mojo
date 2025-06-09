import json
import datetime
import math
from decimal import Decimal

# Try to import ujson for better performance
try:
    import ujson
    HAS_UJSON = True
except ImportError:
    ujson = None
    HAS_UJSON = False

from mojo.helpers import logit

logger = logit.get_logger("json_formatter", "json_formatter.log")


class JsonFormatter:
    """
    Enhanced JSON formatter with performance optimizations and Django model support.
    """
    
    def __init__(self, use_ujson=None, pretty=False, ensure_ascii=False):
        """
        Initialize JSON formatter.
        
        :param use_ujson: Force use of ujson (True/False) or auto-detect (None)
        :param pretty: Enable pretty printing with indentation
        :param ensure_ascii: Ensure ASCII output (escapes unicode)
        """
        self.use_ujson = use_ujson if use_ujson is not None else HAS_UJSON
        self.pretty = pretty
        self.ensure_ascii = ensure_ascii
        
    def serialize(self, data, **kwargs):
        """
        Serialize data to JSON string.
        
        :param data: Data to serialize
        :param kwargs: Additional options for JSON serialization
        :return: JSON string
        """
        # Preprocess data to handle Django types
        processed_data = self._preprocess_data(data)
        
        try:
            if self.use_ujson and HAS_UJSON and not self.pretty:
                # ujson is faster but doesn't support pretty printing or custom encoders
                return ujson.dumps(processed_data, ensure_ascii=self.ensure_ascii, **kwargs)
            else:
                # Use standard json with custom encoder
                json_kwargs = {
                    'cls': ExtendedJSONEncoder,
                    'ensure_ascii': self.ensure_ascii,
                    **kwargs
                }
                
                if self.pretty:
                    json_kwargs.update({
                        'indent': 4,
                        'separators': (',', ': '),
                        'sort_keys': True
                    })
                
                return json.dumps(processed_data, **json_kwargs)
                
        except Exception as e:
            logger.error(f"JSON serialization failed: {e}")
            logger.error(f"Data type: {type(data)}")
            
            # Fallback to string representation
            try:
                return json.dumps({
                    'error': 'Serialization failed',
                    'message': str(e),
                    'data_type': str(type(data))
                })
            except Exception:
                return '{"error": "Critical serialization failure"}'
    
    def _preprocess_data(self, data):
        """
        Recursively preprocess data to handle Django-specific types.
        This is needed for ujson which doesn't support custom encoders.
        """
        if isinstance(data, dict):
            return {key: self._preprocess_data(value) for key, value in data.items()}
        elif isinstance(data, (list, tuple)):
            return [self._preprocess_data(item) for item in data]
        elif isinstance(data, datetime.datetime):
            return int(data.timestamp())
        elif isinstance(data, datetime.date):
            return data.isoformat()
        elif isinstance(data, datetime.time):
            return data.isoformat()
        elif isinstance(data, Decimal):
            return 0.0 if data.is_nan() else float(data)
        elif isinstance(data, float):
            return 0.0 if math.isnan(data) else data
        elif isinstance(data, set):
            return list(data)
        elif hasattr(data, 'pk') and hasattr(data, '_meta'):
            # Django model instance - return primary key
            return data.pk
        elif hasattr(data, '__dict__') and not isinstance(data, (str, bytes)):
            # Generic object - try to serialize its dict
            try:
                return self._preprocess_data(data.__dict__)
            except Exception:
                return str(data)
        else:
            return data
    
    def pretty_serialize(self, data, **kwargs):
        """
        Serialize data with pretty formatting.
        """
        old_pretty = self.pretty
        self.pretty = True
        try:
            result = self.serialize(data, **kwargs)
        finally:
            self.pretty = old_pretty
        return result
    
    def compact_serialize(self, data, **kwargs):
        """
        Serialize data in compact format (no extra whitespace).
        """
        old_pretty = self.pretty
        self.pretty = False
        try:
            result = self.serialize(data, **kwargs)
        finally:
            self.pretty = old_pretty
        return result


class ExtendedJSONEncoder(json.JSONEncoder):
    """
    Extended JSON encoder that handles Django model types and other Python objects.
    """
    
    def default(self, o):
        """
        Convert objects that aren't natively JSON serializable.
        """
        # Handle datetime objects
        if isinstance(o, datetime.datetime):
            return int(o.timestamp())
        elif isinstance(o, datetime.date):
            return o.isoformat()
        elif isinstance(o, datetime.time):
            return o.isoformat()
        
        # Handle numeric types
        elif isinstance(o, Decimal):
            return 0.0 if o.is_nan() else float(o)
        elif isinstance(o, float) and math.isnan(o):
            return 0.0
        elif isinstance(o, complex):
            return {'real': o.real, 'imag': o.imag}
        
        # Handle collections
        elif isinstance(o, set):
            return list(o)
        elif isinstance(o, frozenset):
            return list(o)
        
        # Handle bytes
        elif isinstance(o, (bytes, bytearray)):
            try:
                return o.decode('utf-8')
            except UnicodeDecodeError:
                return o.decode('utf-8', errors='replace')
        
        # Handle Django model instances
        elif hasattr(o, 'pk') and hasattr(o, '_meta'):
            return o.pk
        
        # Handle Django QuerySet
        elif hasattr(o, 'model') and hasattr(o, 'query'):
            return list(o.values())
        
        # Handle callable objects
        elif callable(o):
            try:
                return o()
            except Exception:
                return f"<callable: {o.__name__ if hasattr(o, '__name__') else str(o)}>"
        
        # Handle objects with __dict__
        elif hasattr(o, '__dict__'):
            try:
                return o.__dict__
            except Exception:
                pass
        
        # Try to convert to string as last resort
        try:
            return str(o)
        except Exception:
            return f"<unserializable: {type(o).__name__}>"


# Convenience functions
def to_json(data, pretty=False, use_ujson=None, **kwargs):
    """
    Convert data to JSON string.
    
    :param data: Data to serialize
    :param pretty: Enable pretty printing
    :param use_ujson: Force ujson usage
    :param kwargs: Additional JSON options
    :return: JSON string
    """
    formatter = JsonFormatter(use_ujson=use_ujson, pretty=pretty)
    return formatter.serialize(data, **kwargs)


def to_pretty_json(data, **kwargs):
    """
    Convert data to pretty-formatted JSON string.
    """
    return to_json(data, pretty=True, **kwargs)


def to_compact_json(data, **kwargs):
    """
    Convert data to compact JSON string.
    """
    return to_json(data, pretty=False, **kwargs)


# Legacy compatibility
serialize = to_json
pretty_json = to_pretty_json
prettyJSON = to_pretty_json  # Maintain old naming convention