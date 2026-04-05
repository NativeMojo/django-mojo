"""
REST endpoints for assistant memory management.

Endpoints:
    GET    /api/assistant/memory              — List all tiers
    GET    /api/assistant/memory/<tier>        — List one tier
    POST   /api/assistant/memory/<tier>        — Create/update entry
    DELETE /api/assistant/memory/<tier>/<key>  — Delete entry
"""
from mojo import decorators as md
from mojo.helpers.response import JsonResponse


@md.GET('/api/assistant/memory')
@md.requires_perms('assistant')
def on_memory_list_all(request):
    """List all memory tiers for the current user and group context."""
    from mojo.apps.assistant.services.memory import read_memories

    group = getattr(request, "group", None)
    result = read_memories(request.user, group=group)
    return JsonResponse({"status": True, "data": result})


@md.GET('/api/assistant/memory/<str:tier>')
@md.requires_perms('assistant')
def on_memory_list_tier(request, tier):
    """List memories for a specific tier."""
    from mojo.apps.assistant.services.memory import read_memories, VALID_TIERS

    if tier not in VALID_TIERS:
        return JsonResponse({"status": False, "error": f"Invalid tier: {tier}"}, status=400)

    group = getattr(request, "group", None)
    result = read_memories(request.user, group=group, tier=tier)
    return JsonResponse({"status": True, "data": result.get(tier, {})})


@md.POST('/api/assistant/memory/<str:tier>')
@md.requires_perms('assistant')
@md.requires_params('key', 'value')
def on_memory_write(request, tier):
    """Create or update a memory entry."""
    from mojo.apps.assistant.services.memory import write_memory, VALID_TIERS

    if tier not in VALID_TIERS:
        return JsonResponse({"status": False, "error": f"Invalid tier: {tier}"}, status=400)

    group = getattr(request, "group", None)
    key = request.DATA.key
    value = request.DATA.value

    result = write_memory(request.user, tier=tier, key=key, value=value, group=group)

    if "error" in result:
        return JsonResponse({"status": False, "error": result["error"]}, status=400)

    return JsonResponse({"status": True, "data": result})


@md.DELETE('/api/assistant/memory/<str:tier>/<str:key>')
@md.requires_perms('assistant')
def on_memory_delete(request, tier, key):
    """Delete a memory entry."""
    from mojo.apps.assistant.services.memory import delete_memory, VALID_TIERS

    if tier not in VALID_TIERS:
        return JsonResponse({"status": False, "error": f"Invalid tier: {tier}"}, status=400)

    group = getattr(request, "group", None)
    result = delete_memory(request.user, tier=tier, key=key, group=group)

    if "error" in result:
        return JsonResponse({"status": False, "error": result["error"]}, status=400)

    return JsonResponse({"status": True, "data": result})
