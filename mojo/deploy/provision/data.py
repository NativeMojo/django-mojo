"""Aurora PostgreSQL and the Valkey replication group.

These are the two slow resources. Aurora takes five to fifteen minutes to reach
`available`, Valkey a similar span, and NOTHING HERE WAITS FOR THEM. A cluster
still reporting `creating` is a PENDING finding, the steps that need its endpoint
are skipped, and the next `apply` — five minutes later, or tomorrow — picks up
exactly where this one stopped. That is what makes Ctrl-C safe and what keeps
the Stubber tests from needing to model a polling loop.

Both are created encrypted, in private subnets, reachable only from the node
security group. Aurora also carries `DeletionProtection`, which is worth stating
plainly: this package cannot delete a cluster, and deletion protection means
neither can a mis-click in the console.
"""

from mojo.deploy.provision import report
from mojo.deploy.provision import spec as spec_module


DB_STEP = "db"
CACHE_STEP = "cache"

MASTER_USERNAME = "mojo"

# Statuses that mean "AWS is still working on it". Anything not in here and not
# `available` is a state a human should look at.
BUILDING_STATES = ("creating", "modifying", "backing-up", "configuring",
                   "starting", "upgrading", "rebooting", "resetting-master-credentials",
                   "create-replay", "snapshotting")
READY_STATES = ("available",)

# What this package will change on a cluster that already exists. An engine
# version is NOT in here on purpose — see `_engine_version_finding`.
MUTABLE_CLUSTER_FIELDS = ("DeletionProtection", "BackupRetentionPeriod")
# DatabaseName and StorageEncrypted are fixed at creation. So is the master
# username. A difference on any of them is MANUAL.
IMMUTABLE_CLUSTER_FIELDS = ("DatabaseName", "StorageEncrypted",
                            "MasterUsername", "Engine")


def ensure_database(clients, spec, observed, apply=False):
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    rds = clients.get("rds")

    subnet_ids = list(observed.get("private_subnet_ids") or [])
    sg_id = observed.get("rds_sg_id")
    secrets = observed.get("secrets") or {}

    _ensure_db_subnet_group(rds, spec, observed, names, subnet_ids,
                            findings, actions, apply)

    cluster = observed.get("db_cluster")
    if cluster:
        _report_cluster_state(spec, names, cluster, findings, result)
        _converge_cluster(rds, spec, names, cluster, findings, actions, apply)
    else:
        findings.append(report.missing(
            DB_STEP, "cluster.missing",
            f"Aurora cluster {names['db_cluster']} does not exist",
            f"apply creates it, encrypted, from "
            f"{spec_module.ENGINE_VERSIONS[spec_module.DB_ENGINE]}"))
        actions.append(report.Action(
            DB_STEP, "create", names["db_cluster"],
            f"{spec_module.DB_ENGINE} "
            f"{spec_module.ENGINE_VERSIONS[spec_module.DB_ENGINE]}"))
        if apply:
            if not secrets.get("db_password"):
                findings.append(report.missing(
                    DB_STEP, "cluster.no_password",
                    "the bootstrap secrets object has no db_password, so the "
                    "cluster cannot be created",
                    "let the secrets step run first"))
                return findings, actions, result
            if not subnet_ids or not sg_id:
                findings.append(report.missing(
                    DB_STEP, "cluster.no_network",
                    "the private subnets or the database security group are "
                    "not resolved yet",
                    "let the network and security group steps run first"))
                return findings, actions, result
            created = report.safe(
                findings, DB_STEP, "rds.create_db_cluster",
                lambda: rds.create_db_cluster(
                    DBClusterIdentifier=names["db_cluster"],
                    Engine=spec_module.DB_ENGINE,
                    EngineVersion=spec_module.ENGINE_VERSIONS[
                        spec_module.DB_ENGINE],
                    DatabaseName=names["db_name"],
                    MasterUsername=MASTER_USERNAME,
                    MasterUserPassword=secrets["db_password"],
                    DBSubnetGroupName=names["db_subnet_group"],
                    VpcSecurityGroupIds=[sg_id],
                    Port=spec_module.DB_PORT,
                    StorageEncrypted=True,
                    DeletionProtection=True,
                    BackupRetentionPeriod=spec.db_retention_days,
                    Tags=spec_module.tag_list(spec, "database",
                                              name=names["db_cluster"])))
            if not created:
                return findings, actions, result
            cluster = (created.get("DBCluster") or {})
            # Freshly created is `creating`, never `available`. Saying so here
            # rather than pretending otherwise is what tells the DAG to skip the
            # steps that need an endpoint.
            findings.append(report.pending(
                DB_STEP, "cluster.creating",
                f"{names['db_cluster']} was created and is still coming up"))
        else:
            return findings, actions, result

    endpoint = (cluster or {}).get("Endpoint")
    reader = (cluster or {}).get("ReaderEndpoint")
    status = (cluster or {}).get("Status")
    if status in READY_STATES:
        result.set("db_endpoint", endpoint)
        result.set("db_reader_endpoint", reader)
    result.set("db_cluster_id", names["db_cluster"])
    result.set("db_ready", status in READY_STATES)

    _ensure_cluster_members(rds, spec, observed, names, cluster,
                            findings, actions, apply)
    return findings, actions, result


def _ensure_db_subnet_group(rds, spec, observed, names, subnet_ids,
                            findings, actions, apply):
    if observed.get("db_subnet_group"):
        findings.append(report.existing(
            DB_STEP, "subnet_group.ok",
            f"DB subnet group {names['db_subnet_group']} is in place"))
        return
    findings.append(report.missing(
        DB_STEP, "subnet_group.missing",
        f"DB subnet group {names['db_subnet_group']} does not exist",
        f"apply creates it across {spec_module.AZ_COUNT} private subnets"))
    actions.append(report.Action(DB_STEP, "create", names["db_subnet_group"]))
    if not apply:
        return
    if len(subnet_ids) < spec_module.AZ_COUNT:
        findings.append(report.missing(
            DB_STEP, "subnet_group.no_subnets",
            f"only {len(subnet_ids)} private subnet(s) are resolved; Aurora "
            f"requires {spec_module.AZ_COUNT}",
            "let the network step run first"))
        return
    report.safe(
        findings, DB_STEP, "rds.create_db_subnet_group",
        lambda: rds.create_db_subnet_group(
            DBSubnetGroupName=names["db_subnet_group"],
            DBSubnetGroupDescription="django-mojo aurora cluster",
            SubnetIds=list(subnet_ids),
            Tags=spec_module.tag_list(spec, "database",
                                      name=names["db_subnet_group"])))


def _report_cluster_state(spec, names, cluster, findings, result):
    status = cluster.get("Status")
    if status in READY_STATES:
        findings.append(report.existing(
            DB_STEP, "cluster.ok",
            f"{names['db_cluster']} is available at {cluster.get('Endpoint')}"))
    elif status in BUILDING_STATES:
        findings.append(report.pending(
            DB_STEP, "cluster.pending",
            f"{names['db_cluster']} reports {status!r}"))
    else:
        findings.append(report.Finding(
            DB_STEP, report.MANUAL, "cluster.state",
            f"{names['db_cluster']} reports {status!r}, which is neither "
            f"available nor a normal in-progress state",
            "look at the cluster in the console before re-running"))

    if cluster.get("DatabaseName") and cluster.get("DatabaseName") != names["db_name"]:
        findings.append(report.manual(
            DB_STEP, "cluster.db_name",
            f"{names['db_cluster']} holds database "
            f"{cluster.get('DatabaseName')!r}, not {names['db_name']!r}; "
            f"an RDS DBName cannot be changed after creation",
            "point the application at the existing database name, or build a "
            "new environment under a different project/env slug"))
    if cluster.get("StorageEncrypted") is False:
        findings.append(report.manual(
            DB_STEP, "cluster.unencrypted",
            f"{names['db_cluster']} is not encrypted at rest; encryption "
            f"cannot be turned on for an existing cluster",
            "restore a snapshot into a new encrypted cluster when you can "
            "take the downtime"))
    _engine_version_finding(names, cluster, findings)


def _engine_version_finding(names, cluster, findings):
    """An engine upgrade is a decision, not a side effect of a converge.

    `modify_db_cluster` would do it, which is why this is not simply reported as
    drift and fixed: an in-place major upgrade takes the cluster offline and is
    not reversible. The finding names the version gap and the command; a human
    picks the window.
    """
    wanted = spec_module.ENGINE_VERSIONS[spec_module.DB_ENGINE]
    current = cluster.get("EngineVersion")
    if not current or current == wanted:
        return
    findings.append(report.manual(
        DB_STEP, "cluster.engine_version",
        f"{names['db_cluster']} runs {spec_module.DB_ENGINE} {current}; this "
        f"topology declares {wanted}",
        f"upgrade deliberately in a maintenance window: aws rds "
        f"modify-db-cluster --db-cluster-identifier {names['db_cluster']} "
        f"--engine-version {wanted} --apply-immediately"))


def _converge_cluster(rds, spec, names, cluster, findings, actions, apply):
    changes = {}
    if cluster.get("DeletionProtection") is not True:
        changes["DeletionProtection"] = True
    if cluster.get("BackupRetentionPeriod") != spec.db_retention_days:
        changes["BackupRetentionPeriod"] = spec.db_retention_days
    if not changes:
        return
    findings.append(report.drift(
        DB_STEP, "cluster.settings",
        f"{names['db_cluster']} differs on {', '.join(sorted(changes))}",
        "apply modifies it in place"))
    actions.append(report.Action(DB_STEP, "modify", names["db_cluster"],
                                 ", ".join(sorted(changes))))
    if apply:
        report.safe(
            findings, DB_STEP, "rds.modify_db_cluster",
            lambda: rds.modify_db_cluster(
                DBClusterIdentifier=names["db_cluster"],
                ApplyImmediately=True, **changes))


def _ensure_cluster_members(rds, spec, observed, names, cluster,
                            findings, actions, apply):
    """The writer and its readers.

    Note what is NOT here: nothing removes an instance. Shrinking a preset from
    two readers to one leaves both in place and says so — a reader you stop
    paying for is a deliberate act, not something a converge should do while
    you are looking at a progress bar.
    """
    present = {row.get("DBInstanceIdentifier")
               for row in (observed.get("db_instances") or [])}
    wanted = [names["db_writer"]] + list(names["db_readers"])
    for identifier in wanted:
        if identifier in present:
            findings.append(report.existing(
                DB_STEP, "instance.ok", f"{identifier} is in place"))
            continue
        findings.append(report.missing(
            DB_STEP, "instance.missing",
            f"cluster instance {identifier} does not exist",
            f"apply creates a {spec.db_type}"))
        actions.append(report.Action(DB_STEP, "create", identifier,
                                     spec.db_type))
        if not apply:
            continue
        if not cluster:
            continue
        report.safe(
            findings, DB_STEP, "rds.create_db_instance",
            lambda value=identifier: rds.create_db_instance(
                DBInstanceIdentifier=value,
                DBClusterIdentifier=names["db_cluster"],
                DBInstanceClass=spec.db_type,
                Engine=spec_module.DB_ENGINE,
                PubliclyAccessible=False,
                Tags=spec_module.tag_list(spec, "database", name=value)))

    extra = present - set(wanted)
    if extra:
        findings.append(report.manual(
            DB_STEP, "instance.extra",
            f"the cluster carries {len(extra)} instance(s) this preset does "
            f"not declare: {', '.join(sorted(extra))}",
            "remove them yourself if they are not wanted — this tool never "
            "deletes a database instance"))


# ── cache ───────────────────────────────────────────────────────────────────

def ensure_cache(clients, spec, observed, apply=False):
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    cache = clients.get("elasticache")

    subnet_ids = list(observed.get("private_subnet_ids") or [])
    sg_id = observed.get("cache_sg_id")
    secrets = observed.get("secrets") or {}
    node_count = 1 + spec.cache_replicas

    if observed.get("cache_subnet_group"):
        findings.append(report.existing(
            CACHE_STEP, "subnet_group.ok",
            f"cache subnet group {names['cache_subnet_group']} is in place"))
    else:
        findings.append(report.missing(
            CACHE_STEP, "subnet_group.missing",
            f"cache subnet group {names['cache_subnet_group']} does not exist",
            "apply creates it across the private subnets"))
        actions.append(report.Action(CACHE_STEP, "create",
                                     names["cache_subnet_group"]))
        if apply and subnet_ids:
            report.safe(
                findings, CACHE_STEP, "elasticache.create_cache_subnet_group",
                lambda: cache.create_cache_subnet_group(
                    CacheSubnetGroupName=names["cache_subnet_group"],
                    CacheSubnetGroupDescription="django-mojo valkey",
                    SubnetIds=list(subnet_ids),
                    Tags=spec_module.tag_list(
                        spec, "cache", name=names["cache_subnet_group"])))

    group = observed.get("cache_group")
    if group:
        status = group.get("Status")
        if status in READY_STATES:
            findings.append(report.existing(
                CACHE_STEP, "group.ok",
                f"{names['cache_group']} is available"))
        elif status in BUILDING_STATES:
            findings.append(report.pending(
                CACHE_STEP, "group.pending",
                f"{names['cache_group']} reports {status!r}"))
        else:
            findings.append(report.Finding(
                CACHE_STEP, report.MANUAL, "group.state",
                f"{names['cache_group']} reports {status!r}",
                "look at the replication group in the console before re-running"))

        if group.get("TransitEncryptionEnabled") is False:
            findings.append(report.manual(
                CACHE_STEP, "group.transit",
                f"{names['cache_group']} does not encrypt in transit; that is "
                f"fixed at creation for this engine",
                "rebuild the replication group when you can take the downtime"))
        if group.get("AtRestEncryptionEnabled") is False:
            findings.append(report.manual(
                CACHE_STEP, "group.at_rest",
                f"{names['cache_group']} is not encrypted at rest; that is "
                f"fixed at creation",
                "rebuild the replication group when you can take the downtime"))

        result.set("cache_group_id", names["cache_group"])
        result.set("cache_ready", status in READY_STATES)
        if status in READY_STATES:
            result.set("cache_endpoint", _cache_endpoint(group))
        return findings, actions, result

    findings.append(report.missing(
        CACHE_STEP, "group.missing",
        f"replication group {names['cache_group']} does not exist",
        f"apply creates {node_count} x {spec.cache_type} on "
        f"{spec_module.CACHE_ENGINE} "
        f"{spec_module.ENGINE_VERSIONS[spec_module.CACHE_ENGINE]}, encrypted "
        f"in transit and at rest"))
    actions.append(report.Action(
        CACHE_STEP, "create", names["cache_group"],
        f"{node_count} x {spec.cache_type}"))
    result.set("cache_group_id", names["cache_group"])
    result.set("cache_ready", False)
    if not apply:
        return findings, actions, result

    if not secrets.get("cache_auth_token") or not sg_id or not subnet_ids:
        findings.append(report.missing(
            CACHE_STEP, "group.prerequisites",
            "the auth token, the cache security group or the private subnets "
            "are not resolved yet",
            "let the secrets, network and security group steps run first"))
        return findings, actions, result

    created = report.safe(
        findings, CACHE_STEP, "elasticache.create_replication_group",
        lambda: cache.create_replication_group(
            ReplicationGroupId=names["cache_group"],
            ReplicationGroupDescription="django-mojo valkey",
            Engine=spec_module.CACHE_ENGINE,
            EngineVersion=spec_module.ENGINE_VERSIONS[
                spec_module.CACHE_ENGINE],
            CacheNodeType=spec.cache_type,
            NumCacheClusters=node_count,
            AutomaticFailoverEnabled=spec.cache_replicas > 0,
            MultiAZEnabled=spec.cache_replicas > 0,
            CacheSubnetGroupName=names["cache_subnet_group"],
            SecurityGroupIds=[sg_id],
            Port=spec_module.CACHE_PORT,
            TransitEncryptionEnabled=True,
            AtRestEncryptionEnabled=True,
            AuthToken=secrets["cache_auth_token"],
            Tags=spec_module.tag_list(spec, "cache",
                                      name=names["cache_group"])))
    if created:
        findings.append(report.pending(
            CACHE_STEP, "group.creating",
            f"{names['cache_group']} was created and is still coming up"))
    return findings, actions, result


def _cache_endpoint(group):
    """The primary endpoint, whichever shape this group reports it in.

    A cluster-mode replication group carries a ConfigurationEndpoint; a
    cluster-mode-disabled one carries a PrimaryEndpoint on its first node group.
    """
    configuration = group.get("ConfigurationEndpoint") or {}
    if configuration.get("Address"):
        return configuration["Address"]
    for node_group in group.get("NodeGroups") or []:
        primary = node_group.get("PrimaryEndpoint") or {}
        if primary.get("Address"):
            return primary["Address"]
    return None


def ensure_data(clients, spec, observed, apply=False):
    """Database and cache, for a caller converging this area alone."""
    findings, actions, result = ensure_database(clients, spec, observed, apply)
    merged = dict(observed or {})
    merged.update(result.as_dict())
    more_findings, more_actions, more_result = ensure_cache(
        clients, spec, merged, apply)
    findings.extend(more_findings)
    actions.extend(more_actions)
    result.update(**more_result.as_dict())
    return findings, actions, result
