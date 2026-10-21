"""Local operator ceremony; never a remote proof or root-access grant."""
import json
import os
import stat
import sys

from django.core.management.base import BaseCommand, CommandError


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def read_document(path, *, root_owned=False):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536
                or (root_owned and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600))):
            raise ValueError("evidence_unavailable")
        raw = os.read(fd, 65537)
        after = os.fstat(fd)
        if len(raw) != info.st_size or info.st_mtime_ns != after.st_mtime_ns:
            raise ValueError("evidence_unavailable")
        return json.loads(raw, object_pairs_hook=_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("constant")))
    except (OSError, ValueError, TypeError):
        raise CommandError("evidence_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


class Command(BaseCommand):
    help = "Read ACME installation state or perform the separately authorized local operator cutover."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["status", "plan", "activate"])
        parser.add_argument("--plan", help="Saved nonsecret operation plan")
        parser.add_argument("--artifact", help="Reviewed actual release artifact document")
        parser.add_argument("--roster", help="Authoritative sorted writer-node list document")
        parser.add_argument("--deployment")
        parser.add_argument("--sha")
        parser.add_argument("--resume-floor", help="Safe operator-observed persisted floor receipt; mandatory on partial retry")
        parser.add_argument("--attestation", help="Operator-owned0600 attestation, collected during this exclusive session")

    def handle(self, *args, **options):
        from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
        try:
            if options["action"] == "status":
                result = upgrade.status()
            elif options["action"] == "plan":
                if not all(options.get(key) for key in ("artifact", "roster", "deployment", "sha")):
                    raise CommandError("evidence_unavailable")
                result = upgrade.plan(
                    read_document(options["artifact"]), read_document(options["roster"]),
                    options["deployment"], options["sha"],
                    previous_floor=read_document(options["resume_floor"]) if options.get("resume_floor") else None)
            else:
                if os.geteuid() != 0 or not options.get("plan") or not options.get("attestation"):
                    raise CommandError("evidence_unavailable")
                operation = read_document(options["plan"], root_owned=True)

                def observe(current):
                    # Same immutable activation on retry, fresh nonce each
                    # coordinator session. Every node helper must use it.
                    import uuid
                    current["nonce"] = uuid.uuid4().hex
                    self.stdout.write(json.dumps(dict(
                        operation=current, classification="operator_attested",
                        instruction="Hold all quiescence and helper sessions; collect/reconfirm now, then enter the attestation digest.")))
                    self.stdout.flush()
                    confirmation = sys.stdin.readline(130).strip()
                    if not upgrade.feature.HEX64.fullmatch(confirmation):
                        raise CommandError("evidence_unavailable")
                    self._confirmation = confirmation
                    return read_document(options["attestation"], root_owned=True)

                result = upgrade.activate(operation, observe, lambda value: self._confirmation)
            self.stdout.write(json.dumps(result, sort_keys=True))
        except upgrade.me.MojoException as error:
            raise CommandError(str(error)) from None
