NAME = "people"


def describe(capabilities):
    values = {"users": capabilities["people"], "groups": capabilities["groups"]}
    return {"id": NAME, "enabled": any(values.values()), "capabilities": values}


def reset(handler, fixtures, **options):
    return None
