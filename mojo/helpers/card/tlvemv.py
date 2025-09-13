import binascii
from .emvtags import TAG_NAMES, NAME_TAGS, PLAIN_TEXT_TAGS

"""
Example Usage:
---------------
To parse a TLV-encoded string:

    tlv_string = "9F02060000000001009F0306000000000000"
    tlv_obj = TLVEMV(tlv_string)
    print(tlv_obj)  # Outputs the parsed TLV data as a dictionary

To encode the TLV data back to a string:

    encoded_tlv = tlv_obj.encode_tlv()
    print(encoded_tlv)  # Outputs the re-encoded TLV string

"""


class TLVEMV(dict):
    def __init__(self, tlv_data=None):
        super().__init__()
        if isinstance(tlv_data, dict):
            for key, value in tlv_data.items():
                key = NAME_TAGS.get(key, key)
                self[key] = value
        elif tlv_data:
            self.parse_tlv(tlv_data)

    def parse_tlv(self, tlv_data):
        """Parses TLV-encoded data and stores it in the dictionary."""
        data = binascii.unhexlify(tlv_data) if isinstance(tlv_data, str) else tlv_data
        i = 0
        while i < len(data):
            try:
                tag = data[i]
                i += 1
                if tag & 0x1F == 0x1F:  # Multi-byte tag
                    if i >= len(data):
                        raise ValueError("Incomplete multi-byte tag")
                    tag = (tag << 8) | data[i]
                    i += 1

                if i >= len(data):
                    raise ValueError("Missing length byte")
                length = data[i]
                i += 1

                if i + length > len(data):
                    raise ValueError("Declared length exceeds available data")
                value = data[i:i+length]
                i += length

                tag_hex = f'{tag:X}'
                self[tag_hex] = value.hex().upper()
            except (IndexError, ValueError) as e:
                print(f"Error parsing TLV data at position {i}: {e}")
                raise  # Re-raise the exception for external handling

    def get(self, key, default=None):
        """Overrides get to check both human-readable and hex keys."""
        key = NAME_TAGS.get(key, key)  # Convert human-readable to hex if available
        return super().get(key.upper(), default)

    def __getitem__(self, key):
        """Overrides __getitem__ to check both human-readable and hex keys."""
        if key not in self:
            key = NAME_TAGS.get(key, key)  # Convert human-readable to hex if available
            if key not in self:
                return None
        return super().__getitem__(key.upper())

    def encode_tlv(self):
        """Encodes the dictionary back into TLV format."""
        encoded_data = bytearray()
        for tag, value in self.items():
            try:
                if tag in NAME_TAGS:
                    tag = NAME_TAGS[tag]
                tag_bytes = binascii.unhexlify(tag.zfill(len(tag) + len(tag) % 2))
                value_bytes = binascii.unhexlify(value)
                encoded_data.extend(tag_bytes)
                encoded_data.append(len(value_bytes))
                encoded_data.extend(value_bytes)
            except (binascii.Error, ValueError) as e:
                print(f"Error encoding tag {tag}: {e}")
                continue  # Skip invalid entries gracefully
        return encoded_data.hex().upper()

    def to_dict(self, decode_hex=True, exclude=None, include=None):
        """Transforms the TLV data into a human-readable dictionary using TAG_NAMES."""
        human_readable = {}
        for tag, value in self.items():
            tag_name = TAG_NAMES.get(tag, tag)
            if exclude and tag in exclude:
                continue
            if include and tag not in include:
                continue
            if decode_hex and tag in PLAIN_TEXT_TAGS:
                value = bytes.fromhex(value).decode("utf-8")
            human_readable[tag_name] = value
        return human_readable

    def __setitem__(self, key, value):
        """Ensures values are stored as uppercase hex strings."""
        if isinstance(value, bytes):
            value = value.hex().upper()
        if not all(c in '0123456789ABCDEFabcdef' for c in value):
            raise ValueError(f"Invalid hex value: {value}")
        super().__setitem__(key.upper(), value.upper())
