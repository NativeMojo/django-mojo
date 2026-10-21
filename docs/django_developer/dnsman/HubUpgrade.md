# Enable shared DNS validation

Shared validation uses the existing Admin login, deployment controls and Fleet
configuration. Sites and trusted projects share one DNS validation target;
each issuer keeps its own ACME account, certificates and private keys.

## Normal rollout

1. Publish and deploy a normal framework release carrying this backend and the
   matching Portal UI. It must be newer than **1.39.1**, which used the previous
   activation contract. Local source still numbered 1.39.1 is not a qualifying
   production release. This feature does not choose or publish the next version.
2. Use the existing deployment page to deploy every expected node and verify
   the deployment. The latest attempt must be converged, with no deployment
   in progress. Every frozen-roster node must have fresh existing proof of the
   exact deployment UUID, app SHA and framework version. Restart HTTP, Jobs
   and schedulers through the ordinary deployment lifecycle; do not leave
   unmanaged older writers running.
3. In the existing Portal shared-validation page, refresh hub status and select
   **Enable shared validation**. This is a deliberate, one-time superuser
   action using the current login and existing fresh-auth prompt.
4. Configure consumer credentials through the existing protected Fleet topic.
   Prepare the project's connection request, explicitly approve its apex and
   wildcard issuance scope in the trust controls, then connect and request its
   independent certificate. Sites and project setup use the existing APIs.

The configured hub's exact public Route53 zone and public DNS cut must be
available. Configuration, a copied connection descriptor, and ordinary local
DNS permissions do not grant trust.

## Small activation boundary

`services/acme_hub_upgrade.py` retains shared/exclusive PostgreSQL admission,
the migration-created installation identity and atomic original-owner binding.
An exclusive enable operation waits for admitted writers, rechecks the selected
deployment, binds existing unbound owner leases and enables sharing in one
transaction. A conflicting historical owner fails without partial binding.
Existing allocation names, lease values/timestamps, certificate material and
private keys remain in place. Missing installation state fails closed; reads do
not recreate it.

Published 1.38 owners and existing trusted connections keep renewing while
sharing is disabled. New setup, joins, grants and trust list/revoke maintenance
wait until Enable; existing key deactivation remains the emergency control.
Pinned connections still check their authenticated consumer identity, exact
participant, issuance scope and current revocation state before renewing.
Unused historical metadata columns remain for old ORM readers during the normal
rolling deployment. Schema changes use forward migrations; migration 0005 is
not rewritten by this cleanup.

The dedicated root service, enrollment, retained updater, process attestation,
private operator login and special capacity prohibition are removed. Deployment
uses the existing packaged scripts and normal capacity controls. The ordinary
deployment resolver refuses framework versions below the version recorded at
enablement. Generic stale deployment, runner and cohort checks remain.

## Rollback and operations

Before enablement, ordinary compatible application rollback remains available.
After enablement, retain a framework supporting shared participants and their
lease union. Rollback below the recorded framework version is refused by the
normal deployment policy. Manually installing an older package or launching an
unmanaged old writer bypasses that policy and is unsupported; this feature does
not create another privileged recovery platform to police root operators.

Normal renewals and the existing lease expiry/sweep remain automatic. Use the
existing deployment, Jobs, DNS and certificate status screens for failures.
This source change does not publish packages, deploy production, alter live DNS
or enable a live installation.

## Verification

Run the framework tier and the surviving serial union in
`tests/acme_hub_upgrade_tests.json`. Tests cover ordinary Admin authorization,
selected deployment and complete fresh node proofs, published-owner compatibility,
real PostgreSQL admission, atomic binding, shared lease union and existing
setup/deployment/capacity contracts. Portal browser verification belongs to its
existing UI build.
