# Import all REST handlers to register URL patterns
from . import phone
from . import sms
from . import config

__all__ = ['phone', 'sms', 'config']
