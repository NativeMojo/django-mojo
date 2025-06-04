from mojo.helpers import paths
import mojo.errors
from objict import objict

cache = objict()

def get_global_encrypter():
    return get_privpub("global_encrypter.pub")

def get_global_decrypter():
    return get_privpub("global_decrypter.priv")


def get_privpub(key_file_name):
    name, ext = key_file_name.split(".")
    if cache.get(name) is None:
        key_file = paths.VAR_ROOT / "keys" / f"{key_file_name}"
        if not key_file.exists():
            raise mojo.errors.ValueException(f"missing var/keys/{key_file_name}")
        from mojo.helpers.crypto.privpub import PrivatePublicEncryption
        if ext == "pub":
            cache[name] = PrivatePublicEncryption(public_key_file=key_file)
        else:
            cache[name] = PrivatePublicEncryption(private_key_file=key_file)
    return cache[name]
