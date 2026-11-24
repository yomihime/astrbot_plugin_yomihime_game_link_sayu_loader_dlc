"""Exact public GET resources, shared by the sealed worker and its module."""

import ipaddress
import json
from urllib.parse import urlencode

# Each key selects one complete request. No input from a chat or from the
# worker is interpolated into a host, path, query, header or body.
PROFILE_MODULES = {"waves": "sayuww"}
SOURCES = {
    "sayuww": {
        "sayu_kuro_ip": ("event.kurobbs.com", "/event/ip", (), "ip_text"),
        "sayu_ipify": ("api.ipify.org", "/", (("format", "json"),), "ip_json"),
        "sayu_httpbin": ("httpbin.org", "/ip", (), "origin_json"),
    }
}
MAX_BODY = 4096


def target(spec):
    domain, path, query, _shape = spec
    return "https://" + domain + path + ("?" + urlencode(query) if query else "")


def valid_body(shape, body):
    """Validate the declared representation without rewriting upstream bytes."""
    try:
        text = body.decode("ascii")
        if shape == "ip_text":
            values = [text]
        elif shape in ("ip_json", "origin_json"):
            key = "ip" if shape == "ip_json" else "origin"
            pairs = json.loads(text, object_pairs_hook=tuple)
            if not isinstance(pairs, tuple) or len(pairs) != 1 or pairs[0][0] != key:
                return False
            value = pairs[0][1]
            if not isinstance(value, str):
                return False
            values = value.split(",") if shape == "origin_json" else [value]
        else:
            return False
        if len(values) > 8 or any("%" in value for value in values):
            return False
        for value in values:
            ipaddress.ip_address(value.strip(" \t\r\n"))
        return True
    except (ValueError, RecursionError):
        return False
