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


def _target(path, recursive=False, optional=False, exclude=None):
    value = {"path": path, "recursive": recursive}
    if optional:
        value["optional"] = True
    if exclude:
        value["exclude"] = list(exclude)
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


PROFILES = {AL2023_WEB_V1["name"]: AL2023_WEB_V1}


class ProfileError(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def profile_digest(profile):
    material = {key: value for key, value in profile.items() if key != "digest"}
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


def resolve_profile(name):
    if not name:
        return None
    try:
        source = PROFILES[name]
    except KeyError as err:
        raise ProfileError(f"unknown MojoSec integrity profile: {name}") from err
    profile = json.loads(json.dumps(source))
    for tier, config in profile["tiers"].items():
        config["targets"] = validate_target_graph(config["targets"], tier)
    profile["digest"] = profile_digest(profile)
    return profile


# Import-time validation makes an accidental edit fail in tests and packaging,
# before a mutable deployment can ever select the new graph.
resolve_profile("al2023-web-v1")
