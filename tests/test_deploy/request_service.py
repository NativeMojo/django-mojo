import os
import tempfile

from testit import helpers as th


@th.django_unit_test()
def test_sealed_request_service_reader_is_exact_and_absence_compatible(opts):
    from mojo.deploy import request_service

    with tempfile.TemporaryDirectory(prefix="testit_request_service.") as root:
        path = os.path.join(root, "request-service.conf")
        th.assert_eq(
            request_service.read(path, required_uid=os.getuid()), True,
            "an absent authority must preserve managed and legacy request nodes")
        for text, expected in (
                ("MOJO_REQUEST_SERVICE=true\n", True),
                ("MOJO_REQUEST_SERVICE=false\n", False)):
            with open(path, "w", encoding="ascii") as handle:
                handle.write(text)
            os.chmod(path, 0o600)
            th.assert_eq(
                request_service.read(path, required_uid=os.getuid()), expected,
                f"the exact sealed {text.strip()!r} value must round-trip")


@th.django_unit_test()
def test_sealed_request_service_reader_rejects_shape_mode_and_links(opts):
    from mojo.deploy import request_service

    with tempfile.TemporaryDirectory(prefix="testit_request_service.") as root:
        path = os.path.join(root, "request-service.conf")
        for body in (
                b"MOJO_REQUEST_SERVICE=False\n",
                b"MOJO_REQUEST_SERVICE=false\nMOJO_REQUEST_SERVICE=true\n",
                b"MOJO_REQUEST_SERVICE=false\r\n",
                b"x" * (request_service.MAX_BYTES + 1)):
            with open(path, "wb") as handle:
                handle.write(body)
            os.chmod(path, 0o600)
            try:
                request_service.read(path, required_uid=os.getuid())
            except request_service.RequestServiceError:
                pass
            else:
                raise AssertionError(
                    f"malformed sealed authority {body[:40]!r} was accepted")

        with open(path, "w", encoding="ascii") as handle:
            handle.write("MOJO_REQUEST_SERVICE=true\n")
        os.chmod(path, 0o640)
        try:
            request_service.read(path, required_uid=os.getuid())
        except request_service.RequestServiceError as err:
            th.assert_in("0600", str(err),
                         f"unsafe mode must be named precisely: {err}")
        else:
            raise AssertionError("a group-readable authority was accepted")

        os.chmod(path, 0o600)
        hardlink = os.path.join(root, "second-link")
        os.link(path, hardlink)
        try:
            request_service.read(path, required_uid=os.getuid())
        except request_service.RequestServiceError as err:
            th.assert_in("one link", str(err),
                         f"hard-link ambiguity must be named: {err}")
        else:
            raise AssertionError("a multiply-linked authority was accepted")

        os.unlink(hardlink)
        target = os.path.join(root, "target")
        os.replace(path, target)
        os.symlink(target, path)
        try:
            request_service.read(path, required_uid=os.getuid())
        except request_service.RequestServiceError:
            pass
        else:
            raise AssertionError("O_NOFOLLOW did not reject a symlink authority")


@th.django_unit_test()
def test_sealed_request_service_reader_binds_a_safe_real_parent(opts):
    from mojo.deploy import request_service

    with tempfile.TemporaryDirectory(prefix="testit_request_service.") as root:
        unsafe = os.path.join(root, "unsafe")
        os.mkdir(unsafe, 0o755)
        path = os.path.join(unsafe, "request-service.conf")
        with open(path, "w", encoding="ascii") as handle:
            handle.write("MOJO_REQUEST_SERVICE=false\n")
        os.chmod(path, 0o600)
        os.chmod(unsafe, 0o775)
        try:
            request_service.read(path, required_uid=os.getuid())
        except request_service.RequestServiceError as err:
            th.assert_in("parent", str(err),
                         f"a writable parent refusal must name its cause: {err}")
        else:
            raise AssertionError(
                "a group-writable authority parent granted request authority")

        real = os.path.join(root, "real")
        alias = os.path.join(root, "alias")
        os.mkdir(real, 0o755)
        real_path = os.path.join(real, "request-service.conf")
        with open(real_path, "w", encoding="ascii") as handle:
            handle.write("MOJO_REQUEST_SERVICE=false\n")
        os.chmod(real_path, 0o600)
        os.symlink(real, alias)
        try:
            request_service.read(
                os.path.join(alias, "request-service.conf"),
                required_uid=os.getuid())
        except request_service.RequestServiceError as err:
            th.assert_in("directory", str(err),
                         f"a symlink-parent refusal must name its cause: {err}")
        else:
            raise AssertionError(
                "a symlink authority parent granted request authority")
