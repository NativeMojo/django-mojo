# aws_tools/godaddy.py

import requests
from objict import objict
BASE_URL = "https://api.godaddy.com/v1"

class DNSManager:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret

    def _headers(self):
        return {
            "Authorization": f"sso-key {self.api_key}:{self.api_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def get_domains(self, status="ACTIVE"):
        url = f"{BASE_URL}/domains"
        params = {"statuses": status} if status else {}
        resp = requests.get(url, headers=self._headers(), params=params)
        # resp.raise_for_status()
        return objict(resp.json())

    def get_domain_info(self, domain):
        url = f"{BASE_URL}/domains/{domain}"
        resp = requests.get(url, headers=self._headers())
        # resp.raise_for_status()
        return objict(resp.json())

    def is_domain_active(self, domain):
        info = self.get_domain_info(domain)
        return info.status == "ACTIVE"

    def get_record(self, domain, record_type, name):
        url = f"{BASE_URL}/domains/{domain}/records/{record_type}/{name}"
        resp = requests.get(url, headers=self._headers())
        # resp.raise_for_status()
        return objict(resp.json())

    def get_records(self, domain):
        url = f"{BASE_URL}/domains/{domain}/records"
        resp = requests.get(url, headers=self._headers())
        # resp.raise_for_status()
        return objict(resp.json())

    def edit_record(self, domain, record_type, name, data, ttl):
        url = f"{BASE_URL}/domains/{domain}/records/{record_type}/{name}"
        payload = [{"data": data, "ttl": ttl}]
        resp = requests.put(url, headers=self._headers(), json=payload)
        # resp.raise_for_status()
        return objict(resp.json()) if resp.content else {"status": "success"}

    def add_record(self, domain, record_type, name, data, ttl):
        return self.edit_record(domain, record_type, name, data, ttl)

    def bulk_add_records(self, domain, records):
        url = f"{BASE_URL}/domains/{domain}/records"
        resp = requests.patch(url, headers=self._headers(), json=records)
        # resp.raise_for_status()
        return objict(resp.json()) if resp.content else {"status": "success"}
