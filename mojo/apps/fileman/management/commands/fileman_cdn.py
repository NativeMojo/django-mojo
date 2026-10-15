"""
Provision, check, disable and rotate CloudFront delivery for a file manager's
bucket (maestro #8250).

    manage.py fileman_cdn --manager <id> enable [--harden] [--json]
    manage.py fileman_cdn --manager <id> check [--json]
    manage.py fileman_cdn --manager <id> byo --distribution <id> [--alias https://cdn.example.com]
    manage.py fileman_cdn --manager <id> disable [--retire]
    manage.py fileman_cdn rotate-key
    manage.py fileman_cdn revoke-key --key-id <K...>

`enable` creates what is missing and changes nothing it did not create; a
second run creates nothing. `check` is read-only and prints the exact policy
JSON for every gap. Nothing here deletes an AWS resource. Credentials are the
deployment's (AWS_KEY/AWS_SECRET or the instance role); `--profile` runs under
an operator's boto3 profile instead.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from mojo.apps.fileman.models import FileManager
from mojo.apps.fileman.services import cdn_provision


class Command(BaseCommand):
    help = "Provision, check, disable or rotate CloudFront delivery for a file manager's bucket"

    def add_arguments(self, parser):
        parser.add_argument("operation", choices=[
            "enable", "check", "byo", "disable", "rotate-key", "revoke-key"])
        parser.add_argument("--manager", type=int,
                            help="FileManager id (required for enable, check, byo, disable; "
                                 "optional for rotate-key and revoke-key to pick the bucket)")
        parser.add_argument("--harden", action="store_true",
                            help="enable only: also turn on Block Public Access and Object Ownership")
        parser.add_argument("--retire", action="store_true",
                            help="disable only: also set the distribution Enabled=false")
        parser.add_argument("--distribution", help="byo only: the distribution id to register")
        parser.add_argument("--alias", default="", help="byo only: custom https:// domain the distribution serves")
        parser.add_argument("--key-id", help="revoke-key only: the CloudFront public key id")
        parser.add_argument("--profile", help="boto3 profile to run under instead of the deployment identity")
        parser.add_argument("--region", help="AWS region override")
        parser.add_argument("--json", action="store_true", help="print the report as JSON")

    def handle(self, *args, **options):
        operation = options["operation"]
        kwargs = {"profile": options.get("profile"), "region": options.get("region")}
        if operation in ("enable", "check", "byo", "disable"):
            if not options.get("manager"):
                raise CommandError("--manager is required", returncode=2)
            manager = FileManager.objects.filter(pk=options["manager"]).first()
            if manager is None:
                raise CommandError(f"no FileManager {options['manager']}", returncode=2)
            if not manager.is_s3:
                raise CommandError("CDN delivery applies to S3 file managers only", returncode=2)
        if operation == "enable":
            report = cdn_provision.enable(manager, harden=options["harden"], **kwargs)
        elif operation == "check":
            report = cdn_provision.check(manager, **kwargs)
        elif operation == "byo":
            if not options.get("distribution"):
                raise CommandError("--distribution is required for byo", returncode=2)
            row = cdn_provision.register_byo(manager, options["distribution"], options["alias"])
            report = cdn_provision.check(manager, **kwargs)
            report["registered"] = row.to_dict(graph="basic")
        elif operation == "disable":
            report = cdn_provision.disable(manager, retire=options["retire"], **kwargs)
        elif operation == "rotate-key":
            manager = FileManager.objects.filter(pk=options["manager"]).first() if options.get("manager") else None
            report = cdn_provision.rotate_key(manager=manager, **kwargs)
        else:
            if not options.get("key_id"):
                raise CommandError("--key-id is required for revoke-key", returncode=2)
            manager = FileManager.objects.filter(pk=options["manager"]).first() if options.get("manager") else None
            report = cdn_provision.revoke_key(options["key_id"], manager=manager, **kwargs)

        if options["json"]:
            self.stdout.write(json.dumps(report, sort_keys=True, default=str))
            return
        for item in report.get("items", []):
            line = f"{item['status']:7} {item['step']}/{item['code']}: {item['message']}"
            self.stdout.write(line)
            if item.get("remedy"):
                self.stdout.write(f"        fix: {item['remedy']}")
        for key in ("worst", "blocking", "distribution", "disabled", "retired", "revoked", "error"):
            if key in report:
                self.stdout.write(f"{key}: {json.dumps(report[key], default=str)}")
