from mojo import decorators as md
from mojo import JsonResponse
from mojo.helpers.aws import s3


@md.URL('s3/bucket')
@md.URL('s3/bucket/<str:bucket_name>')
@md.requires_perms("manage_aws")
def on_s3_bucket(request, bucket_name=None):
    bucket_name = request.DATA.get('bucket_name', bucket_name)
    if request.method == "GET":
        if bucket_name is None:
            # List all buckets
            buckets = s3.S3.list_all_buckets()
            return JsonResponse({
                "size": len(buckets),
                "count": len(buckets),
                "data": buckets,
                "status": True
            })
        else:
            # Get specific bucket info
            bucket = s3.S3Bucket(bucket_name)
            if bucket._check_exists():
                return JsonResponse({"data": {"name": bucket_name, "exists": True}, "status": True})
            else:
                return JsonResponse({"error": "Bucket not found", "code": 404}, status=404)

    elif request.method == "POST":
        if bucket_name is None:
            return JsonResponse({"error": "Bucket name required"}, status=400)

        # Create or update bucket
        bucket = s3.S3Bucket(bucket_name)
        try:
            if not bucket._check_exists():
                bucket.create()
                bucket.enable_cors()
                return JsonResponse({"message": f"Bucket {bucket_name} created successfully"})
            else:
                return JsonResponse({"message": f"Bucket {bucket_name} already exists"})
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    # elif request.method == "DELETE":
    #     if bucket_name is None:
    #         return JsonResponse({"error": "Bucket name required"}, status=400)

    #     # Check for confirmation
    #     if request.DATA.get("confirm_delete") != "yes delete bucket":
    #         return JsonResponse({"error": "Confirmation required: confirm_delete = 'yes delete bucket'"}, status=400)

    #     # Delete bucket
    #     bucket = s3.S3Bucket(bucket_name)
    #     try:
    #         if bucket._check_exists():
    #             bucket.delete()
    #             return JsonResponse({"message": f"Bucket {bucket_name} deleted successfully"})
    #         else:
    #             return JsonResponse({"error": "Bucket not found"}, status=404)
    #     except Exception as e:
    #         return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"message": "Invalid request method"}, status=405)
