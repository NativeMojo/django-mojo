NAME = "activity"


def describe(capabilities):
    return {"id": NAME, "enabled": False, "capabilities": {"view": False}}


def reset(handler, fixtures, **options):
    return None
