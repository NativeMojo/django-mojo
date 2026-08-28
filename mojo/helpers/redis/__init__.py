from .client import get_bounded_connection, get_connection
from .adapter import RedisAdapter, reset_adapter, get_adapter
from .channels import channel_name, isolation_prefix


def get_client(reader=False):
    return get_connection(reader=reader)
