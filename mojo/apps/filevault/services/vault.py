"""
FileVault service layer.

Orchestrates crypto helpers, FileManager storage, and model persistence.
"""

import json
import uuid
import mimetypes
from io import BytesIO
from django.conf import settings as django_settings

from mojo.helpers import logit
from mojo.helpers.crypto import vault as crypto_vault


def _get_secret_key():
    return django_settings.SECRET_KEY


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def upload_file(file_obj, name, group, user=None, password=None, description=None, metadata=None):
    """
    Encrypt and upload a file to S3 via FileManager.

    Args:
        file_obj: file-like object with .read()
        name: original filename
        group: Group instance
        user: User instance (optional)
        password: optional password for extra protection
        description: optional description
        metadata: optional dict of metadata

    Returns:
        VaultFile instance
    """
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.filevault.models import VaultFile

    # resolve storage
    fm = FileManager.get_for_group(group, use="filevault")
    if fm and fm.is_public:
        fm.is_public = False
        fm.save()

    # read file content
    file_bytes = file_obj.read()
    content_type = getattr(file_obj, "content_type", None)
    if not content_type:
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

    # generate crypto material
    ekey = crypto_vault.generate_ekey()
    file_uuid = uuid.uuid4().hex

    # encrypt the file
    encrypted_blob = crypto_vault.encrypt_file(file_bytes, ekey, password=password)

    # parse header to get chunk count
    header_info = crypto_vault.parse_header(encrypted_blob[:crypto_vault.VAULT_HEADER_SIZE])

    # wrap ekey for storage
    wrapped_ekey = crypto_vault.wrap_ekey(ekey, _get_secret_key(), file_uuid)

    # hash password if provided
    hashed_pw = None
    if password:
        hashed_pw = crypto_vault.hash_password(password)

    # create DB record
    vault_file = VaultFile(
        uuid=file_uuid,
        name=name,
        content_type=content_type,
        description=description or "",
        size=len(file_bytes),
        chunk_count=header_info["total_chunks"],
        is_encrypted=2,
        ekey=wrapped_ekey,
        hashed_password=hashed_pw,
        user=user,
        group=group,
        metadata=metadata or {},
    )
    vault_file.save()

    # upload encrypted blob to S3
    storage_path = f"{fm.root_path}/{file_uuid}"
    blob_io = BytesIO(encrypted_blob)
    fm.backend.save(blob_io, storage_path, content_type="application/octet-stream")

    logit.info(f"filevault: uploaded {name} ({len(file_bytes)} bytes) as {file_uuid}")
    return vault_file


def download_file(vault_file, password=None):
    """
    Decrypt and return file content.

    Args:
        vault_file: VaultFile instance
        password: password if file is password-protected

    Returns:
        bytes (decrypted file content)

    Raises:
        ValueError on wrong password or decryption failure
    """
    from mojo.apps.fileman.models import FileManager

    # verify password if required
    if vault_file.hashed_password:
        if not password:
            raise ValueError("Password required")
        if not crypto_vault.verify_password(password, vault_file.hashed_password):
            raise ValueError("Invalid password")

    # unwrap ekey
    ekey = crypto_vault.unwrap_ekey(vault_file.ekey, _get_secret_key(), vault_file.uuid)

    # fetch encrypted blob from S3
    fm = FileManager.get_for_group(vault_file.group, use="filevault")
    storage_path = f"{fm.root_path}/{vault_file.uuid}"
    s3_body = fm.backend.open(storage_path)
    encrypted_blob = s3_body.read()

    # decrypt
    return crypto_vault.decrypt_file(encrypted_blob, ekey, password=password)


def download_file_streaming(vault_file, password=None):
    """
    Generator that yields decrypted chunks for StreamingHttpResponse.

    Args:
        vault_file: VaultFile instance
        password: password if file is password-protected

    Yields:
        bytes chunks of decrypted plaintext
    """
    from mojo.apps.fileman.models import FileManager

    # verify password if required
    if vault_file.hashed_password:
        if not password:
            raise ValueError("Password required")
        if not crypto_vault.verify_password(password, vault_file.hashed_password):
            raise ValueError("Invalid password")

    # unwrap ekey
    ekey = crypto_vault.unwrap_ekey(vault_file.ekey, _get_secret_key(), vault_file.uuid)

    # fetch encrypted blob from S3
    fm = FileManager.get_for_group(vault_file.group, use="filevault")
    storage_path = f"{fm.root_path}/{vault_file.uuid}"
    s3_body = fm.backend.open(storage_path)

    # read and parse header
    header_bytes = s3_body.read(crypto_vault.VAULT_HEADER_SIZE)
    header_info = crypto_vault.parse_header(header_bytes)
    chunk_size = header_info["chunk_size"]
    kdf_salt = header_info["kdf_salt"]
    total_chunks = header_info["total_chunks"]

    passphrase = (password + ekey) if password else ekey
    aes_key = crypto_vault.derive_aes_key(passphrase, kdf_salt)

    # stream decrypted chunks
    enc_chunk_size = crypto_vault.VAULT_NONCE_LENGTH + chunk_size + crypto_vault.VAULT_TAG_LENGTH
    for i in range(total_chunks):
        if i < total_chunks - 1:
            chunk_data = s3_body.read(enc_chunk_size)
        else:
            chunk_data = s3_body.read()  # last chunk may be smaller
        yield crypto_vault.decrypt_chunk(aes_key, i, chunk_data)


def delete_s3_object(vault_file):
    """Delete the S3 object for a VaultFile."""
    from mojo.apps.fileman.models import FileManager

    fm = FileManager.get_for_group(vault_file.group, use="filevault")
    if fm:
        storage_path = f"{fm.root_path}/{vault_file.uuid}"
        fm.backend.delete(storage_path)
        logit.info(f"filevault: deleted S3 object for {vault_file.uuid}")


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------

def generate_download_token(vault_file, client_ip, ttl=None):
    """Generate a signed, IP-bound download token."""
    token = crypto_vault.generate_access_token(
        vault_file.pk, client_ip, _get_secret_key(), ttl=ttl
    )
    return token


def validate_download_token(token, client_ip):
    """
    Validate a download token.

    Returns VaultFile on success, None on failure.
    """
    from mojo.apps.filevault.models import VaultFile

    file_id = crypto_vault.validate_access_token(token, client_ip, _get_secret_key())
    if file_id is None:
        return None
    try:
        return VaultFile.objects.get(pk=file_id)
    except VaultFile.DoesNotExist:
        return None


# ---------------------------------------------------------------------------
# Data operations (VaultData)
# ---------------------------------------------------------------------------

def store_data(group, user, name, data, password=None, description=None, metadata=None):
    """
    Encrypt and store JSON data in the database.

    Args:
        group: Group instance
        user: User instance
        name: data name
        data: dict to encrypt
        password: optional password
        description: optional description
        metadata: optional dict

    Returns:
        VaultData instance
    """
    from mojo.apps.filevault.models import VaultData

    ekey = crypto_vault.generate_ekey()
    data_uuid = uuid.uuid4().hex

    # encrypt the JSON data (single-shot, not chunked)
    plaintext = json.dumps(data).encode("utf-8")
    kdf_salt = crypto_vault.get_random_bytes(crypto_vault.VAULT_SALT_LENGTH)
    passphrase = (password + ekey) if password else ekey
    aes_key = crypto_vault.derive_aes_key(passphrase, kdf_salt)
    nonce = crypto_vault.get_random_bytes(crypto_vault.VAULT_NONCE_LENGTH)
    from Crypto.Cipher import AES as _AES
    cipher = _AES.new(aes_key, _AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)

    # pack: [salt(32)][nonce(12)][ciphertext][tag(16)]
    from base64 import b64encode
    edata = b64encode(kdf_salt + nonce + ciphertext + tag).decode("utf-8")

    # wrap ekey
    wrapped_ekey = crypto_vault.wrap_ekey(ekey, _get_secret_key(), data_uuid)

    # hash password if provided
    hashed_pw = None
    if password:
        hashed_pw = crypto_vault.hash_password(password)

    vault_data = VaultData(
        name=name,
        description=description or "",
        ekey=wrapped_ekey,
        edata=edata,
        hashed_password=hashed_pw,
        user=user,
        group=group,
        metadata=metadata or {},
    )
    # store the uuid in metadata so we can unwrap the ekey
    vault_data.metadata["_uuid"] = data_uuid
    vault_data.save()

    logit.info(f"filevault: stored data '{name}' for group {group.pk}")
    return vault_data


def retrieve_data(vault_data, password=None):
    """
    Decrypt and return VaultData content.

    Returns:
        dict (the original JSON data)

    Raises:
        ValueError on wrong password or decryption failure
    """
    # verify password if required
    if vault_data.hashed_password:
        if not password:
            raise ValueError("Password required")
        if not crypto_vault.verify_password(password, vault_data.hashed_password):
            raise ValueError("Invalid password")

    data_uuid = vault_data.metadata.get("_uuid", "")
    ekey = crypto_vault.unwrap_ekey(vault_data.ekey, _get_secret_key(), data_uuid)

    from base64 import b64decode
    raw = b64decode(vault_data.edata)
    kdf_salt = raw[:crypto_vault.VAULT_SALT_LENGTH]
    nonce = raw[crypto_vault.VAULT_SALT_LENGTH:crypto_vault.VAULT_SALT_LENGTH + crypto_vault.VAULT_NONCE_LENGTH]
    tag = raw[-crypto_vault.VAULT_TAG_LENGTH:]
    ciphertext = raw[crypto_vault.VAULT_SALT_LENGTH + crypto_vault.VAULT_NONCE_LENGTH:-crypto_vault.VAULT_TAG_LENGTH]

    passphrase = (password + ekey) if password else ekey
    aes_key = crypto_vault.derive_aes_key(passphrase, kdf_salt)

    from Crypto.Cipher import AES as _AES
    cipher = _AES.new(aes_key, _AES.MODE_GCM, nonce=nonce)
    plaintext = cipher.decrypt_and_verify(ciphertext, tag)

    return json.loads(plaintext.decode("utf-8"))
