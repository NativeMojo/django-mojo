"""
Learned skills service for the admin assistant.

Skills are multi-step procedures stored in the database, scoped by tier
(global / user / group).  The assistant discovers them via the find_skill
tool and replays the steps through normal tool-calling.

Permission model mirrors memory.py:
  - Global: assistant permission required
  - User:   users read/write own tier, superusers can access any
  - Group:  group members can read, members with assistant perm can write
"""
from django.db.models import Q
from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger("assistant", "assistant.log")

VALID_TIERS = ("global", "user", "group")
MAX_FIND_RESULTS = 5


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def _get_limits():
    """Return (user_max, group_max, global_max, max_steps)."""
    return (
        settings.get("LLM_ADMIN_SKILLS_MAX_PER_USER", 20, kind="int"),
        settings.get("LLM_ADMIN_SKILLS_MAX_PER_GROUP", 30, kind="int"),
        settings.get("LLM_ADMIN_SKILLS_MAX_GLOBAL", 20, kind="int"),
        settings.get("LLM_ADMIN_SKILLS_MAX_STEPS", 10, kind="int"),
    )


def _max_for_tier(tier):
    user_max, group_max, global_max, _ = _get_limits()
    return {"global": global_max, "user": user_max, "group": group_max}.get(tier, 20)


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------

def _can_read_tier(tier, user, group=None):
    """Check if user can read skills in a tier."""
    if user.is_superuser:
        return True
    if tier == "global":
        return user.has_permission("assistant")
    if tier == "user":
        return user.has_permission("assistant")
    if tier == "group":
        if not group:
            return False
        member = group.get_member_for_user(user, check_parents=True)
        return member is not None
    return False


def _can_write_tier(tier, user, group=None):
    """Check if user can write skills in a tier."""
    if user.is_superuser:
        return True
    if tier == "global":
        return user.has_permission("assistant")
    if tier == "user":
        return user.has_permission("assistant")
    if tier == "group":
        if not group:
            return False
        member = group.get_member_for_user(user, check_parents=True)
        if not member:
            return False
        return member.has_permission("assistant")
    return False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_steps(steps, max_steps):
    """
    Validate step list structure.

    Returns error string or None.
    """
    if not isinstance(steps, list):
        return "steps must be a list"
    if not steps:
        return "steps must contain at least one step"
    if len(steps) > max_steps:
        return f"Too many steps (max {max_steps})"
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            return f"Step {i + 1} must be a dict"
        if not step.get("tool"):
            return f"Step {i + 1} missing required field 'tool'"
        if not step.get("description"):
            return f"Step {i + 1} missing required field 'description'"
    return None


def _validate_triggers(triggers):
    """Validate triggers list. Returns error string or None."""
    if not isinstance(triggers, list):
        return "triggers must be a list of strings"
    for t in triggers:
        if not isinstance(t, str) or not t.strip():
            return "Each trigger must be a non-empty string"
    if len(triggers) > 10:
        return "Too many trigger phrases (max 10)"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_skills(user, query, group=None):
    """
    Search for skills matching a query string.

    Searches name, description, and triggers (JSONField text) using
    icontains.  Results are scoped to tiers the user can read.

    Returns list of skill dicts with full step definitions.
    """
    if not settings.get("LLM_ADMIN_SKILLS_ENABLED", True, kind="bool"):
        return []

    from mojo.apps.assistant.models import Skill

    if not query or not query.strip():
        return []

    query = query.strip()

    # Build tier-scoped base queryset
    qs = _scoped_queryset(user, group).filter(is_active=True)

    # Search across name, description, and triggers JSON text
    words = query.lower().split()
    combined = Q()
    for word in words[:5]:  # Cap at 5 keywords to avoid overly broad queries
        combined |= (
            Q(name__icontains=word)
            | Q(description__icontains=word)
            | Q(triggers__icontains=word)
        )
    qs = qs.filter(combined)

    results = []
    for skill in qs[:MAX_FIND_RESULTS]:
        results.append(_skill_to_detail(skill))
    return results


def save_skill(user, tier, name, description, triggers, steps,
               group=None, auto_execute=False):
    """
    Create or update a skill.

    Returns dict with skill data or error.
    """
    if not settings.get("LLM_ADMIN_SKILLS_ENABLED", True, kind="bool"):
        return {"error": "Skills are disabled"}

    from mojo.apps.assistant.models import Skill

    # Validate tier
    if tier not in VALID_TIERS:
        return {"error": f"Invalid tier: {tier}. Must be one of: {', '.join(VALID_TIERS)}"}

    # Permission check
    if not _can_write_tier(tier, user, group):
        return {"error": f"You don't have permission to write {tier} skills"}

    # Validate name
    if not name or not name.strip():
        return {"error": "Skill name is required"}
    name = name.strip()
    if len(name) > 128:
        return {"error": "Skill name too long (max 128 characters)"}

    # Validate description
    if not description or not description.strip():
        return {"error": "Skill description is required"}

    # Validate triggers
    if triggers:
        err = _validate_triggers(triggers)
        if err:
            return {"error": err}
    else:
        triggers = []

    # Validate steps
    _, _, _, max_steps = _get_limits()
    err = _validate_steps(steps, max_steps)
    if err:
        return {"error": err}

    # Enforce tier limits
    scope_filter = _scope_filter(tier, user, group)
    count = Skill.objects.filter(**scope_filter, is_active=True).count()
    max_count = _max_for_tier(tier)

    # Check for existing skill with same name (upsert)
    existing = Skill.objects.filter(**scope_filter, name=name).first()

    if not existing and count >= max_count:
        return {"error": f"Skill limit reached for {tier} tier ({max_count})"}

    if existing:
        existing.description = description.strip()
        existing.triggers = triggers
        existing.steps = steps
        existing.auto_execute = auto_execute
        existing.is_active = True
        existing.save(update_fields=[
            "description", "triggers", "steps", "auto_execute", "is_active", "modified",
        ])
        return {"message": f"Skill '{name}' updated", "skill": _skill_to_summary(existing)}
    else:
        skill = Skill.objects.create(
            user=user if tier == "user" else None,
            group=group if tier == "group" else None,
            tier=tier,
            name=name,
            description=description.strip(),
            triggers=triggers,
            steps=steps,
            auto_execute=auto_execute,
        )
        return {"message": f"Skill '{name}' saved", "skill": _skill_to_summary(skill)}


def list_skills(user, group=None, tier=None):
    """
    List all skills accessible to the user.

    Returns summary view (no steps) grouped by tier.
    """
    if not settings.get("LLM_ADMIN_SKILLS_ENABLED", True, kind="bool"):
        return {}

    from mojo.apps.assistant.models import Skill

    tiers_to_read = [tier] if tier and tier in VALID_TIERS else list(VALID_TIERS)
    result = {}

    for t in tiers_to_read:
        if not _can_read_tier(t, user, group):
            continue

        scope = _scope_filter(t, user, group)
        skills = Skill.objects.filter(**scope, is_active=True)
        if skills.exists():
            result[t] = [_skill_to_summary(s) for s in skills]

    return result


def delete_skill(user, skill_id):
    """
    Delete a skill by ID. Owner or superuser only.

    Returns dict with message or error.
    """
    if not settings.get("LLM_ADMIN_SKILLS_ENABLED", True, kind="bool"):
        return {"error": "Skills are disabled"}

    from mojo.apps.assistant.models import Skill

    try:
        skill = Skill.objects.get(pk=skill_id)
    except Skill.DoesNotExist:
        return {"error": f"Skill {skill_id} not found"}

    # Owner or superuser can delete
    if not user.is_superuser:
        if skill.tier == "user" and skill.user_id != user.pk:
            return {"error": "You can only delete your own skills"}
        if skill.tier == "group":
            if not skill.group:
                return {"error": "Permission denied"}
            member = skill.group.get_member_for_user(user, check_parents=True)
            if not member or not member.has_permission("assistant"):
                return {"error": "You need assistant permission in this group to delete skills"}
        if skill.tier == "global":
            if not user.has_permission("assistant"):
                return {"error": "You need assistant permission to delete global skills"}

    name = skill.name
    skill.delete()
    return {"message": f"Skill '{name}' deleted"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _scope_filter(tier, user, group):
    """Return a dict of filter kwargs for a specific tier scope."""
    f = {"tier": tier}
    if tier == "user":
        f["user"] = user
    elif tier == "group":
        f["group"] = group
    return f


def _scoped_queryset(user, group=None):
    """Build a queryset covering all tiers the user can read."""
    from mojo.apps.assistant.models import Skill

    q = Q()
    if _can_read_tier("global", user, group):
        q |= Q(tier="global")
    if _can_read_tier("user", user, group):
        q |= Q(tier="user", user=user)
    if _can_read_tier("group", user, group) and group:
        q |= Q(tier="group", group=group)

    if not q:
        return Skill.objects.none()
    return Skill.objects.filter(q)


def _skill_to_summary(skill):
    """Serialize a skill for list views (no steps)."""
    return {
        "id": skill.pk,
        "tier": skill.tier,
        "name": skill.name,
        "description": skill.description,
        "auto_execute": skill.auto_execute,
        "trigger_count": len(skill.triggers) if skill.triggers else 0,
        "step_count": len(skill.steps) if skill.steps else 0,
    }


def _skill_to_detail(skill):
    """Serialize a skill with full step definitions."""
    return {
        "id": skill.pk,
        "tier": skill.tier,
        "name": skill.name,
        "description": skill.description,
        "triggers": skill.triggers,
        "steps": skill.steps,
        "auto_execute": skill.auto_execute,
    }
