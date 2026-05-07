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
        # Global skills affect all users — require view_admin (stricter than assistant)
        return user.has_permission("view_admin")
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

MAX_STEPS_BYTES = 16384  # 16 KB cap on entire steps JSON payload
MAX_TRIGGER_LENGTH = 200  # max chars per trigger phrase
MAX_DESCRIPTION_LENGTH = 1000  # max chars for skill description


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

    # Size cap on entire payload to prevent bloat
    import ujson
    try:
        if len(ujson.dumps(steps)) > MAX_STEPS_BYTES:
            return f"Steps payload too large (max {MAX_STEPS_BYTES} bytes)"
    except Exception:
        return "Steps contain non-serializable data"

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
        if len(t) > MAX_TRIGGER_LENGTH:
            return f"Trigger phrase too long (max {MAX_TRIGGER_LENGTH} characters)"
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
    if len(description.strip()) > MAX_DESCRIPTION_LENGTH:
        return {"error": f"Description too long (max {MAX_DESCRIPTION_LENGTH} characters)"}

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


def get_skill(user, skill_id, group=None):
    """
    Load a single skill by ID with permission check.

    Returns full detail dict or error dict.
    """
    if not settings.get("LLM_ADMIN_SKILLS_ENABLED", True, kind="bool"):
        return {"error": "Skills are disabled"}

    from mojo.apps.assistant.models import Skill

    try:
        skill = Skill.objects.get(pk=skill_id)
    except Skill.DoesNotExist:
        return {"error": f"Skill {skill_id} not found"}

    # Use skill's own group for group-tier checks, not caller's ambient group
    check_group = skill.group if skill.tier == "group" else group
    if not _can_read_tier(skill.tier, user, check_group):
        return {"error": "You don't have permission to view this skill"}

    # User-tier skills are scoped to owner
    if skill.tier == "user" and not user.is_superuser and skill.user_id != user.pk:
        return {"error": "You don't have permission to view this skill"}

    return _skill_to_detail(skill)


def update_skill(user, skill_id, group=None, **fields):
    """
    Partial update of a skill by ID.

    Only updates provided fields. Accepts any subset of:
    name, description, triggers, steps, auto_execute, is_active.

    Returns dict with updated skill summary or error.
    """
    if not settings.get("LLM_ADMIN_SKILLS_ENABLED", True, kind="bool"):
        return {"error": "Skills are disabled"}

    from mojo.apps.assistant.models import Skill

    try:
        skill = Skill.objects.get(pk=skill_id)
    except Skill.DoesNotExist:
        return {"error": f"Skill {skill_id} not found"}

    # Use skill's own group for group-tier checks, not caller's ambient group
    check_group = skill.group if skill.tier == "group" else group
    if not _can_write_tier(skill.tier, user, check_group):
        return {"error": f"You don't have permission to modify {skill.tier} skills"}

    # User-tier: only owner or superuser
    if skill.tier == "user" and not user.is_superuser and skill.user_id != user.pk:
        return {"error": "You can only modify your own skills"}

    UPDATABLE = {"name", "description", "triggers", "steps", "auto_execute", "is_active"}
    to_update = {k: v for k, v in fields.items() if k in UPDATABLE}

    if not to_update:
        return {"error": "No valid fields provided to update"}

    # Validate each field that's being changed
    if "name" in to_update:
        name = to_update["name"]
        if not name or not str(name).strip():
            return {"error": "Skill name is required"}
        name = str(name).strip()
        if len(name) > 128:
            return {"error": "Skill name too long (max 128 characters)"}
        # Check uniqueness within scope
        scope = _scope_filter(skill.tier, user if skill.tier == "user" else None,
                              skill.group if skill.tier == "group" else None)
        from mojo.apps.assistant.models import Skill as SkillModel
        collision = SkillModel.objects.filter(**scope, name=name).exclude(pk=skill_id).exists()
        if collision:
            return {"error": f"A skill named '{name}' already exists in this scope"}
        to_update["name"] = name

    if "description" in to_update:
        desc = to_update["description"]
        if not desc or not str(desc).strip():
            return {"error": "Skill description is required"}
        if len(str(desc).strip()) > MAX_DESCRIPTION_LENGTH:
            return {"error": f"Description too long (max {MAX_DESCRIPTION_LENGTH} characters)"}
        to_update["description"] = str(desc).strip()

    if "triggers" in to_update:
        err = _validate_triggers(to_update["triggers"])
        if err:
            return {"error": err}

    if "steps" in to_update:
        _, _, _, max_steps = _get_limits()
        err = _validate_steps(to_update["steps"], max_steps)
        if err:
            return {"error": err}

    for field, value in to_update.items():
        setattr(skill, field, value)

    update_fields = list(to_update.keys()) + ["modified"]
    skill.save(update_fields=update_fields)

    return {"message": f"Skill '{skill.name}' updated", "skill": _skill_to_summary(skill)}


def build_skill_catalog(user, group=None):
    """
    Build a markdown catalog of all accessible skills for injection
    into the system prompt.

    Returns markdown string or "" if no skills or skills disabled.
    """
    if not settings.get("LLM_ADMIN_SKILLS_ENABLED", True, kind="bool"):
        return ""

    qs = _scoped_queryset(user, group).filter(is_active=True)
    skills = list(qs)
    if not skills:
        return ""

    lines = []
    for s in skills:
        triggers_str = ""
        if s.triggers:
            triggers_str = " | Triggers: " + ", ".join(s.triggers)
        auto = " | AUTO-EXECUTE" if s.auto_execute else ""
        lines.append(
            f"- **{s.name}** (ID: {s.pk}, {s.tier}): "
            f"{s.description}{triggers_str}{auto}"
        )

    return "\n".join(lines)


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

    # Superuser bypasses all checks
    if not user.is_superuser:
        # Fail-closed: check tier-specific permission with elif chain
        if skill.tier == "user":
            if skill.user_id != user.pk:
                return {"error": "You can only delete your own skills"}
        elif skill.tier == "group":
            if not skill.group:
                return {"error": "Permission denied"}
            member = skill.group.get_member_for_user(user, check_parents=True)
            if not member or not member.has_permission("assistant"):
                return {"error": "You need assistant permission in this group to delete skills"}
        elif skill.tier == "global":
            if not user.has_permission("view_admin"):
                return {"error": "You need admin permission to delete global skills"}
        else:
            # Unknown tier — fail closed
            return {"error": "Permission denied"}

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
