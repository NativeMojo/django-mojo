NAME = "dashboard"


def describe(capabilities):
    return {"id": NAME, "enabled": True, "capabilities": {"view": True}}


def reset(handler, fixtures, **options):
    handler.events = []
