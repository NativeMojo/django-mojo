NAME = "webapps"


def describe(capabilities):
    values = {"view": capabilities["webapps"], "manage": capabilities["manage_webapps"]}
    return {"id": NAME, "enabled": values["view"], "capabilities": values}


def reset(handler, fixtures, *, key_state="active", **options):
    handler.key_state = key_state
