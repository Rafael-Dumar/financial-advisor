from __future__ import annotations

import json
import ssl
import subprocess
import sys
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def fetch_json(url: str, *, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
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
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = _short_error_body(error)
        suffix = f":{body}" if body else ""
        raise RuntimeError(f"http_error:{error.code}{suffix}") from error
    except URLError as error:
        reason = _short_reason(error.reason)
        if _should_use_windows_tls_fallback(reason):
            return _fetch_json_via_powershell(url, payload=payload, headers=request_headers)
        raise RuntimeError(f"network_error:{reason}") from error
    except TimeoutError as error:
        raise RuntimeError("network_error:timeout") from error


def fetch_text(url: str, *, headers: dict[str, str] | None = None) -> str:
    request_headers = {"User-Agent": "financial-advisor-v1"}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=20, context=_ssl_context()) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        body = _short_error_body(error)
        suffix = f":{body}" if body else ""
        raise RuntimeError(f"http_error:{error.code}{suffix}") from error
    except URLError as error:
        reason = _short_reason(error.reason)
        if _should_use_windows_tls_fallback(reason):
            return _fetch_text_via_powershell(url, headers=request_headers)
        raise RuntimeError(f"network_error:{reason}") from error
    except TimeoutError as error:
        raise RuntimeError("network_error:timeout") from error


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
