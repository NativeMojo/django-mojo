import re
from objict import objict
from .tlvemv import TLVEMV
from datetime import datetime

# Constants for card brands
BRANDS = {
    'visa': re.compile(r'^4\d{12}(\d{3})?$'),
    'mastercard': re.compile(r"^5[1-5][0-9]{14}|^(222[1-9]|22[3-9][0-9]|2[3-6][0-9]{2}|27[0-1][0-9]|2720)[0-9]{12}$"),
    'amex': re.compile(r'^3[47]\d{13}$'),
    'jcb': re.compile(r'^35[2-8]\d{13}$'),
    'discover': re.compile(r'^(6011|65\d{2})\d{12}$'),
    'diners': re.compile(r'^(30[0-5]|309|54|55|36|38|39)\d{10,12}$'),
    'maestro': re.compile(r'^(50|[56][6-9])\d{10,17}$'),
    'gas': re.compile(r'^7\d{12}(\d{3})?$'),
    'instapayment': re.compile(r'^63[7-9]\d{13}$'),
    'interpayment': re.compile(r'^636\d{13}$'),
    'uatp': re.compile(r'^1\d{14}$')
}

FRIENDLY_BRANDS = {
    'visa': 'Visa',
    'mastercard': 'MasterCard',
    'amex': 'American Express',
    'discover': 'Discover',
    'jcb': "JCB",
    'diners': "Diners Club",
    'gas': "Gas Card"
}

def parse_pan(track):
    for info in (parse_track1(track), parse_track2(track)):
        if info and info.pan:
            return info.pan
    return ""

def parse_track1(track):
    output = objict(raw_track1=track, track1=parse_raw_track1(track))
    if output.track1 and output.track1[0].upper() == "B":
        fields = output.track1.split("^")
        output.pan = fields[0][1:].replace(" ", "")
        output.name = fields[1].strip() if len(fields) > 1 else ""
        if "/" in output.name:
            names = output.name.split("/")
            output.lastname, output.firstname = names[0].strip().title(), names[-1].strip().title()
            output.name = f"{output.firstname} {output.lastname}"
        if len(fields) > 2:
            output.expires = parse_expiry(fields[2][:4])
            output.service_code = fields[2][4:7] if len(fields[2]) > 7 else ""
    return output

def parse_track2(track):
    output = objict(raw_track2=track, track2=parse_raw_track2(track))
    if output.track2 and "=" in output.track2:
        output.pan, extra = output.track2.split("=")
        output.expires = parse_expiry(extra[:4])
        output.service_code = extra[4:7] if len(extra) > 7 else ""
    return output

def parse_raw_track1(track):
    return track.split("%")[-1].split("?")[0] if track else None

def parse_raw_track2(track):
    track = track.split(";")[-1].split("?")[0] if track else None
    return track[:-1] if track and track[-1].upper() == "F" else track

def parse_expiry(date_str):
    if date_str.isdigit() and len(date_str) == 4:
        exp = objict(year=2000 + int(date_str[:2]), month=int(date_str[2:4]), is_valid=False)
        exp.is_valid = exp.month >= 1 and exp.month <= 12
        return exp
    return objict(year=1970, month=1, is_valid=False)

def split_tracks(track):
    track1 = parse_raw_track1(track) if "%" in track else None
    track2 = parse_raw_track2(track) if ";" in track else None
    return track1, track2


# def split_tracks(track):
#     return (parse_raw_track1(track), None) if "^" in track else (None, parse_raw_track2(track)) if "=" in track else (None, None)

def get_brand_slug(pan):
    return next((brand for brand, pattern in BRANDS.items() if pattern.match(pan)), 'unknown')

def parse_emv(data):
    tlv = TLVEMV(data)
    return tlv, tlv.to_dict()


def get_card_from_emv(emv_data):
    card = parse_emv_track2(emv_data)
    if card.pan:
        card.iin = card.pan[:6]
        card.last_4 = card.pan[-4:]
    card.name = get_emv_cardholder_name(emv_data)
    card.app_label = get_emv_app_label(emv_data)
    card.emv_data = emv_data
    return card


def encode_emv(data):
    tlv = TLVEMV(data)
    return tlv.encode_tlv()

def get_emv_track_2(emv_data):
    t2 = emv_data.get("57", "").replace("D", "=")
    if t2[-1].upper() == "F":
        t2 = t2[:-1]
    return t2

def parse_emv_track2(emv_data):
    return parse_track2(get_emv_track_2(emv_data))

def get_emv_pan(emv_data):
    return emv_data.get("5A", None)

def get_emv_cardholder_name(emv_data):
    # Decode the hex-encoded cardholder name
    encoded_name = emv_data.get("5F20", "")
    return bytes.fromhex(encoded_name).decode('utf-8') if encoded_name else ""

def get_emv_app_label(emv_data):
    # Decode the hex-encoded application label
    encoded_label = emv_data.get("50", "")
    return bytes.fromhex(encoded_label).decode('utf-8') if encoded_label else ""

def get_emv_card_expiration(emv_data):
    expiration_str = emv_data.get("5F24", "")
    if len(expiration_str) == 4 and expiration_str.isdigit():
        return datetime.strptime(expiration_str, "%y%m").date()
    return None

def scrub_emv(emv, protected_fields=set()):
    return {k: v for k, v in emv.items() if k not in protected_fields}

def luhn_check(pan):
    pan = str(pan).replace(' ', '')
    if not pan.isdigit():
        return False
    digits = list(map(int, pan))
    total = sum(digits[-1::-2]) + sum(sum(divmod(2 * d, 10)) for d in digits[-2::-2])
    return total % 10 == 0
