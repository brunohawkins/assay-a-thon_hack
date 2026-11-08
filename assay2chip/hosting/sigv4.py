"""AWS Signature Version 4 for S3-compatible object stores, standard library only.

Used by storage.S3Store, so moving artefacts from a local directory to any S3-compatible
store (AWS S3, MinIO, OCI Object Storage's S3 compatibility API, Cloudflare R2) is a
configuration change, with no new dependency. Checked against the worked examples in the
AWS S3 documentation (tests/test_deploy_storage.py).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Dict, Mapping, Optional, Tuple
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED = "UNSIGNED-PAYLOAD"


def uri_encode(text: str, keep_slash: bool) -> str:
    return quote(text, safe="-_.~" + ("/" if keep_slash else ""))


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str = "s3") -> bytes:
    k = _hmac(("AWS4" + secret).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def _canonical_query(query: Mapping[str, str]) -> str:
    pairs = sorted((uri_encode(str(k), False), uri_encode(str(v), False))
                   for k, v in query.items())
    return "&".join("%s=%s" % kv for kv in pairs)


def _stamp(now: Optional[datetime]) -> Tuple[str, str]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")


def _sign(method, path, query, headers, payload_hash, region, access_key, secret_key,
          amz_date, date, service):
    lowered = {k.lower().strip(): " ".join(str(v).strip().split()) for k, v in headers.items()}
    names = sorted(lowered)
    canonical_headers = "".join("%s:%s\n" % (n, lowered[n]) for n in names)
    signed = ";".join(names)
    canonical = "\n".join([method.upper(), uri_encode(path, True), _canonical_query(query),
                           canonical_headers, signed, payload_hash])
    scope = "%s/%s/%s/aws4_request" % (date, region, service)
    to_sign = "\n".join([ALGORITHM, amz_date, scope,
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(secret_key, date, region, service),
                         to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return signature, signed, scope


def sign_headers(method: str, host: str, path: str, *, region: str, access_key: str,
                 secret_key: str, query: Optional[Mapping[str, str]] = None,
                 headers: Optional[Mapping[str, str]] = None,
                 payload_hash: str = EMPTY_SHA256, now: Optional[datetime] = None,
                 service: str = "s3") -> Dict[str, str]:
    """The headers to send (Authorization, x-amz-date, x-amz-content-sha256, host...)."""
    amz_date, date = _stamp(now)
    out = dict(headers or {})
    out["host"] = host
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_hash
    signature, signed, scope = _sign(method, path, query or {}, out, payload_hash, region,
                                     access_key, secret_key, amz_date, date, service)
    out["Authorization"] = "%s Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (
        ALGORITHM, access_key, scope, signed, signature)
    return out


def presign(method: str, host: str, path: str, *, region: str, access_key: str,
            secret_key: str, expires_s: int = 3600, now: Optional[datetime] = None,
            query: Optional[Mapping[str, str]] = None, service: str = "s3") -> str:
    """The query string of a presigned URL (everything after `?`)."""
    amz_date, date = _stamp(now)
    q = dict(query or {})
    q.update({
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": "%s/%s/%s/%s/aws4_request" % (access_key, date, region, service),
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires_s)),
        "X-Amz-SignedHeaders": "host",
    })
    signature, _, _ = _sign(method, path, q, {"host": host}, UNSIGNED, region, access_key,
                            secret_key, amz_date, date, service)
    q["X-Amz-Signature"] = signature
    return _canonical_query(q)
