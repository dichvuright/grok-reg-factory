# -*- coding: utf-8 -*-
"""Small, dependency-light helpers used by the Kiro registration protocol."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import random
import re
import struct
import time
import uuid
from collections import OrderedDict

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_DELTA = 0x9E3779B9
_FALLBACK_KEY = (1888420705, 2576816180, 2347232058, 874813317)
_IDENTIFIER = "ECdITeCs"
_TES_VERSION = "4.0.0"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _uuid() -> str:
    return str(uuid.uuid4())


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _xxtea_encrypt(text: str, key: tuple[int, int, int, int]) -> bytes:
    raw = text.encode("utf-8")
    n = (len(raw) + 3) // 4
    if not n:
        return b""
    values = []
    for offset in range(0, len(raw), 4):
        part = raw[offset:offset + 4].ljust(4, b"\0")
        values.append(int.from_bytes(part, "little"))
    rounds = 6 + 52 // n
    total = 0
    z = values[-1]
    for _ in range(rounds):
        total = _u32(total + _DELTA)
        e = (total >> 2) & 3
        for p in range(n):
            y = values[(p + 1) % n]
            mx = (((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4)) ^
                  ((total ^ y) + (key[(p & 3) ^ e] ^ z)))
            values[p] = _u32(values[p] + mx)
            z = values[p]
    return b"".join(value.to_bytes(4, "little") for value in values)


def _xxtea_decrypt(data: bytes, key: tuple[int, int, int, int]) -> str:
    n = len(data) // 4
    if n < 2:
        return ""
    values = [int.from_bytes(data[offset:offset + 4], "little") for offset in range(0, len(data), 4)]
    rounds = 6 + 52 // n
    total = _u32(rounds * _DELTA)
    y = values[0]
    for _ in range(rounds):
        e = (total >> 2) & 3
        for p in range(n - 1, -1, -1):
            z = values[(p - 1) % n]
            mx = (((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4)) ^
                  ((total ^ y) + (key[(p & 3) ^ e] ^ z)))
            values[p] = _u32(values[p] - mx)
            y = values[p]
        total = _u32(total - _DELTA)
    return b"".join(value.to_bytes(4, "little") for value in values).rstrip(b"\0").decode("utf-8", "replace")


class FingerprintBuilder:
    """Builds the encrypted telemetry envelope expected by signin.aws."""

    def __init__(self):
        self.ua_version = str(random.randint(131, 144))
        self.ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{self.ua_version}.0.0.0 Safari/537.36"
        )
        self.sec_ua = f'"Not/A)Brand";v="24", "Chromium";v="{self.ua_version}", "Google Chrome";v="{self.ua_version}"'
        self.canvas = 5000 + random.randint(0, 100000)
        self.bins = [0] * 256
        self.bins[0] = 9000 + random.randint(0, 4000)
        self.bins[255] = 11000 + random.randint(0, 4000)
        self.bins[102] = 500 + random.randint(0, 180)
        self.bins[153] = 400 + random.randint(0, 250)
        remaining = max(0, 36000 - sum(self.bins))
        for index in range(1, 255):
            if self.bins[index] == 0:
                value = min(remaining, random.randint(2, 18))
                self.bins[index] = value
                remaining -= value
        self.bins[0] += remaining
        self.signin_ubid = f"X{random.choice((10, 19, 42, 55, 73, 81, 96))}-{random.randrange(10**7):07d}-{random.randrange(10**7):07d}:{int(time.time())}"
        self.profile_ubid = ""
        self.start_ms = None
        self.perf = None
        self.webpack_hash = f"{random.getrandbits(64):x}"[:10]
        self.key = _FALLBACK_KEY
        self.identifier = _IDENTIFIER
        self.version = _TES_VERSION

    def update_app_js(self, text: str) -> None:
        match = re.search(r"var\s+\w+\s*=\s*\[(\d+),\s*[\"']([A-Za-z0-9]+)[\"'],\s*(\d+),\s*(\d+),\s*(\d+)\]", text or "")
        if match:
            values = [int(match.group(index)) for index in (1, 3, 4, 5)]
            self.key = (values[2], values[0], values[3], values[1])
            self.identifier = match.group(2)
        version = re.search(r"FWCIM_VERSION\s*=\s*[\"'](\d+\.\d+\.\d+)[\"']", text or "")
        if version:
            self.version = version.group(1)

    def _performance(self, now_ms: int) -> dict[str, int]:
        if self.perf is not None:
            return self.perf
        load_end = now_ms - random.randint(500, 1500)
        base = load_end - random.randint(2000, 4000)
        connect = base + random.randint(300, 600)
        response = connect + random.randint(200, 600)
        interactive = load_end - random.randint(5, 15)
        self.perf = {
            "connectStart": base + 5, "secureConnectionStart": base + 8,
            "unloadEventEnd": 0, "domainLookupStart": base + 3,
            "domainLookupEnd": base + 4, "responseStart": response,
            "connectEnd": connect, "responseEnd": response + 2,
            "requestStart": connect, "domLoading": response + 3,
            "redirectStart": 0, "loadEventEnd": load_end,
            "domComplete": load_end, "navigationStart": base,
            "loadEventStart": load_end, "domContentLoadedEventEnd": load_end,
            "unloadEventStart": 0, "redirectEnd": 0,
            "domInteractive": interactive, "fetchStart": base + 3,
            "domContentLoadedEventStart": interactive + 1,
        }
        return self.perf

    @staticmethod
    def _interaction(event_type: str) -> dict:
        if event_type in {"PageLoad", "first_load"}:
            return {"clicks": 0, "touches": 0, "keyPresses": 0, "cuts": 0,
                    "copies": 0, "pastes": 0, "keyPressTimeIntervals": [],
                    "mouseClickPositions": [], "keyCycles": [], "mouseCycles": [],
                    "touchCycles": []}
        keys = random.randint(4, 20)
        return {"clicks": random.randint(1, 5), "touches": 0, "keyPresses": keys,
                "cuts": 0, "copies": 0, "pastes": 0,
                "keyPressTimeIntervals": [random.randint(30, 900) for _ in range(max(1, keys // 3))],
                "mouseClickPositions": [f"{random.randint(50, 1500)},{random.randint(50, 800)}" for _ in range(2)],
                "keyCycles": [random.randint(10, 700) for _ in range(max(2, keys // 2))],
                "mouseCycles": [random.randint(20, 300) for _ in range(2)], "touchCycles": []}

    def encrypted(self, location: str, referrer: str, page_type: str, event_type: str,
                  time_on_page: int = 0, email: str = "") -> str:
        now_ms = int(time.time() * 1000)
        perf = self._performance(now_ms)
        if page_type == "profile" and not self.profile_ubid:
            self.profile_ubid = f"X{random.choice((10, 19, 42, 55, 73, 81, 96))}-{random.randrange(10**7):07d}-{random.randrange(10**7):07d}:{perf['loadEventEnd'] // 1000}"
        if self.start_ms is None:
            self.start_ms = now_ms - random.randint(400, 900)
        start = now_ms - time_on_page if time_on_page else self.start_ms
        dynamic = [f"/dist/main/app_{self.webpack_hash}.min.js"] if page_type == "profile" else ["/assets/js/app.js"]
        data = OrderedDict([
            ("metrics", {name: (random.randint(0, 3) if name == "perf" else 0) for name in
                         ("el", "script", "h", "batt", "perf", "auto", "tz", "fp2", "lsubid", "browser", "capabilities", "gpu", "dnt", "math", "tts", "input", "canvas", "captchainput", "pow")} ),
            ("start", start), ("interaction", self._interaction(event_type)),
            ("scripts", {"dynamicUrls": dynamic, "inlineHashes": [], "elapsed": 0,
                          "dynamicUrlCount": len(dynamic), "inlineHashesCount": 0}),
            ("history", {"length": 3 if page_type == "profile" else 1}),
            ("battery", {}), ("performance", {"timing": perf}),
            ("automation", {"wd": {"properties": {"document": [], "window": [], "navigator": []}},
                             "phantom": {"properties": {"window": []}}}),
            ("end", now_ms + random.randint(0, 50)), ("timeZone", 8), ("flashVersion", None),
            ("plugins", "PDF Viewer Chrome PDF Viewer ||1920-1080-1032-24-*-*-*"),
            ("dupedPlugins", "PDF Viewer Chrome PDF Viewer ||1920-1080-1032-24-*-*-*"),
            ("screenInfo", "1920-1080-1032-24-*-*-*"),
            ("lsUbid", self.profile_ubid if page_type == "profile" else self.signin_ubid),
            ("referrer", referrer), ("userAgent", self.ua), ("deviceMemory", 8),
            ("hardwareConcurrency", 8), ("platform", "Win32"), ("location", location),
            ("webDriver", False),
            ("capabilities", {"css": {"textShadow": 1, "WebkitTextStroke": 1, "boxShadow": 1,
                                       "borderRadius": 1, "borderImage": 1, "opacity": 1,
                                       "transform": 1, "transition": 1},
                               "js": {"audio": True, "geolocation": True, "localStorage": "supported",
                                      "touch": False, "video": True, "webWorker": True}, "elapsed": 0}),
            ("gpu", {"vendor": "Google Inc. (Intel)", "model": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)", "extensions": []}),
            ("dnt", None), ("math", {"tan": "-1.4214488238747245", "sin": "0.8178819121159085", "cos": "-0.5753861119575491"}),
        ])
        if page_type == "profile":
            data["timeToSubmit"] = max(1, time_on_page or random.randint(1000, 4000))
        checksum = f"{binascii.crc32(email.encode('utf-8')) & 0xFFFFFFFF:08X}" if email else ""
        data["form"] = {} if not email else {f"formField29-{now_ms}-{random.randint(1000, 9999)}": {
            "clicks": 1, "touches": 0, "keyPresses": max(3, len(email) // 2), "cuts": 0,
            "copies": 0, "pastes": 0, "keyPressTimeIntervals": [80, 120],
            "mouseClickPositions": ["120.5,20.5"], "keyCycles": [100, 120],
            "mouseCycles": [90], "touchCycles": [], "width": 180, "height": 32,
            "totalFocusTime": 0, "checksum": checksum, "autocomplete": False, "prefilled": False}}
        data.update({"canvas": {"hash": self.canvas, "emailHash": None, "histogramBins": self.bins},
                     "token": {"isCompatible": page_type in {"profile", "signup"}, "pageHasCaptcha": 0},
                     "auth": {"form": {"method": "get"}}, "errors": [], "version": self.version})
        plain = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        crc = binascii.crc32(plain.encode("utf-8")) & 0xFFFFFFFF
        encrypted = _xxtea_encrypt(f"{crc:08X}#{plain}", self.key)
        return f"{self.identifier}:{base64.b64encode(encrypted).decode('ascii')}"


def encrypt_password(password: str, jwk: dict, issuer: str = "signin",
                     audience: str = "AWSPasswordService", region: str = "us-east-1") -> str:
    """Encrypt an AWS signin password using the JWE compact serialization."""
    n = int.from_bytes(base64.urlsafe_b64decode(str(jwk["n"]) + "=" * (-len(str(jwk["n"])) % 4)), "big")
    e = int.from_bytes(base64.urlsafe_b64decode(str(jwk["e"]) + "=" * (-len(str(jwk["e"])) % 4)), "big")
    public = rsa.RSAPublicNumbers(e, n).public_key()
    header = {"alg": "RSA-OAEP-256", "kid": jwk.get("kid", ""), "enc": "A256GCM",
              "cty": "enc", "typ": "application/aws+signin+jwe"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    cek = os.urandom(32)
    encrypted_cek = public.encrypt(cek, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                                                      algorithm=hashes.SHA256(), label=None))
    now = int(time.time())
    claims = {"iss": f"{region}.{issuer}", "iat": now, "nbf": now, "jti": _uuid(),
              "exp": now + 300, "aud": f"{region}.{audience}", "password": password}
    iv = os.urandom(12)
    encrypted = AESGCM(cek).encrypt(iv, json.dumps(claims, separators=(",", ":")).encode("utf-8"), header_b64.encode("ascii"))
    return ".".join((header_b64, _b64url(encrypted_cek), _b64url(iv), _b64url(encrypted[:-16]), _b64url(encrypted[-16:])))

