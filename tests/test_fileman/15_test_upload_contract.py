"""Security and lifecycle contract for maestro #1485."""

import os
import tempfile

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


OWNER = "upload_contract_owner"
OTHER = "upload_contract_other"
ADMIN = "upload_contract_admin"
PASSWORD = "upload-contract-99!"


def _login(opts, username):
    assert_true(opts.client.login(username, PASSWORD), f"{username} login must succeed")


def _initiate(opts, **overrides):
    payload = {
        "filename": "contract.txt",
        "content_type": "text/plain",
        "file_size": 4,
        "file_manager": opts.owner_manager_id,
    }
    payload.update(overrides)
    return opts.client.post("/api/fileman/upload/initiate", payload)


@th.django_unit_setup()
def setup_upload_contract(opts):
    from mojo.apps.account.models import ApiKey, Group, GroupMember, User
    from mojo.apps.account.services import group_token
    from mojo.apps.fileman.models import File, FileManager, UploadInitiation
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")
    UploadInitiation.objects.filter(actor__username__in=[OWNER, OTHER, ADMIN]).delete()
    ApiKey.objects.filter(name__startswith="upload_contract_").delete()
    GroupMember.objects.filter(group__name__startswith="upload_contract_").delete()
    Group.objects.filter(name__startswith="upload_contract_").delete()
    User.objects.filter(username__in=[OWNER, OTHER, ADMIN]).delete()
    FileManager.objects.filter(name__startswith="upload_contract_").delete()

    def make_user(username):
        user = User(username=username, email=f"{username}@example.com")
        user.save()
        user.is_email_verified = True
        user.save_password(PASSWORD)
        user.save()
        return user

    owner = make_user(OWNER)
    other = make_user(OTHER)
    admin = make_user(ADMIN)
    admin.add_permission("manage_files")
    admin.save()
    group = Group(name="upload_contract_group")
    group.save()
    owner_member = group.add_member(owner)
    inactive_parent = Group(name="upload_contract_inactive_parent", is_active=False)
    inactive_parent.save()

    opts.owner_id = owner.id
    opts.other_id = other.id
    opts.admin_id = admin.id
    opts.group_id = group.id
    opts.owner_member_id = owner_member.id
    opts.inactive_parent_id = inactive_parent.id
    opts.tmpdir = tempfile.mkdtemp(prefix="upload_contract_")

    def make_manager(name, **scope):
        manager = FileManager(
            name=name,
            backend_type="file",
            backend_url="file://",
            is_active=True,
            allowed_extensions=[".TXT"],
            allowed_mime_types=["text/*"],
            max_file_size=32,
            **scope,
        )
        manager.save()
        manager.set_setting("base_path", opts.tmpdir)
        manager.save(update_fields=["mojo_secrets", "modified"])
        return manager

    opts.owner_manager_id = make_manager("upload_contract_owner_fm", user=owner).id
    opts.other_manager_id = make_manager("upload_contract_other_fm", user=other).id
    opts.group_manager_id = make_manager("upload_contract_group_fm", group=group).id
    opts.dual_manager_id = make_manager("upload_contract_dual_fm", user=owner, group=group).id
    opts.system_manager_id = make_manager("upload_contract_system_fm").id
    owner_manager = FileManager.objects.get(pk=opts.owner_manager_id)
    owner_manager.supports_direct_upload = True
    owner_manager.save(update_fields=["supports_direct_upload", "modified"])
    _, opts.reference_key = ApiKey.create_for_group(
        group, "upload_contract_reference_key", user=owner)
    _, opts.override_key = ApiKey.create_for_group(
        group, "upload_contract_override_key", user=owner, override_user=True)
    opts.group_token = group_token.mint(owner, group)
    File.objects.filter(user_id__in=[owner.id, other.id, admin.id]).delete()


@th.django_unit_test("upload contract: explicit owner manager is exact and response is capability-bounded")
def test_owner_manager_and_safe_shape(opts):
    from mojo.apps.fileman.models import File

    _login(opts, OWNER)
    response = _initiate(opts, filename="../safe.txt")
    assert_eq(response.status_code, 200, f"owner initiate must succeed: {response.response}")
    data = response.response.data
    assert_eq(data.filename, "safe.txt", f"client path must reduce to basename: {data}")
    assert_true("upload_url" in data, f"initiate must return a transfer target: {data}")
    for forbidden in ("upload_token", "storage_file_path", "metadata", "url", "renditions"):
        assert_true(forbidden not in data, f"initiate must omit {forbidden}: {data}")
    file = File.objects.get(pk=data.id)
    assert_eq(set(file.to_dict("reference")), {"id", "filename", "content_type", "category"},
              "reference graph must remain the exact shared four-field shape")


@th.django_unit_test("upload contract: cross-owner manager and system manager fail closed")
def test_cross_owner_and_system_denied(opts):
    _login(opts, OWNER)
    cross = _initiate(opts, file_manager=opts.other_manager_id)
    assert_eq(cross.status_code, 403, f"cross-owner manager must be denied: {cross.response}")
    system = _initiate(opts, file_manager=opts.system_manager_id)
    assert_eq(system.status_code, 403, f"system manager needs global storage grant: {system.response}")

    _login(opts, ADMIN)
    allowed = _initiate(opts, file_manager=opts.system_manager_id)
    assert_eq(allowed.status_code, 200, f"global file admin may use system manager: {allowed.response}")


@th.django_unit_test("upload contract: exact active group membership is required")
def test_group_scope(opts):
    _login(opts, OWNER)
    allowed = _initiate(opts, file_manager=opts.group_manager_id, group=opts.group_id)
    assert_eq(allowed.status_code, 200, f"active member in exact group must succeed: {allowed.response}")
    mismatch = _initiate(opts, file_manager=opts.owner_manager_id, group=opts.group_id)
    assert_eq(mismatch.status_code, 403, f"contradictory manager/group selectors must fail: {mismatch.response}")

    _login(opts, OTHER)
    denied = _initiate(opts, file_manager=opts.group_manager_id, group=opts.group_id)
    assert_eq(denied.status_code, 403, f"nonmember must not use group manager: {denied.response}")


@th.django_unit_test("upload contract: inactive scopes and dual scope fail closed")
def test_inactive_and_dual_scopes(opts):
    from mojo.apps.account.models import Group, GroupMember
    from mojo.apps.fileman.models import FileManager

    _login(opts, OWNER)
    manager = FileManager.objects.get(pk=opts.owner_manager_id)
    manager.is_active = False
    manager.save(update_fields=["is_active", "modified"])
    try:
        assert_eq(_initiate(opts).status_code, 403, "inactive manager must be unavailable")
    finally:
        manager.is_active = True
        manager.save(update_fields=["is_active", "modified"])

    member = GroupMember.objects.get(pk=opts.owner_member_id)
    member.is_active = False
    member.save(update_fields=["is_active", "modified"])
    try:
        denied = _initiate(opts, file_manager=opts.group_manager_id, group=opts.group_id)
        assert_eq(denied.status_code, 403, "inactive membership must not authorize upload")
    finally:
        member.is_active = True
        member.save(update_fields=["is_active", "modified"])

    group = Group.objects.get(pk=opts.group_id)
    group.parent_id = opts.inactive_parent_id
    group.save(update_fields=["parent", "modified"])
    try:
        denied = _initiate(opts, file_manager=opts.group_manager_id, group=opts.group_id)
        assert_eq(denied.status_code, 403, "group under an inactive ancestor must fail closed")
    finally:
        group.parent = None
        group.save(update_fields=["parent", "modified"])

    allowed = _initiate(opts, file_manager=opts.dual_manager_id, group=opts.group_id)
    assert_eq(allowed.status_code, 200, f"dual scope needs both matching constraints: {allowed.response}")
    missing_group = _initiate(opts, file_manager=opts.dual_manager_id)
    assert_eq(missing_group.status_code, 403, "dual scope must not degrade to its user half")
    _login(opts, OTHER)
    denied_user = _initiate(opts, file_manager=opts.dual_manager_id, group=opts.group_id)
    assert_eq(denied_user.status_code, 403, "dual scope must enforce its user half")


@th.django_unit_test("upload contract: key-backed and restricted identities cannot initiate")
def test_machine_identities_denied(opts):
    payload = {
        "filename": "contract.txt", "content_type": "text/plain", "file_size": 4,
        "file_manager": opts.group_manager_id, "group": opts.group_id,
    }
    for scheme, token in (
        ("apikey", opts.reference_key),
        ("apikey", opts.override_key),
        ("grouptoken", opts.group_token),
    ):
        response = opts.client.post(
            "/api/fileman/upload/initiate", payload,
            headers={"Authorization": f"{scheme} {token}"},
        )
        assert_eq(response.status_code, 403,
                  f"{scheme} identity must not receive an upload capability: {response.response}")


@th.django_unit_test("upload contract: normalized policy rejects malformed and disallowed declarations")
def test_policy_grammar(opts):
    _login(opts, OWNER)
    assert_eq(_initiate(opts, filename="no_extension").status_code, 400,
              "extension allowlist must deny suffixless filenames")
    assert_eq(_initiate(opts, file_size=-1).status_code, 400,
              "negative declared size must be rejected")
    assert_eq(_initiate(opts, file_size=True).status_code, 400,
              "boolean declared size must be rejected")
    assert_eq(_initiate(opts, content_type="image/png").status_code, 400,
              "MIME wildcard policy must deny another major type")
    from mojo.apps.fileman.models import FileManager
    manager = FileManager.objects.get(pk=opts.owner_manager_id)
    policy = manager.to_dict("upload_policy")
    assert_eq(set(policy), {
        "id", "name", "use", "is_active", "max_file_size",
        "allowed_extensions", "allowed_mime_types", "supports_direct_upload",
    }, "upload_policy graph must omit all backend and credential configuration")


@th.django_unit_test("upload contract: idempotent initiation reuses File and conflicts on fingerprint")
def test_idempotent_initiation(opts):
    from mojo.apps.fileman.models import File, UploadInitiation

    _login(opts, OWNER)
    first = _initiate(opts, idempotency_key="portal:upload-1")
    second = _initiate(opts, idempotency_key="portal:upload-1")
    assert_eq(first.status_code, 200, f"first keyed initiate must succeed: {first.response}")
    assert_eq(second.status_code, 200, f"same keyed initiate must recover: {second.response}")
    assert_eq(first.response.data.id, second.response.data.id, "same key/fingerprint must reuse one File")
    assert_true(first.response.data.upload_url != second.response.data.upload_url,
                "uploading local recovery must rotate the bearer target")
    assert_eq(File.objects.filter(pk=first.response.data.id).count(), 1,
              "idempotent replay must leave one File row")
    conflict = _initiate(opts, idempotency_key="portal:upload-1", filename="other.txt")
    assert_eq(conflict.status_code, 409, f"same key with changed fingerprint must conflict: {conflict.response}")
    bad_key = _initiate(opts, idempotency_key="contains space")
    assert_eq(bad_key.status_code, 400, "idempotency key grammar must reject whitespace")
    unkeyed_one = _initiate(opts)
    unkeyed_two = _initiate(opts)
    assert_true(unkeyed_one.response.data.id != unkeyed_two.response.data.id,
                "omitting idempotency_key must create distinct File rows")
    assert_eq(UploadInitiation.objects.filter(actor_id=opts.owner_id).count(), 1,
              "only keyed initiation may persist internal retry state")


@th.django_unit_test("upload contract: keyed replay exposes targets only while uploading")
def test_idempotent_terminal_replay(opts):
    from mojo.apps.fileman.models import File

    _login(opts, OWNER)
    completed = _initiate(opts, idempotency_key="portal:completed-replay")
    File.objects.filter(pk=completed.response.data.id).update(upload_status=File.COMPLETED)
    replay = _initiate(opts, idempotency_key="portal:completed-replay")
    assert_eq(replay.status_code, 200, f"completed replay returns lifecycle state: {replay.response}")
    assert_eq(replay.response.data.upload_status, File.COMPLETED, "completed state must be authoritative")
    assert_true("upload_url" not in replay.response.data, "completed replay must not return a writable target")

    for status, key in ((File.FAILED, "portal:failed-replay"),
                        (File.EXPIRED, "portal:expired-replay")):
        first = _initiate(opts, idempotency_key=key)
        File.objects.filter(pk=first.response.data.id).update(upload_status=status)
        terminal = _initiate(opts, idempotency_key=key)
        assert_eq(terminal.status_code, 200, f"terminal replay returns lifecycle state: {terminal.response}")
        assert_eq(terminal.response.data.upload_status, status, "terminal state must remain terminal")
        assert_true("upload_url" not in terminal.response.data,
                    "failed and expired retries must not mint writable targets")


@th.django_unit_test("upload contract: local transfer stays uploading, retries, then completes once")
def test_local_transfer_then_completion(opts):
    from mojo.apps.fileman.models import File
    from mojo.apps.jobs.models import Job

    _login(opts, OWNER)
    initiated = _initiate(opts, idempotency_key="portal:transfer-1")
    data = initiated.response.data
    expected_lifecycle = {
        "id", "filename", "content_type", "file_size", "category",
        "upload_status", "is_active", "user_id", "group_id", "file_manager_id",
    }
    assert_eq(set(data) - {"upload_url", "method", "fields", "headers"}, expected_lifecycle,
              "initiation lifecycle fields must remain an exact capability-free set")
    target = data.upload_url
    headers = {"Content-Type": "text/plain", "Content-Length": "4"}
    transfer = opts.client.put(target, data=b"test", headers=headers)
    assert_eq(transfer.status_code, 200, f"raw PUT transfer must succeed: {transfer.response}")
    assert_eq(set(transfer.response.file), expected_lifecycle,
              "transfer response must contain only capability-free lifecycle fields")
    file = File.objects.get(pk=data.id)
    assert_eq(file.upload_status, File.UPLOADING,
              "successful local transfer must await explicit completion")
    retry = opts.client.put(target, data=b"test", headers=headers)
    assert_eq(retry.status_code, 200, f"same uploading token must remain retryable: {retry.response}")

    completed = opts.client.post(f"/api/fileman/file/{file.id}", {"action": "mark_as_completed"})
    assert_eq(completed.status_code, 200, f"explicit completion must succeed: {completed.response}")
    assert_eq(set(completed.response), expected_lifecycle | {"code", "server"},
              "completion response must contain only capability-free lifecycle fields")
    again = opts.client.post(f"/api/fileman/file/{file.id}", {"action": "mark_as_completed"})
    assert_eq(again.status_code, 200, f"completion replay must be side-effect-idempotent: {again.response}")
    file.refresh_from_db()
    assert_eq(file.upload_status, File.COMPLETED, "explicit completion must set completed")
    assert_eq(Job.objects.filter(idempotency_key=f"renditions:{file.id}").count(), 1,
              "completion replay must publish exactly one rendition job")
    stale = opts.client.put(target, data=b"test", headers=headers)
    assert_eq(stale.status_code, 404, "completed local token must no longer be usable")


@th.django_unit_test("upload contract: multipart and actual-byte validation clean partial objects")
def test_multipart_and_partial_cleanup(opts):
    from mojo.apps.fileman.models import File

    _login(opts, OWNER)
    good = _initiate(opts, idempotency_key="portal:multipart-good")
    response = opts.client.post(
        good.response.data.upload_url,
        files={"file": ("contract.txt", b"test", "text/plain")},
    )
    assert_eq(response.status_code, 200, f"multipart transfer must be supported: {response.response}")
    assert_eq(File.objects.get(pk=good.response.data.id).upload_status, File.UPLOADING,
              "multipart transfer also awaits explicit completion")

    short = _initiate(opts, idempotency_key="portal:multipart-short")
    short_file = File.objects.get(pk=short.response.data.id)
    response = opts.client.post(
        short.response.data.upload_url,
        files={"file": ("contract.txt", b"bad", "text/plain")},
    )
    assert_eq(response.status_code, 400, "actual-size mismatch must fail validation")
    short_file.refresh_from_db()
    assert_eq(short_file.upload_status, File.FAILED, "size mismatch must become terminal")
    assert_true(not short_file.file_manager.backend.exists(short_file.storage_file_path),
                "size mismatch must remove the partial storage object")

    disguised_bytes = b"%PDF-1.4\n1 0 obj\n"
    disguised = _initiate(
        opts, filename="disguised.txt", file_size=len(disguised_bytes),
        idempotency_key="portal:multipart-type")
    disguised_file = File.objects.get(pk=disguised.response.data.id)
    response = opts.client.post(
        disguised.response.data.upload_url,
        files={"file": ("disguised.txt", disguised_bytes, "text/plain")},
    )
    assert_eq(response.status_code, 400, "actual MIME outside policy must fail validation")
    disguised_file.refresh_from_db()
    assert_eq(disguised_file.upload_status, File.FAILED, "MIME mismatch must become terminal")
    assert_true(not disguised_file.file_manager.backend.exists(disguised_file.storage_file_path),
                "MIME mismatch must remove the partial storage object")


@th.django_unit_test("upload contract: raw PUT requires length and accepts declared zero bytes")
def test_raw_put_length_and_zero(opts):
    _login(opts, OWNER)
    missing = _initiate(opts, idempotency_key="portal:no-length")
    response = opts.client.put(
        missing.response.data.upload_url,
        data=(chunk for chunk in (b"test",)),
        headers={"Content-Type": "text/plain", "Transfer-Encoding": "chunked"},
    )
    assert_eq(response.status_code, 400, "raw PUT without a usable Content-Length must fail")

    zero = _initiate(opts, filename="empty.txt", file_size=0, idempotency_key="portal:zero")
    empty = opts.client.put(
        zero.response.data.upload_url,
        data=b"",
        headers={"Content-Type": "text/plain", "Content-Length": "0"},
    )
    assert_eq(empty.status_code, 200, f"declared zero-byte transfer must succeed: {empty.response}")


@th.django_unit_setup()
def cleanup_upload_contract(opts):
    import shutil
    from mojo.apps.account.models import ApiKey, Group, GroupMember, User
    from mojo.apps.fileman.models import File, FileManager, UploadInitiation

    UploadInitiation.objects.filter(actor_id__in=[opts.owner_id, opts.other_id, opts.admin_id]).delete()
    File.objects.filter(user_id__in=[opts.owner_id, opts.other_id, opts.admin_id]).delete()
    FileManager.objects.filter(name__startswith="upload_contract_").delete()
    ApiKey.objects.filter(name__startswith="upload_contract_").delete()
    GroupMember.objects.filter(group_id=opts.group_id).delete()
    Group.objects.filter(pk=opts.group_id).delete()
    Group.objects.filter(pk=opts.inactive_parent_id).delete()
    User.objects.filter(pk__in=[opts.owner_id, opts.other_id, opts.admin_id]).delete()
    if os.path.exists(opts.tmpdir):
        shutil.rmtree(opts.tmpdir, ignore_errors=True)
