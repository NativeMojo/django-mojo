"""Immutable, framework-owned MojoSec integrity profiles.

Profiles are code data.  A deployed policy selects a profile by name; it does
not carry a mutable copy of the target graph.  Changing any path or semantic
requires a new profile name.
"""

import hashlib
import json
import os


PRIVATE_TREES = (
    "/etc/mojosec",
    "/var/lib/mojosec",
    "/run/mojosec",
)
APPLICATION_TREES = (
    "/opt/api",
    "/opt/www",
)
# Content roots are a THIRD path category. They are neither an application
# release tree (still forbidden everywhere it is today) nor host state: they
# are tenant-published trees an unprivileged application legitimately rewrites,
# declared once by root enrollment and substituted into the content tier below.
CONTENT_TIER = "content"
MAX_CONTENT_ROOTS = 8
# Import-time validation only. The packaged content profile carries no roots of
# its own, so the graph check needs one representative root to resolve against.
_VALIDATION_CONTENT_ROOTS = ("/opt/content",)


def _target(path, recursive=False, optional=False, exclude=None,
            tenant_scoped=False):
    value = {"path": path, "recursive": recursive}
    if optional:
        value["optional"] = True
    if exclude:
        value["exclude"] = list(exclude)
    if tenant_scoped:
        value["tenant_scoped"] = True
    return value


AL2023_WEB_V1 = {
    "name": "al2023-web-v1",
    "version": 1,
    "tiers": {
        "fast": {
            "interval_seconds": 60,
            "max_entries": 100000,
            "max_file_bytes": 16 * 1024 * 1024,
            "max_depth": 64,
            "targets": [
                _target("/etc", True, exclude=("mojosec", "mojosec/**")),
                _target("/root/.ssh", True, True),
                _target("/root/.bashrc", optional=True),
                _target("/root/.bash_profile", optional=True),
                _target("/root/.profile", optional=True),
                _target("/root/.config/systemd/user", True, True),
                _target("/root/.local/bin", True, True),
                _target("/root/.aws/config", optional=True),
                _target("/root/.aws/credentials", optional=True),
                _target("/home/ec2-user/.ssh", True, True),
                _target("/home/ec2-user/.bashrc", optional=True),
                _target("/home/ec2-user/.bash_profile", optional=True),
                _target("/home/ec2-user/.profile", optional=True),
                _target("/home/ec2-user/.config/systemd/user", True, True),
                _target("/home/ec2-user/.local/bin", True, True),
                _target("/home/ec2-user/.aws/config", optional=True),
                _target("/home/ec2-user/.aws/credentials", optional=True),
                _target("/var/spool/cron", True, True),
                _target("/var/spool/at", True, True),
                _target("/var/lib/cloud/scripts", True, True),
                _target("/var/lib/cloud/instance/scripts", True, True),
                _target("/var/lib/cloud/instances", True, True),
                _target("/usr/local/bin", True, True),
                _target("/usr/local/sbin", True, True),
                _target("/usr/local/lib", True, True),
            ],
        },
        "slow": {
            "interval_seconds": 6 * 60 * 60,
            "max_entries": 500000,
            "max_file_bytes": 64 * 1024 * 1024,
            "max_depth": 64,
            "targets": [
                _target("/boot", True, True),
                _target("/usr/bin", True),
                _target("/usr/sbin", True),
            ],
        },
    },
    "rpm": {
        "interval_seconds": 6 * 60 * 60,
        "interpreter": "/usr/bin/python3",
        "max_entries": 250000,
        "max_packages": 10000,
        "max_owner_queries": 250000,
        "max_output_bytes": 8 * 1024 * 1024,
        "timeout_seconds": 300,
        "max_file_bytes": 64 * 1024 * 1024,
        "max_depth": 64,
    },
}


def _al2023_web_v2():
    """Return the AL2023 graph without the cloud-init symlink descendant."""
    profile = json.loads(json.dumps(AL2023_WEB_V1))
    profile["name"] = "al2023-web-v2"
    profile["version"] = 2
    profile["tiers"]["fast"]["targets"] = [
        target for target in profile["tiers"]["fast"]["targets"]
        if target["path"] != "/var/lib/cloud/instance/scripts"
    ]
    return profile


AL2023_WEB_V2 = _al2023_web_v2()


def _al2023_content_v1():
    """Return the AL2023 host graph plus one enrollment-substituted content tier.

    The content tier's targets are deliberately empty here and stay empty in
    every digest computation: WHICH tenant trees a node publishes is node
    identity, not profile identity, so two content nodes with different roots
    share one profile digest and are one fleet-wide policy.
    """
    profile = json.loads(json.dumps(AL2023_WEB_V2))
    profile["name"] = "al2023-content-v1"
    profile["version"] = 1
    profile["content_roots_required"] = True
    profile["tiers"][CONTENT_TIER] = {
        "interval_seconds": 300,
        # A publish is an unprivileged write the app annotates around itself,
        # so its evidence must survive a longer gap between the broker's
        # completion and this tier's next 5-minute walk than a root deploy
        # needs. Nothing else moves: every other tier keeps 300 (see
        # expected_changes.MAX_OPERATION_CORRELATION_SECONDS).
        "correlation_seconds": 900,
        "max_entries": 500000,
        "max_file_bytes": 16 * 1024 * 1024,
        "max_depth": 64,
        "targets": [],
    }
    return profile


AL2023_CONTENT_V1 = _al2023_content_v1()


PROFILES = {
    AL2023_WEB_V1["name"]: AL2023_WEB_V1,
    AL2023_WEB_V2["name"]: AL2023_WEB_V2,
    AL2023_CONTENT_V1["name"]: AL2023_CONTENT_V1,
}


class ProfileError(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def profile_digest(profile):
    """Digest one profile's immutable semantics, never its node-local roots.

    Emptying the content tier's targets here is a constant of the code, not a
    caller's choice: it is what makes the profile digest — and therefore the
    activation ceremony, rollback history, and fleet-wide digest-drift alarm —
    mean the same thing on every content node regardless of which tenant trees
    that node happens to serve. Tenant roots re-enter identity through the
    content tier's baseline key instead (see FimCollector.baseline_key).
    """
    material = {key: value for key, value in profile.items() if key != "digest"}
    tiers = material.get("tiers")
    if material.get("content_roots_required") and isinstance(tiers, dict) and \
            CONTENT_TIER in tiers:
        blind = json.loads(json.dumps(tiers))
        blind[CONTENT_TIER]["targets"] = []
        material = dict(material, tiers=blind)
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def profile_identity(profile):
    return {
        "name": profile["name"],
        "version": profile["version"],
        "digest": profile_digest(profile),
    }


def _contained(path, root):
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def validate_target_graph(targets, tier):
    """Normalize a final graph and reject hidden or duplicate coverage."""
    result = []
    seen = []
    for submitted in targets:
        if not isinstance(submitted, dict):
            raise ProfileError(f"profile tier {tier} target must be an object")
        path = submitted.get("path")
        if (not isinstance(path, str) or not os.path.isabs(path) or
                os.path.normpath(path) != path or path == "/"):
            raise ProfileError(f"profile tier {tier} target is not normalized: {path!r}")
        for forbidden in APPLICATION_TREES + PRIVATE_TREES:
            if _contained(path, forbidden) or _contained(forbidden, path):
                if path == "/etc" and forbidden == "/etc/mojosec":
                    excludes = set(submitted.get("exclude", ()))
                    if {"mojosec", "mojosec/**"}.issubset(excludes):
                        continue
                raise ProfileError(
                    f"profile tier {tier} target overlaps forbidden tree {forbidden}: {path}")
        for prior in seen:
            if _contained(path, prior) or _contained(prior, path):
                raise ProfileError(
                    f"profile tier {tier} has ancestor/descendant overlap: {prior}, {path}")
        seen.append(path)
        result.append(json.loads(json.dumps(submitted)))
    return result


def _content_targets(profile, content_roots):
    """Validate enrolled tenant roots and render them as content-tier targets."""
    roots = list(content_roots or ())
    if not profile.get("content_roots_required"):
        if roots:
            raise ProfileError(
                f"profile {profile['name']} does not accept content roots")
        return None
    if not 1 <= len(roots) <= MAX_CONTENT_ROOTS:
        # Fail closed: a content profile with no roots would monitor nothing
        # while still reporting a healthy content tier.
        raise ProfileError(
            f"profile {profile['name']} requires 1-{MAX_CONTENT_ROOTS} content roots")
    seen = set()
    for root in roots:
        if root in seen:
            raise ProfileError(f"duplicate content root: {root}")
        seen.add(root)
    return [_target(root, recursive=True, tenant_scoped=True) for root in roots]


def _reject_cross_tier_overlap(tiers):
    """No content root may contain, or sit inside, a host-tier target."""
    content = [target["path"] for target in tiers[CONTENT_TIER]["targets"]]
    for tier, config in tiers.items():
        if tier == CONTENT_TIER:
            continue
        for target in config["targets"]:
            for path in content:
                if _contained(path, target["path"]) or _contained(target["path"], path):
                    raise ProfileError(
                        "content root overlaps immutable host tier "
                        f"{tier} target {target['path']}: {path}")


def resolve_profile(name, content_roots=()):
    if not name:
        return None
    try:
        source = PROFILES[name]
    except KeyError as err:
        raise ProfileError(f"unknown MojoSec integrity profile: {name}") from err
    profile = json.loads(json.dumps(source))
    substituted = _content_targets(profile, content_roots)
    if substituted is not None:
        profile["tiers"][CONTENT_TIER]["targets"] = substituted
    for tier, config in profile["tiers"].items():
        config["targets"] = validate_target_graph(config["targets"], tier)
    if profile.get("content_roots_required"):
        _reject_cross_tier_overlap(profile["tiers"])
    profile["digest"] = profile_digest(profile)
    return profile


# Import-time validation makes an accidental edit fail in tests and packaging,
# before a mutable deployment can ever select the new graph.
for _profile_name, _profile in PROFILES.items():
    resolve_profile(
        _profile_name,
        _VALIDATION_CONTENT_ROOTS if _profile.get("content_roots_required") else (),
    )
