"""Permission-key expansion for domain-category permissions.

The bare domain term ("users", "groups", ...) is view_X and manage_X combined
into one simple term — a holder of "users" satisfies any check for
"view_users" or "manage_users". The implication is one-directional:
manage_users alone does NOT satisfy a check for "users".

Expansion applies only to the domain categories below; fine-grained perms
(manage_group, manage_members, manage_settings, ...) are never expanded.
"""

DOMAIN_CATEGORIES = {"users", "groups", "security", "comms", "jobs", "metrics", "files"}


def implied_perms(perm_key):
    """Return the perm keys whose holder satisfies a check for perm_key."""
    if isinstance(perm_key, str):
        for prefix in ("view_", "manage_"):
            if perm_key.startswith(prefix):
                base = perm_key[len(prefix):]
                if base in DOMAIN_CATEGORIES:
                    return (perm_key, base)
    return (perm_key,)
