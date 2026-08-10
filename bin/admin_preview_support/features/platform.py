from copy import deepcopy


NAME = "platform"


def describe(capabilities):
    values = {"view": capabilities["network"], "manage": capabilities["manage_network"]}
    return {"id": NAME, "enabled": values["view"], "capabilities": values}


def reset(handler, fixtures, **options):
    for name in ("records", "credentials", "vhosts", "routes"):
        setattr(handler, name, deepcopy(fixtures[name]))
