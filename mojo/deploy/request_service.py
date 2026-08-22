"""Read the root-sealed framework request-service authority."""

import os
import stat
import sys


AUTHORITY_PATH = "/etc/mojo/request-service.conf"
MAX_BYTES = 128


class RequestServiceError(RuntimeError):
    pass


def read(path=AUTHORITY_PATH, required_uid=0):
    """Return the sealed boolean; absence is legacy request-serving true."""
    absolute = os.path.abspath(path)
    parent_path = os.path.dirname(absolute)
    name = os.path.basename(absolute)
    parent_flags = os.O_RDONLY
    parent_flags |= getattr(os, "O_CLOEXEC", 0)
    parent_flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        parent = os.open(parent_path, parent_flags)
    except FileNotFoundError:
        return True
    except OSError as err:
        raise RequestServiceError(
            f"cannot open request-service authority directory: {err}")

    try:
        parent_details = os.fstat(parent)
        parent_mode = stat.S_IMODE(parent_details.st_mode)
        if not stat.S_ISDIR(parent_details.st_mode):
            raise RequestServiceError(
                "request-service authority parent is not a directory")
        if (parent_details.st_uid != required_uid
                or parent_mode & 0o022):
            raise RequestServiceError(
                "request-service authority parent must be root-owned and "
                "not group/world writable")

        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            return True
        except OSError as err:
            raise RequestServiceError(
                f"cannot open sealed request-service authority: {err}")

        try:
            details = os.fstat(descriptor)
            mode = stat.S_IMODE(details.st_mode)
            if not stat.S_ISREG(details.st_mode):
                raise RequestServiceError(
                    "request-service authority is not a regular file")
            if details.st_uid != required_uid or mode != 0o600:
                raise RequestServiceError(
                    "request-service authority must be root-owned mode 0600")
            if details.st_nlink != 1:
                raise RequestServiceError(
                    "request-service authority must have exactly one link")
            body = os.read(descriptor, MAX_BYTES + 1)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)

    if len(body) > MAX_BYTES:
        raise RequestServiceError("request-service authority is oversized")
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        raise RequestServiceError("request-service authority is not ASCII")
    if text == "MOJO_REQUEST_SERVICE=true\n":
        return True
    if text == "MOJO_REQUEST_SERVICE=false\n":
        return False
    raise RequestServiceError(
        "request-service authority must contain one exact key=value boolean")


def main():
    try:
        selected = read()
    except RequestServiceError as err:
        print(f"request-service: {err}", file=sys.stderr)
        return 2
    print("true" if selected else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
