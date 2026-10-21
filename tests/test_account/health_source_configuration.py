"""The snapshot roster must execute against the real PostgreSQL edge schema."""
from testit import helpers as th


@th.django_unit_test()
def test_real_vhost_roster_uses_canonical_names_with_one_bounded_query(opts):
    import time
    import uuid
    from contextlib import contextmanager
    from django.db import connection
    from mojo.apps.account.models import Group
    from mojo.apps.account.services import health_evidence as he, system_settings
    from mojo.apps.dnsman.models import Certificate, Domain
    from mojo.apps.edge.models import Vhost, WebApp

    assert connection.vendor == "postgresql", "the roster regression must exercise PostgreSQL's real schema"
    token = uuid.uuid4().hex
    group = Group.objects.create(name="health-roster-" + token)
    try:
        domain = Domain.objects.create(name="health-" + token + ".example", group=group)
        certificate = Certificate.objects.create(domain=domain, common_name=domain.name)
        # Persist fixtures without requesting edge convergence or deployment work.
        vhosts = Vhost.objects.bulk_create([
            Vhost(domain=domain, certificate=certificate, label=label)
            for label in ("", "api", "*")
        ])
        # Negative test-owned IDs keep these rows inside the bounded first page,
        # even when other packages create their own WebApps concurrently.
        first_id = -int(token[:13], 16)
        apps = [WebApp(id=first_id + index, group=group, slug="health-" + str(index),
                       vhost=vhost, bucket="testit-health", prefix="testit-health")
                for index, vhost in enumerate(vhosts + [None])]
        WebApp.objects.bulk_create(list(reversed(apps)))

        @contextmanager
        def configured_cursor(deadline):
            # Execute the production SQL on its real private primary connection;
            # supply only frozen identity fixture values, never global Settings.
            with he.primary_cursor(deadline, budget=budget) as cursor:
                class Cursor:
                    execute = cursor.execute

                    def fetchone(self):
                        stored, roster = cursor.fetchone()
                        return {**(stored or {}), system_settings.INSTALLATION_UUID: token,
                                system_settings.INSTALLATION_SLUG: "testit-health"}, roster
                yield Cursor()

        def get_static(key, default=None):
            return {"HEALTH_SNAPSHOT_ENVIRONMENT_ID": "testit", "JOBS_CHANNELS": ["testit_health"]}.get(key, default)

        deadline = time.monotonic() + 3
        budget = he.CycleBudget(deadline)
        configuration = he.source_configuration(deadline, get_static=get_static, cursor_context=configured_cursor)
        services = {row["service_id"]: row for row in configuration["services"]}
        for app, vhost in zip(apps, vhosts + [None]):
            expected = "https://" + vhost.server_name if vhost else None
            assert services["webapp-%s" % app.pk]["origin"] == expected, "real apex, subdomain, wildcard or unbound WebApp origin differs from Vhost.server_name"
        actual_order = [row["service_id"] for row in configuration["services"][1:5]]
        assert actual_order == ["webapp-%s" % app.pk for app in apps], "source roster lost its bounded primary-key ordering"
        assert budget.counts == {"db_queries": 3}, "source configuration added a query beyond BEGIN, one bounded SELECT and COMMIT"
    finally:
        Vhost.objects.filter(domain__group=group).delete()
        Group.objects.filter(pk=group.pk).delete()
