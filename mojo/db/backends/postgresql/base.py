"""Django PostgreSQL backend with an exact pool-acquisition observation seam."""

import os
import time

from django.db.backends.postgresql.base import DatabaseWrapper as DjangoDatabaseWrapper

from mojo.db import config
from mojo.db.errors import emit_pool_error, is_pool_acquisition_error
from mojo.db.pool_identity import application_name


class DatabaseWrapper(DjangoDatabaseWrapper):
    """Delegate pooling to Django; observe only its supported acquisition call."""

    def get_connection_params(self):
        params = super().get_connection_params()
        plan = config.LAST_POOL_PLAN or {}
        if plan.get("enabled") and plan.get("valid"):
            params["application_name"] = application_name(
                dict(plan.get("identity") or {}),
                plan.get("role") or "api",
                self.alias,
                pid=os.getpid(),
            )
        return params

    def get_new_connection(self, conn_params):
        started = time.monotonic()
        try:
            connection = super().get_new_connection(conn_params)
        except Exception as error:
            from mojo.db.pool_telemetry import record_acquisition
            elapsed = max(0.0, time.monotonic() - started)
            record_acquisition(elapsed, error=error)
            if is_pool_acquisition_error(error):
                emit_pool_error(error)
            raise
        from mojo.db.pool_telemetry import record_acquisition
        record_acquisition(max(0.0, time.monotonic() - started))
        from mojo.db.pool_telemetry import lease_trace_enabled, record_lease_acquired
        if self.pool and lease_trace_enabled():
            record_lease_acquired(connection, self.alias)
        return connection

    def _close(self):
        connection = self.connection
        if connection is None or not self.pool:
            return super()._close()
        from mojo.db.pool_telemetry import (
            lease_trace_enabled,
            record_lease_returned,
            record_lease_return_failed,
            record_lease_returning,
        )
        if not lease_trace_enabled():
            return super()._close()
        return_token = record_lease_returning(connection)
        try:
            result = super()._close()
        except Exception as error:
            if return_token is not None:
                record_lease_return_failed(
                    connection, error, lease_id=return_token)
            raise
        if return_token is not None:
            record_lease_returned(connection, lease_id=return_token)
        return result
