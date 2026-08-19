from django.apps import AppConfig


class DnsmanConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mojo.apps.dnsman'
    verbose_name = 'DNS Manager'

    def ready(self):
        from mojo.apps.account.services.admin_settings import Descriptor, register_descriptor
        register_descriptor(Descriptor(
            "DNSMAN_ACME_CONTACT_EMAIL", "ACME contact", "Domains & DNS",
            "Contact address used for certificate authority notices.", "email",
            resolver="dynamic", writable="owner", owner="Domains & DNS",
            change_behavior="owner_review", owner_route="domains",
            unset_meaning="the certificate authority has nobody to warn about "
                          "expiry or revocation"))
        register_descriptor(Descriptor(
            "DNSMAN_CERT_RENEW_DAYS", "Certificate renewal window", "Domains & DNS",
            "How early automatic certificate renewal begins.", "integer", 30,
            resolver="dynamic", writable="owner", owner="Domains & DNS",
            change_behavior="owner_review", constraints="Positive whole days",
            owner_route="domains", unit="days"))
