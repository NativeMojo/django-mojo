import base64
import binascii
import json
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.encoding import Base64Encoder
from nacl.exceptions import CryptoError


class PrivatePublicEncryption:
    def __init__(self, private_key=None, public_key=None, private_key_file=None, public_key_file=None):
        self.private_key = self._load_key(private_key, private_key_file, is_private=True)
        self.public_key = self._load_key(public_key, public_key_file, is_private=False)

        if self.private_key and not self.public_key:
            self.public_key = self.private_key.public_key

    def _load_key(self, key, key_file, is_private):
        if key_file:
            with open(key_file, 'r') as f:
                key = f.read().strip()

        if key:
            if isinstance(key, str):
                key_bytes = Base64Encoder.decode(key)
            elif isinstance(key, bytes):
                key_bytes = key
            else:
                raise ValueError("Key must be a base64 string or bytes")

            return PrivateKey(key_bytes) if is_private else PublicKey(key_bytes)
        return None

    def generate_public_key(self, make_new=False):
        if self.public_key is None or make_new:
            if not self.private_key:
                self.private_key = PrivateKey.generate()
            self.public_key = self.private_key.public_key
        return self.public_key

    def encrypt(self, data):
        self.generate_public_key()
        return self.encrypt_to_b64(data)

    def decrypt(self, data, as_string=True):
        return self.decrypt_from_b64(data, as_string)

    def encrypt_to_b64(self, data):
        encrypted_bytes = encrypt_with_public_key(data, self.public_key)
        return base64.b64encode(encrypted_bytes).decode('utf-8')

    def decrypt_from_b64(self, data, as_string=True):
        decoded = base64.b64decode(data)
        return decrypt_with_private_key(decoded, self.private_key, as_string)

    def encrypt_to_hex(self, data):
        encrypted_bytes = encrypt_with_public_key(data, self.public_key)
        return binascii.hexlify(encrypted_bytes).decode('utf-8')

    def decrypt_from_hex(self, data, as_string=True):
        decoded = binascii.unhexlify(data)
        return decrypt_with_private_key(decoded, self.private_key, as_string)


def generate_private_key():
    return PrivateKey.generate()


def generate_public_key(private_key):
    if isinstance(private_key, str):
        private_key = PrivateKey(Base64Encoder.decode(private_key))
    return private_key.public_key


def encrypt_with_public_key(data, public_key):
    if isinstance(public_key, str):
        public_key = PublicKey(Base64Encoder.decode(public_key))

    if isinstance(data, (dict, list)):
        data = json.dumps(data)
    if isinstance(data, str):
        data = data.encode('utf-8')

    sealed_box = SealedBox(public_key)
    return sealed_box.encrypt(data)


def decrypt_with_private_key(data, private_key, as_string=True):
    if isinstance(private_key, str):
        private_key = PrivateKey(Base64Encoder.decode(private_key))

    sealed_box = SealedBox(private_key)
    try:
        decrypted = sealed_box.decrypt(data)
        decoded = decrypted.decode('utf-8')
        return json.loads(decoded) if as_string else decrypted
    except (CryptoError, json.JSONDecodeError):
        return decrypted.decode('utf-8') if as_string else decrypted
