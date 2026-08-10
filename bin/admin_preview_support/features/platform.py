NAME = "platform"


def describe(capabilities):
    return {"id": NAME, "enabled": capabilities["setup"],
            "capabilities": {"setup": capabilities["setup"]}}


def reset(handler, fixtures, *, setup_state="idle", **options):
    handler.setup_operation = fixtures["setup_choice"]() if setup_state == "choice" else None
