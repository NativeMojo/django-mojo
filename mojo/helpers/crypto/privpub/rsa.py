from Crypto import Random
from Crypto.PublicKey import RSA
from Crypto.Cipher import AES, PKCS1_OAEP
import base64
import binascii
import json
from io import BytesIO
from contextlib import closing


class PrivatePublicEncryption:
    def __init__(self, private_key=None, public_key=None, private_key_file=None, public_key_file=None):
        self.private_key = self._load_key(private_key, private_key_file)
        self.public_key = self._load_key(public_key, public_key_file)

    def _load_key(self, key, key_file):
        if key_file:
            with open(key_file, 'r') as f:
                key = f.read()
        if isinstance(key, str):
            return RSA.import_key(key)
        return key

    def generate_public_key(self, make_new=False):
        if self.public_key is None or make_new:
            self.public_key = generate_public_key(self.private_key)
        return self.public_key

    def encrypt(self, data):
        self.generate_public_key()
        return self.encrypt_to_b64(data)

    def decrypt(self, data, as_string=True):
        return self.decrypt_from_b64(data, as_string)

    def encrypt_to_b64(self, data):
        ebytes = encrypt_with_public_key(data, self.public_key)
        return base64.b64encode(ebytes).decode('utf-8')

    def decrypt_from_b64(self, data, as_string=True):
        data = base64.b64decode(data)
        return decrypt_with_private_key(data, self.private_key, as_string)

    def encrypt_to_hex(self, data):
        ebytes = encrypt_with_public_key(data, self.public_key)
        return binascii.hexlify(ebytes).decode('utf-8')

    def decrypt_from_hex(self, data, as_string=True):
        data = binascii.unhexlify(data)
        return decrypt_with_private_key(data, self.private_key, as_string)


def generate_private_key(size=2048):
    return RSA.generate(size)


def generate_public_key(private_key):
    if isinstance(private_key, str):
        private_key = RSA.import_key(private_key)
    return private_key.publickey()


def encrypt_with_public_key(data, public_key):
    if isinstance(public_key, str):
        public_key = RSA.import_key(public_key)

    if isinstance(data, (dict, list)):
        data = json.dumps(data)

    if isinstance(data, str):
        data = data.encode('utf-8')

    session_key = Random.get_random_bytes(16)
    cipher_rsa = PKCS1_OAEP.new(public_key)
    enc_session_key = cipher_rsa.encrypt(session_key)

    cipher_aes = AES.new(session_key, AES.MODE_EAX)
    ciphertext, tag = cipher_aes.encrypt_and_digest(data)

    with closing(BytesIO()) as output:
        for x in (enc_session_key, cipher_aes.nonce, tag, ciphertext):
            output.write(x)
        return output.getvalue()


def decrypt_with_private_key(data, private_key, as_string=True):
    if isinstance(private_key, str):
        private_key = RSA.import_key(private_key)
        if isinstance(data, str):
            data = data.encode('utf-8')
        if isinstance(data, bytes):
            data = BytesIO(data)

    enc_session_key, nonce, tag, ciphertext = (
        data.read(x) for x in (private_key.size_in_bytes(), 16, 16, -1)
    )

    cipher_rsa = PKCS1_OAEP.new(private_key)
    session_key = cipher_rsa.decrypt(enc_session_key)

    cipher_aes = AES.new(session_key, AES.MODE_EAX, nonce)
    decrypted_data = cipher_aes.decrypt_and_verify(ciphertext, tag)

    return decrypted_data.decode('utf-8') if as_string else decrypted_data
