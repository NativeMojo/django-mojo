from base64 import b64encode, b64decode
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from Crypto.Hash import SHA256
import hashlib
import hmac
from objict import objict
import json


def encrypt(data, key):
    if isinstance(data, dict):
        data = json.dumps(data)
    if not isinstance(data, str):
        raise ValueError("Data must be a string or a dictionary")

    data = data.encode('utf-8')

    # Hash the key to ensure it's 32 bytes long (256 bits)
    hashed_key = SHA256.new(key.encode('utf-8')).digest()

    # Create a new AES cipher with the hashed key and a random IV
    cipher = AES.new(hashed_key, AES.MODE_CBC)
    iv = cipher.iv

    # Pad the data to make it a multiple of the AES block size
    padded_data = pad(data, AES.block_size)

    # Encrypt the padded data
    encrypted_data = cipher.encrypt(padded_data)

    # Encode the IV and encrypted data as Base64 and concatenate them
    encrypted_data_b64 = b64encode(iv + encrypted_data).decode('utf-8')

    return encrypted_data_b64

def decrypt(enc_data_b64, key):
    # Decode the base64 data to retrieve the bytes
    enc_data_bytes = b64decode(enc_data_b64)

    # Extract the IV and the encrypted data
    iv = enc_data_bytes[:AES.block_size]
    encrypted_data = enc_data_bytes[AES.block_size:]

    # Hash the key to ensure it's 32 bytes long (256 bits)
    hashed_key = SHA256.new(key.encode('utf-8')).digest()

    # Create a new AES cipher with the key and the extracted IV
    cipher = AES.new(hashed_key, AES.MODE_CBC, iv)

    # Decrypt the data
    decrypted_padded_data = cipher.decrypt(encrypted_data)

    # Unpad the decrypted data
    decrypted_data = unpad(decrypted_padded_data, AES.block_size)

    # Try to decode the decrypted data as UTF-8
    decrypted_data_str = decrypted_data.decode('utf-8')
    try:
        return objict.from_json(decrypted_data_str)
    except Exception:
        return decrypted_data_str


def hash_to_hex(input_string):
    if not isinstance(input_string, str):
        raise ValueError("Input must be a string")
    # Create a new SHA-256 hasher
    hasher = hashlib.sha256()
    # Update the hasher with the input string encoded to bytes
    hasher.update(input_string.encode('utf-8'))
    # Return the hexadecimal representation of the hash
    return hasher.hexdigest()


def derive_salt(digits, secret_key):
    """Derives a salt from the last 8 digits of the DIGITs using HMAC."""
    last_8_digits = digits[-8:]
    if isinstance(secret_key, str):  # Ensure secret_key is bytes
        secret_key = secret_key.encode()
    return hmac.new(secret_key, last_8_digits.encode(), hashlib.sha256).digest()[:16]  # Use first 16 bytes

def hash_digits(digits, secret_key):
    """Hashes the PAN using a derived salt without storing it."""
    salt = derive_salt(digits, secret_key)
    hash_obj = hashlib.sha256(salt + digits.encode())
    return hash_obj.hexdigest()
