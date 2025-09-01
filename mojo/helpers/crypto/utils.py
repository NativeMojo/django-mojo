from Crypto.Random import get_random_bytes
import string
from base64 import b64encode, b64decode
import json


def generate_key(bit_size=128):
    byte_size = bit_size // 8
    return random_string(byte_size, allow_special=False)


def random_bytes(length):
    return get_random_bytes(length)


def random_string(length, allow_digits=True, allow_chars=True, allow_special=True):
    characters = ''
    if allow_digits:
        characters += string.digits
    if allow_chars:
        characters += string.ascii_letters
    if allow_special:
        characters += string.punctuation

    if characters == '':
        raise ValueError("At least one character set (digits, chars, special) must be allowed")
    random_bytes = get_random_bytes(length)
    return ''.join(characters[b % len(characters)] for b in random_bytes)


def b64_encode(data):
    if isinstance(data, dict):
        data = json.dumps(data)
    return b64encode(data.encode('utf-8')).decode('utf-8')


def b64_decode(data):
    dec = b64decode(data.encode('utf-8')).decode('utf-8')
    if dec[0] == '{':
        return json.loads(dec)
    return dec
