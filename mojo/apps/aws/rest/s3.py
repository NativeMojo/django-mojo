from mojo import decorators as md
from mojo import JsonResponse
from mojo.apps.account.services import fresh_auth
from mojo.helpers.aws import s3


def _invalid(message="Invalid S3 bucket request"):
    return JsonResponse({
        "status": False,
        "error": message,
        "error_code": "invalid_request",
    }, status=400)


def _resolve_target(data, path_name=None):
    if "group" in data:
        raise ValueError("Group context is not accepted for global S3 operations")
    body_name = data.get("bucket_name") if "bucket_name" in data else None
    if body_name is not None and (not isinstance(body_name, str) or not body_name.strip()):
        raise ValueError("Bucket name must be a nonempty string")
    if path_name is not None and (not isinstance(path_name, str) or not path_name.strip()):
        raise ValueError("Bucket name must be a nonempty string")
    if path_name is not None and body_name is not None and body_name != path_name:
        raise ValueError("Path and request bucket names must match")
    return path_name if path_name is not None else body_name


def _parse_post_action(data, bucket_name):
    allowed = {"bucket_name", "set_public", "empty"}
    if set(data.keys()) - allowed:
        raise ValueError("Unknown S3 bucket action")
    has_public = "set_public" in data
    has_empty = "empty" in data
    if has_public and has_empty:
        raise ValueError("Choose exactly one S3 bucket action")
    if has_public:
        value = data.get("set_public")
        if type(value) is not bool:
            raise ValueError("set_public must be a boolean")
        return "set_public", value
    if has_empty:
        value = data.get("empty")
        if not isinstance(value, dict) or set(value.keys()) != {"confirm_name"}:
            raise ValueError("empty requires only confirm_name")
        if value.get("confirm_name") != bucket_name:
            raise ValueError("empty confirm_name must exactly match the bucket name")
        return "empty", value
    return "create", None


def _provider_error_response(error, bucket_name=None, action=None):
    data = dict(error.data)
    if not data:
        data = {
            "complete": False,
            "mutation_state": error.mutation_state,
            "failure": error.failure(),
        }
        if bucket_name:
            data["name"] = bucket_name
        if action:
            data["action"] = action
    elif "failure" not in data:
        data["failure"] = error.failure()
    return JsonResponse({
        "status": False,
        "error": error.message,
        "error_code": error.error_code,
        "data": data,
    }, status=error.status)


@md.URL('s3/bucket')
@md.URL('s3/bucket/<str:bucket_name>')
@md.requires_global_perms("manage_aws", "files")
def on_s3_bucket(request, bucket_name=None):
    if request.method not in ("GET", "POST"):
        return JsonResponse({
            "status": False,
            "error": "S3 bucket method is not supported",
            "error_code": "method_not_allowed",
        }, status=405)

    data = request.DATA
    try:
        target = _resolve_target(data, bucket_name)
    except ValueError as error:
        return _invalid(str(error))

    if request.method == "GET":
        try:
            if target is None:
                buckets = s3.S3.list_all_buckets()
                return JsonResponse({
                    "status": True,
                    "data": buckets,
                    "count": len(buckets),
                    "size": len(buckets),
                })
            bucket = s3.S3Bucket(target)
            if not bucket.exists:
                return JsonResponse({
                    "status": False,
                    "error": "S3 bucket was not found",
                    "error_code": "s3_not_found",
                }, status=404)
            return JsonResponse({
                "status": True,
                "data": {"id": target, "name": target, "exists": True},
            })
        except s3.S3OperationError as error:
            return _provider_error_response(error, bucket_name=target, action="read")

    if target is None:
        return _invalid("Bucket name is required")
    try:
        action, action_value = _parse_post_action(data, target)
    except ValueError as error:
        return _invalid(str(error))

    if action == "empty":
        fresh_auth.require_fresh(request, seconds=300)

    try:
        bucket = s3.S3Bucket(target)
        if action == "create":
            result = bucket.create_private()
        elif action == "set_public":
            result = bucket.set_public(action_value)
        else:
            result = bucket.empty()
        return JsonResponse({"status": True, "data": result})
    except s3.S3OperationError as error:
        return _provider_error_response(error, bucket_name=target, action=action)
