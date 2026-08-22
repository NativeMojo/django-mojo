"""Moved from tests/test_deploy/config_sync.py (maestro #2558).

Asserting the "installing unverified" warning requires observing the module
logger, and `mock.patch.object(cs, "log")` is a process-global patch of a
production module attribute — unsafe under the parallel default tier. The
sync/install/restart contracts themselves stay in the default-tier module
through injectable seams; only this log-observation test lives here.
"""

import hashlib
import os
import shutil
import tempfile
from unittest import mock

from testit import helpers as th


def _tempdir():
    return tempfile.mkdtemp(prefix="testit_config_sync_ext.")


def _s3_publishing(payload, sha256=None, etag="abc"):
    """A mock S3 client that publishes `payload` and, unless told otherwise,
    advertises its real sha256 in object metadata."""
    if sha256 is None:
        sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    metadata = {} if sha256 is False else {"sha256": sha256}
    s3 = mock.Mock()
    s3.head_object.return_value = {"ETag": '"%s"' % etag, "Metadata": metadata}

    def _download(bucket, key, path):
        with open(path, "w") as handle:
            handle.write(payload)

    s3.download_file.side_effect = _download
    return s3


@th.django_unit_test()
def test_sync_warns_when_no_sha_is_published(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "django.conf")
        s3 = _s3_publishing("SECRET=unverified\n", sha256=False)
        config = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "p"}

        with mock.patch.object(cs, "log") as log:
            code = cs.sync(s3, config, target, "django.conf", False)

        th.assert_eq(code, 0,
                     "an object with no sha256 metadata still installs by "
                     "default — being strict would break every publisher using "
                     "a plain `aws s3 cp` on an unpinned upgrade")
        warnings = " ".join(str(c) for c in log.warning.call_args_list)
        th.assert_in("no sha256 metadata", warnings,
                     f"an unverifiable install must warn; logged: {warnings}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
