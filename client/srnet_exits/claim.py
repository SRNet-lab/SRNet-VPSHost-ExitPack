"""Talk to a seller's claim desk (claimd) over Session Router: ask for a quote, report a payment,
read the status of a quote."""
from __future__ import annotations

import http.client
import json
import os
import re
import urllib.parse

import listing as L  # noqa: E402

MAX_REPLY = 8192
_STATUS_ID = re.compile(r"^[0-9a-f]{16}$")


class ClaimError(RuntimeError):
    pass


def base_url(body: dict, testnets: bool = False) -> str:
    """The seller's claim desk from the signed listing ("http://<name>.sesh/claim").

    For local tests only (test chains on), SRNET_EXITS_TEST_CLAIM_URL may point at a loopback desk."""
    test = os.environ.get("SRNET_EXITS_TEST_CLAIM_URL")
    if test and testnets:
        host = urllib.parse.urlparse(test).hostname
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ClaimError("SRNET_EXITS_TEST_CLAIM_URL must be a loopback URL")
        return test.rstrip("/")
    raw = str((body or {}).get("claim_url") or "")
    if not L.CLAIM_URL_RE.match(raw):
        raise ClaimError("this listing has no usable claim address")
    return raw


def http_transport(url: str, body: bytes | None, timeout: float = 30, dns: str | None = None) -> tuple[int, dict]:
    """POST (body) or GET (None) over plain HTTP; .sesh names resolve via Session Router's DNS."""
    u = urllib.parse.urlparse(url)
    host = u.hostname or ""
    if host.endswith(".sesh"):
        import seshdns
        ip = seshdns.resolve(host, dns)
    elif host in ("127.0.0.1", "::1", "localhost"):
        ip = host
    else:
        raise ClaimError("claims go only to .sesh sellers")
    c = http.client.HTTPConnection(ip, u.port or 80, timeout=timeout)
    try:
        h = {"Host": u.netloc}
        if body is not None:
            h["Content-Type"] = "application/json"
        c.request("POST" if body is not None else "GET", u.path, body=body, headers=h)
        r = c.getresponse()
        data = r.read(MAX_REPLY)
    finally:
        c.close()
    try:
        out = json.loads(data or b"{}")
    except ValueError:
        out = {}
    return r.status, out if isinstance(out, dict) else {}


class Desk:
    def __init__(self, url: str, transport=http_transport, dns: str | None = None, timeout: float = 30):
        self.url, self.transport, self.dns, self.timeout = url.rstrip("/"), transport, dns, timeout

    def _call(self, path: str, body: dict | None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        try:
            return self.transport(self.url + path, data, timeout=self.timeout, dns=self.dns)
        except (OSError, http.client.HTTPException) as e:
            raise ClaimError(f"could not reach the seller over SRNet ({e})") from e

    def quote(self, req: dict) -> dict:
        code, out = self._call("/quote", req)
        if code != 200 or "quote" not in out:
            raise ClaimError(out.get("error") or f"the seller did not give a quote (HTTP {code})")
        return out

    def paid(self, notice: dict) -> tuple[int, dict]:
        code, out = self._call("/paid", notice)
        if code not in (200, 202):
            raise ClaimError(out.get("error") or f"the seller did not take the payment notice (HTTP {code})")
        return code, out

    def status(self, qid: str) -> dict:
        if not _STATUS_ID.match(qid):
            raise ClaimError("bad quote id")
        code, out = self._call(f"/status/{qid}", None)
        if code != 200:
            raise ClaimError(out.get("error") or f"no status from the seller (HTTP {code})")
        return out
