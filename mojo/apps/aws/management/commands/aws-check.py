import json
import sys

from django.core.management.base import BaseCommand, CommandError

from mojo.apps.aws.services.aws_check import ALL_SECTIONS, AWSCheckRunner


class Command(BaseCommand):
    help = "Audit and safely bootstrap django-mojo AWS deployment wiring"

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Strictly read-only, non-interactive audit")
        parser.add_argument("--apply", action="store_true", help="Allow confirmation-gated create-missing actions")
        parser.add_argument("--yes", action="store_true", help="Approve create-missing actions (requires --apply)")
        parser.add_argument("--json", action="store_true", help="Emit versioned JSON; implies --check")
        parser.add_argument("--section", action="append", choices=ALL_SECTIONS, help="Audit only this section; repeatable")
        parser.add_argument("--timeout", type=int, default=3, help="AWS connect/read timeout in seconds (1-30)")
        parser.add_argument("--region", help="AWS region override")
        parser.add_argument("--aws-profile", help="Non-secret boto3 profile override")
        parser.add_argument("--probe-s3", action="store_true", help="Confirm a write/read/delete S3 sentinel probe")
        parser.add_argument("--bucket-name", help="Globally unique bucket name for create-missing apply")
        parser.add_argument("--mailbox-email", help="Mailbox address for create-missing apply")
        parser.add_argument("--adopt-bucket", action="store_true", help="Explicitly adopt an exact owned bucket after interrupted tagging")

    def _confirm(self, message):
        self.stdout.write(f"\n{message}")
        return input("Proceed? [y/N] ").strip().lower() in ("y", "yes")

    def handle(self, *args, **options):
        if options["check"] and options["apply"]:
            raise CommandError("--check and --apply are mutually exclusive", returncode=2)
        if options["yes"] and not options["apply"]:
            raise CommandError("--yes requires --apply", returncode=2)
        if options["json"] and (options["apply"] or options["yes"] or options["probe_s3"]):
            raise CommandError("--json is read-only and cannot be combined with apply/probe flags", returncode=2)
        if not 1 <= options["timeout"] <= 30:
            raise CommandError("--timeout must be between 1 and 30 seconds", returncode=2)

        interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
        apply = options["apply"] or (interactive and not options["check"] and not options["json"])
        runner = AWSCheckRunner(
            region=options["region"], profile=options["aws_profile"], timeout=options["timeout"],
            apply=apply, yes=options["yes"], probe_s3=options["probe_s3"],
            bucket_name=options["bucket_name"], mailbox_email=options["mailbox_email"],
            adopt_bucket=options["adopt_bucket"], confirm=self._confirm if interactive else None,
        )
        report = runner.run(options["section"])
        if options["json"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
        else:
            current = None
            for item in report["items"]:
                if item["section"] != current:
                    current = item["section"]
                    self.stdout.write(self.style.MIGRATE_HEADING(f"\n[{current}]"))
                self.stdout.write(f"{item['status'].upper():7} {item['code']}: {item['message']}")
                if item["remediation"]:
                    self.stdout.write(f"        {item['remediation']}")
            counts = report["counts"]
            self.stdout.write(
                f"\nSummary: PASS={counts['pass']} WARN={counts['warn']} FAIL={counts['fail']} "
                f"PENDING={counts['pending']} SKIP={counts['skip']}"
            )
            rerun = "python manage.py aws-check --check"
            for section in options["section"] or []:
                rerun += f" --section {section}"
            self.stdout.write(f"Rerun: {rerun}")
        if report["overall"] == "fail":
            raise CommandError("AWS readiness checks failed", returncode=1)
