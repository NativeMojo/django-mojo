from .client import get_connection
from .adapter import RedisAdapter, reset_adapter, get_adapter


def get_client():
    return get_connection()
