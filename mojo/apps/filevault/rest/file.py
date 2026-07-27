import mojo.decorators as md
import mojo.errors as me
from mojo.helpers import logit
from mojo.helpers.request import get_remote_ip
from mojo.helpers.crypto import vault as crypto_vault
from mojo.apps.filevault.models import VaultFile, VaultAccessLog
from mojo.apps.filevault.services import vault as vault_service


@md.URL('file')
@md.URL('file/<int:pk>')
def on_vault_file(request, pk=None):
    return VaultFile.on_rest_request(request, pk)


@md.POST('file/upload')
@md.requires_auth()
def on_vault_file_upload(request):
    """Upload and encrypt a file."""
    uploaded = request.FILES.get("file")
    if not uploaded:
        raise me.ValueException("No file provided")

    password = request.DATA.get("password", None)
    description = request.DATA.get("description", None)
    name = request.DATA.get("name", uploaded.name)
    metadata = request.DATA.get("metadata", {})
    if isinstance(metadata, str):
        import json
        metadata = json.loads(metadata)

    group = request.group
    if not group:
        raise me.ValueException("Group required")

    vault_file = vault_service.upload_file(
        file_obj=uploaded,
        name=name,
        group=group,
        user=request.user,
        password=password,
        description=description,
        metadata=metadata,
    )
    return vault_file.on_rest_get(request)


@md.POST('file/<int:pk>/unlock')
@md.requires_auth()
def on_vault_file_unlock(request, pk=None):
    """Generate a signed, IP-bound download token."""
    vault_file = VaultFile.get_instance_or_404(pk)
    try:
        VaultFile.rest_check_permission_or_raise(request, "VIEW_PERMS", vault_file)
    except Exception:
        # Denials land in the TARGET's trail. The framework already emits a
        # permission-denied incident, but that lives in the security plane keyed
        # to the actor; the owner's "who tried to get at my secret" belongs on
        # the row itself.
        VaultAccessLog.record(
            VaultAccessLog.ACTION_UNLOCK, VaultAccessLog.RESULT_DENIED,
            vault_file=vault_file, request=request,
            reason=VaultAccessLog.REASON_PERMISSION_DENIED)
        raise

    # A password-protected file requires proof of the password to mint a
    # download capability — VIEW access alone must not let a caller who does
    # not know the password initiate a share.
    if vault_file.hashed_password:
        password = request.DATA.get("password", None)
        if not password or not crypto_vault.verify_password(password, vault_file.hashed_password):
            # These take the generic error branch rather than the denial one,
            # so without this row a password brute-force against a vault file
            # would leave no audit trace at all.
            VaultAccessLog.record(
                VaultAccessLog.ACTION_UNLOCK, VaultAccessLog.RESULT_DENIED,
                vault_file=vault_file, request=request,
                reason=VaultAccessLog.REASON_INVALID_PASSWORD)
            raise me.ValueException("Invalid password", code=403)

    ttl = crypto_vault.clamp_token_ttl(request.DATA.get("ttl", None))
    client_ip = get_remote_ip(request)
    token = vault_service.generate_download_token(vault_file, client_ip, ttl=ttl)
    download_url = f"/api/filevault/file/download/{token}"

    vault_file.unlocked_by = request.user
    vault_file.save()

    VaultAccessLog.record(
        VaultAccessLog.ACTION_UNLOCK, VaultAccessLog.RESULT_GRANTED,
        vault_file=vault_file, request=request)

    return dict(
        token=token,
        download_url=download_url,
        ttl=ttl,
    )


@md.POST('file/<int:pk>/password')
@md.requires_auth()
@md.requires_params("password")
def on_vault_file_password(request, pk=None):
    """Verify a password without downloading."""
    vault_file = VaultFile.get_instance_or_404(pk)
    try:
        VaultFile.rest_check_permission_or_raise(request, "VIEW_PERMS", vault_file)
    except Exception:
        VaultAccessLog.record(
            VaultAccessLog.ACTION_PASSWORD, VaultAccessLog.RESULT_DENIED,
            vault_file=vault_file, request=request,
            reason=VaultAccessLog.REASON_PERMISSION_DENIED)
        raise

    if not vault_file.hashed_password:
        return dict(valid=True, message="File is not password-protected")

    password = request.DATA.get("password")
    valid = crypto_vault.verify_password(password, vault_file.hashed_password)
    # This endpoint is the cheapest password-guessing surface in the app — it
    # returns a clean true/false with no download. Both outcomes are recorded.
    VaultAccessLog.record(
        VaultAccessLog.ACTION_PASSWORD,
        VaultAccessLog.RESULT_GRANTED if valid else VaultAccessLog.RESULT_DENIED,
        vault_file=vault_file, request=request,
        reason="" if valid else VaultAccessLog.REASON_INVALID_PASSWORD)
    return dict(valid=valid)


@md.GET('file/download/<str:token>')
@md.public_endpoint("Token-secured vault file download")
def on_vault_file_download(request, token=None):
    """Download a file using a signed access token."""
    from django.http import StreamingHttpResponse

    client_ip = get_remote_ip(request)
    vault_file = vault_service.validate_download_token(token, client_ip)
    if not vault_file:
        # Deliberately NOT logged. This branch is reached by any garbage token,
        # so writing a row here would make the public endpoint an
        # unauthenticated way to flood an arbitrary tenant's trail — burying
        # the real entries is the classic way to defeat an audit log. Only a
        # token that resolved to a real file gets a row.
        raise me.ValueException("Invalid or expired token", code=403)

    password = request.DATA.get("password", None)

    try:
        chunks = vault_service.download_file_streaming(vault_file, password=password)
        response = StreamingHttpResponse(
            chunks,
            content_type=vault_file.content_type,
        )
        response["Content-Disposition"] = f'attachment; filename="{vault_file.name}"'
        if vault_file.size:
            response["Content-Length"] = str(vault_file.size)
        # Recorded after the eager work succeeded but before the body streams:
        # everything that can fail has already run (download_file_streaming is
        # no longer a generator function), so reaching here means the bytes
        # will be served. user is NULL — this endpoint is public and
        # token-authenticated, there is no logged-in caller.
        VaultAccessLog.record(
            VaultAccessLog.ACTION_DOWNLOAD, VaultAccessLog.RESULT_GRANTED,
            vault_file=vault_file, request=request)
        return response
    except ValueError as e:
        msg = str(e)
        reason = (VaultAccessLog.REASON_PASSWORD_REQUIRED
                  if "required" in msg.lower()
                  else VaultAccessLog.REASON_INVALID_PASSWORD)
        VaultAccessLog.record(
            VaultAccessLog.ACTION_DOWNLOAD, VaultAccessLog.RESULT_DENIED,
            vault_file=vault_file, request=request, reason=reason)
        raise me.ValueException(msg, code=403)
