from Crypto.Random import get_random_bytes
import string


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
