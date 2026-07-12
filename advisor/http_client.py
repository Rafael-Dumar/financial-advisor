from __future__ import annotations

import json
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def fetch_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    observer: Any | None = None,
) -> Any:
    started_at = _now_iso()
    _notify_observer(
        observer,
        "on_request",
        url=_sanitize_observer_url(url),
        method="POST" if payload is not None else "GET",
        started_at=started_at,
        payload=_payload_summary(payload),
        headers=_safe_headers(headers),
    )
    request_headers = {"User-Agent": "financial-advisor-v1"}
    if headers:
        request_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers)
    try:
        with urlopen(request, timeout=20, context=_ssl_context()) as response:
            raw = response.read()
            result = json.loads(raw.decode("utf-8"))
            _notify_observer(
                observer,
                "on_response",
                url=_sanitize_observer_url(url),
                method="POST" if payload is not None else "GET",
                started_at=started_at,
                received_at=_now_iso(),
                http_status=getattr(response, "status", None),
                payload_type=_payload_type(result),
                payload_size_bytes=len(raw),
                payload=_payload_summary(result),
            )
            return result
    except HTTPError as error:
        body = _short_error_body(error)
        suffix = f":{body}" if body else ""
        _notify_observer(
            observer,
            "on_error",
            url=_sanitize_observer_url(url),
            method="POST" if payload is not None else "GET",
            started_at=started_at,
            received_at=_now_iso(),
            http_status=error.code,
            retry_after=error.headers.get("Retry-After") if error.headers else None,
            error=_sanitize_observer_error(f"http_error:{error.code}{suffix}"),
        )
        raise RuntimeError(f"http_error:{error.code}{suffix}") from error
    except URLError as error:
        reason = _short_reason(error.reason)
        if _should_use_windows_tls_fallback(reason):
            return _fetch_json_via_powershell(url, payload=payload, headers=request_headers)
        _notify_observer(
            observer,
            "on_error",
            url=_sanitize_observer_url(url),
            method="POST" if payload is not None else "GET",
            started_at=started_at,
            received_at=_now_iso(),
            http_status=None,
            retry_after=None,
            error=_sanitize_observer_error(f"network_error:{reason}"),
        )
        raise RuntimeError(f"network_error:{reason}") from error
    except TimeoutError as error:
        _notify_observer(
            observer,
            "on_error",
            url=_sanitize_observer_url(url),
            method="POST" if payload is not None else "GET",
            started_at=started_at,
            received_at=_now_iso(),
            http_status=None,
            retry_after=None,
            error="network_error:timeout",
        )
        raise RuntimeError("network_error:timeout") from error


def fetch_text(url: str, *, headers: dict[str, str] | None = None, observer: Any | None = None) -> str:
    started_at = _now_iso()
    _notify_observer(
        observer,
        "on_request",
        url=_sanitize_observer_url(url),
        method="GET",
        started_at=started_at,
        payload=None,
        headers=_safe_headers(headers),
    )
    request_headers = {"User-Agent": "financial-advisor-v1"}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=20, context=_ssl_context()) as response:
            raw = response.read()
            result = raw.decode("utf-8", errors="replace")
            _notify_observer(
                observer,
                "on_response",
                url=_sanitize_observer_url(url),
                method="GET",
                started_at=started_at,
                received_at=_now_iso(),
                http_status=getattr(response, "status", None),
                payload_type="text",
                payload_size_bytes=len(raw),
                payload={"length": len(result)},
            )
            return result
    except HTTPError as error:
        body = _short_error_body(error)
        suffix = f":{body}" if body else ""
        _notify_observer(
            observer,
            "on_error",
            url=_sanitize_observer_url(url),
            method="GET",
            started_at=started_at,
            received_at=_now_iso(),
            http_status=error.code,
            retry_after=error.headers.get("Retry-After") if error.headers else None,
            error=_sanitize_observer_error(f"http_error:{error.code}{suffix}"),
        )
        raise RuntimeError(f"http_error:{error.code}{suffix}") from error
    except URLError as error:
        reason = _short_reason(error.reason)
        if _should_use_windows_tls_fallback(reason):
            return _fetch_text_via_powershell(url, headers=request_headers)
        _notify_observer(
            observer,
            "on_error",
            url=_sanitize_observer_url(url),
            method="GET",
            started_at=started_at,
            received_at=_now_iso(),
            http_status=None,
            retry_after=None,
            error=_sanitize_observer_error(f"network_error:{reason}"),
        )
        raise RuntimeError(f"network_error:{reason}") from error
    except TimeoutError as error:
        _notify_observer(
            observer,
            "on_error",
            url=_sanitize_observer_url(url),
            method="GET",
            started_at=started_at,
            received_at=_now_iso(),
            http_status=None,
            retry_after=None,
            error="network_error:timeout",
        )
        raise RuntimeError("network_error:timeout") from error


def _notify_observer(observer: Any | None, callback_name: str, **metadata: object) -> None:
    callback = getattr(observer, callback_name, None) if observer is not None else None
    if callback is not None:
        callback(**metadata)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _payload_type(payload: Any) -> str:
    if payload is None:
        return "null"
    if isinstance(payload, bool):
        return "boolean"
    if isinstance(payload, (int, float)):
        return "number"
    if isinstance(payload, str):
        return "string"
    if isinstance(payload, list):
        return "list"
    if isinstance(payload, dict):
        return "dict"
    return type(payload).__name__


def _payload_summary(payload: Any) -> dict[str, object] | None:
    if payload is None:
        return None
    if isinstance(payload, dict):
        return {"type": "dict", "keys": sorted(str(key) for key in payload)[:40]}
    if isinstance(payload, list):
        return {"type": "list", "records": len(payload)}
    return {"type": _payload_type(payload)}


def _safe_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    secret_names = ("authorization", "api-key", "apikey", "token", "secret")
    return {
        str(key): "REDACTED" if any(name in str(key).lower() for name in secret_names) else str(value)
        for key, value in headers.items()
    }


def _sanitize_observer_url(url: str) -> str:
    parts = urlsplit(url)
    secret_names = ("apikey", "api_key", "token", "secret", "key")
    query = urlencode(
        [
            (key, "REDACTED" if any(name in key.lower() for name in secret_names) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _sanitize_observer_error(error: str) -> str:
    return _sanitize_observer_url(error).replace("REDACTED", "REDACTED")[:240]


def _short_reason(reason: Any, max_length: int = 120) -> str:
    text = str(reason).replace("\n", " ").strip()
    return text[:max_length] if text else "unavailable"


def _short_error_body(error: HTTPError, max_length: int = 160) -> str:
    try:
        raw = error.read()
    except Exception:
        return ""
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace").replace("\n", " ").strip()
    return text[:max_length]


def _should_use_windows_tls_fallback(reason: str) -> bool:
    return sys.platform == "win32" and "CERTIFICATE_VERIFY_FAILED" in reason


def _fetch_json_via_powershell(
    url: str,
    *,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
) -> Any:
    request_payload = json.dumps(
        {
            "url": url,
            "headers": headers,
            "payload": payload,
        }
    )
    script = r"""
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$headers = @{}
foreach ($property in $request.headers.PSObject.Properties) {
    $headers[$property.Name] = [string]$property.Value
}
$params = @{
    Uri = [string]$request.url
    Headers = $headers
    UseBasicParsing = $true
    TimeoutSec = 20
    Method = 'GET'
}
if ($null -ne $request.payload) {
    $params.Method = 'POST'
    $params.Body = ($request.payload | ConvertTo-Json -Compress)
    $headers['Content-Type'] = 'application/json'
}
$response = Invoke-WebRequest @params
[Console]::Out.Write($response.Content)
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        input=request_payload,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        message = _short_reason(completed.stderr or completed.stdout or "powershell_http_failed")
        raise RuntimeError(f"network_error:powershell_tls_fallback:{message}")
    return json.loads(completed.stdout)


def _fetch_text_via_powershell(url: str, *, headers: dict[str, str]) -> str:
    request_payload = json.dumps({"url": url, "headers": headers})
    script = r"""
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$headers = @{}
foreach ($property in $request.headers.PSObject.Properties) {
    $headers[$property.Name] = [string]$property.Value
}
$response = Invoke-WebRequest -Uri ([string]$request.url) -Headers $headers -UseBasicParsing -TimeoutSec 20
[Console]::Out.Write($response.Content)
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        input=request_payload,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        message = _short_reason(completed.stderr or completed.stdout or "powershell_http_failed")
        raise RuntimeError(f"network_error:powershell_tls_fallback:{message}")
    return completed.stdout


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if sys.platform == "win32" and hasattr(ssl, "enum_certificates"):
        for store_name in ("ROOT", "CA"):
            try:
                certificates = ssl.enum_certificates(store_name)
            except OSError:
                continue
            for certificate, encoding, trust in certificates:
                if encoding != "x509_asn":
                    continue
                if trust is not True and "1.3.6.1.5.5.7.3.1" not in trust:
                    continue
                try:
                    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(certificate))
                except ssl.SSLError:
                    continue
    return context
