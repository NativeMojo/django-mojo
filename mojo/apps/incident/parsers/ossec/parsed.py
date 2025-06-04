# parsed_alert.py

from objict import objict

class ParsedAlert(objict):
    def normalize_fields(self):
        if self.source_ip in [None, "-"] and hasattr(self, "client"):
            self.source_ip = self.client
        if not hasattr(self, "ext_ip") or self.ext_ip in [None, "-"]:
            self.ext_ip = self.source_ip

    def truncate(self, field, max_len=199):
        value = getattr(self, field, "")
        if isinstance(value, str) and len(value) > max_len:
            value = value[:max_len]
            value = value[:value.rfind(' ')] + "..."
            setattr(self, field, value)

    def truncate_str(self, text, max_len=199):
        if len(text) > max_len:
            text = text[:max_len]
            text = text[:text.rfind(' ')] + "..."
        return text
