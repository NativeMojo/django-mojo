"""
Format handlers for advanced serialization.
Supports JSON, CSV, Excel, and other output formats.
"""

from .json import JsonFormatter
from .csv import CsvFormatter  
from .excel import ExcelFormatter
from .response import ResponseFormatter

__all__ = [
    'JsonFormatter',
    'CsvFormatter', 
    'ExcelFormatter',
    'ResponseFormatter'
]

# Default formatters
DEFAULT_FORMATTERS = {
    'json': JsonFormatter,
    'csv': CsvFormatter,
    'excel': ExcelFormatter,
    'xlsx': ExcelFormatter,
}

def get_formatter(format_type):
    """Get formatter class for the specified format type."""
    return DEFAULT_FORMATTERS.get(format_type.lower())