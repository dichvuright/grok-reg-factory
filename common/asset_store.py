"""Read registered mailboxes and platform credentials through a stable local API."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from common.session_export import (
    build_chatgpt2api_account,
    build_cpa_codex_json,
    build_sub2api_content,
    sub2api_expires_at,
)


class AssetError(Exception):
    status_code = 400


class AssetNotFound(AssetError):
    status_code = 404


class AssetExhausted(AssetError):
    status_code = 404


class AssetUnverified(AssetError):
    status_code = 409


_CURSOR_LOCK = threading.Lock()
_PLATFORMS = {
    "claude": {
        "key_names": {"sessionKey"},
        "domains": {"claude.ai"},
    },
    "chatgpt": {
        "key_names": {"__Secure-next-auth.session-token"},
        "domains": {"chatgpt.com", "openai.com"},
    },
    "grok": {
        "key_names": {"sso", "sso-rw", "__Secure-next-auth.session-token"},
        "domains": {"grok.com", "x.ai"},
    },
    "kiro": {
        "key_names": set(),
        "domains": set(),
    },
}


def _data_root() -> Path:
    return Path(os.environ.get("REG_FACTORY_DATA_DIR") or Path.cwd()).resolve()


def _token_root() -> Path:
    configured = os.environ.get("TOKEN_OUTPUT_DIR", "").strip()
    if not configured:
        env_path = Path(os.environ.get("REG_FACTORY_ENV_FILE") or _data_root() / ".env")
        if env_path.is_file():
            for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and line.partition("=")[0].strip() == "TOKEN_OUTPUT_DIR":
                    configured = line.partition("=")[2].strip().strip('"').strip("'")
                    break
    configured = configured or "tokens"
    path = Path(configured)
    return path.resolve() if path.is_absolute() else (_data_root() / path).resolve()


def _cursor_path() -> Path:
    return _data_root() / "runtime" / "state" / "asset_api_cursors.json"


def _claim_path() -> Path:
    return _data_root() / "runtime" / "state" / "asset_api_claims.json"


def _read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _read_cursors() -> dict[str, int]:
    try:
        value = _read_json(_cursor_path())
        if isinstance(value, dict):
            return {str(key): max(0, int(index)) for key, index in value.items()}
    except Exception:
        pass
    return {}


def _write_cursors(value: dict[str, int]) -> None:
    path = _cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read_claims() -> dict[str, set[str]]:
    try:
        value = _read_json(_claim_path())
        scopes = value.get("scopes", {}) if isinstance(value, dict) else {}
        if isinstance(scopes, dict):
            return {
                str(scope): {str(claim) for claim in claims if str(claim)}
                for scope, claims in scopes.items()
                if isinstance(claims, list)
            }
    except Exception:
        pass
    return {}


def _write_claims(value: dict[str, set[str]]) -> None:
    path = _claim_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    payload = {
        "version": 1,
        "scopes": {
            scope: sorted(claims)
            for scope, claims in sorted(value.items())
            if claims
        },
    }
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _claim_id(scope: str, identity: str) -> str:
    value = f"{scope}\0{str(identity or '').strip().lower()}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _asset_identity(email: str, source: str) -> str:
    normalized_email = str(email or "").strip().lower()
    if normalized_email:
        return f"email:{normalized_email}"
    return f"source:{str(source or '').strip().lower()}"


def _claim_record(
    records: list[dict],
    scope: str,
    identity_for,
    index: int | None,
) -> tuple[int, dict, int, int, bool]:
    """Atomically select and claim one healthy account across all output formats."""
    with _CURSOR_LOCK:
        claims = _read_claims()
        claimed = set(claims.get(scope, set()))
        candidates = []
        seen = set(claimed)
        for record in records:
            claim = _claim_id(scope, identity_for(record))
            if claim in seen:
                continue
            seen.add(claim)
            candidates.append((record, claim))

        total = len(candidates)
        if total <= 0:
            raise AssetExhausted("没有未领取的正常资产；如需重新使用，请重置领取记录")
        selected = 0 if index is None else index
        if selected < 0 or selected >= total:
            raise AssetNotFound(f"index 超出未领取正常资产范围：{selected}，可用范围 0-{total - 1}")

        record, claim = candidates[selected]
        claimed.add(claim)
        claims[scope] = claimed
        _write_claims(claims)
    return selected, record, total, total - 1, index is None


def _select_index(total: int, cursor_key: str, index: int | None) -> tuple[int, int, bool]:
    if total <= 0:
        raise AssetNotFound("没有可读取的资产")
    if index is not None:
        if index < 0 or index >= total:
            raise AssetNotFound(f"index 超出范围：{index}，可用范围 0-{total - 1}")
        return index, index + 1, False
    with _CURSOR_LOCK:
        cursors = _read_cursors()
        selected = int(cursors.get(cursor_key, 0))
        if selected >= total:
            raise AssetExhausted(f"顺序游标已取完：{selected}/{total}；请指定 index 或重置游标")
        cursors[cursor_key] = selected + 1
        _write_cursors(cursors)
    return selected, selected + 1, True


def _claim_scope(scope: str) -> str:
    if scope in {"outlook", *_PLATFORMS}:
        return scope
    if scope in {"email", "verified:email"}:
        return "outlook"
    parts = scope.split(":")
    for platform in _PLATFORMS:
        if platform in parts:
            return platform
    return ""


def reset_cursor(scope: str = "all") -> dict:
    normalized = str(scope or "all").strip().lower()
    with _CURSOR_LOCK:
        cursors = _read_cursors()
        claims = _read_claims()
        if normalized == "all":
            removed = sorted(cursors)
            cursors = {}
            claims_removed = sum(len(items) for items in claims.values())
            claim_scopes_removed = sorted(claims)
            claims = {}
        else:
            removed = [normalized] if normalized in cursors else []
            cursors.pop(normalized, None)
            claim_scope = _claim_scope(normalized)
            claim_scopes_removed = [claim_scope] if claim_scope in claims else []
            claims_removed = len(claims.pop(claim_scope, set())) if claim_scope else 0
        _write_cursors(cursors)
        _write_claims(claims)
    return {
        "scope": normalized,
        "removed": removed,
        "remaining": cursors,
        "claim_scopes_removed": claim_scopes_removed,
        "claims_removed": claims_removed,
        "remaining_claims": {key: len(value) for key, value in sorted(claims.items())},
    }


def _mailboxes() -> list[dict]:
    path = _data_root() / "emails.txt"
    if not path.is_file():
        return []
    records = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        records.append({
            "email": parts[0].strip(),
            "password": parts[1].strip() if len(parts) > 1 else "",
            "refresh_token": parts[2].strip() if len(parts) > 2 else "",
            "client_id": parts[3].strip() if len(parts) > 3 else "",
            "line": line,
        })
    return records


def _verification_for(platform: str, email: str, source: str) -> dict | None:
    """Return a recent normal scan record that identifies this local asset.

    This import stays lazy because asset_scanner imports this module to read the
    local pools.  Matching by email handles merged token/cookie records; the
    source fallback keeps records without an embedded email usable.
    """
    from common import asset_scanner

    normalized_email = str(email or "").strip().lower()
    normalized_source = str(source or "").strip()
    for item in asset_scanner.get_report().get("items", []):
        if not isinstance(item, dict):
            continue
        if item.get("platform") != platform or item.get("status") != "normal":
            continue
        item_email = str(item.get("email") or "").strip().lower()
        if normalized_email and item_email == normalized_email:
            verification = {
                "status": "normal",
                "checked_at": str(item.get("checked_at") or ""),
                "evidence": str(item.get("evidence") or ""),
            }
            if platform == "chatgpt":
                verification.update({
                    "plus_trial": str(item.get("plus_trial") or "unknown"),
                    "plus_trial_detail": str(item.get("plus_trial_detail") or ""),
                    "plus_trial_evidence": str(item.get("plus_trial_evidence") or ""),
                })
            return verification
        sources = {part.strip() for part in str(item.get("source") or "").split(",")}
        if normalized_source and normalized_source in sources:
            verification = {
                "status": "normal",
                "checked_at": str(item.get("checked_at") or ""),
                "evidence": str(item.get("evidence") or ""),
            }
            if platform == "chatgpt":
                verification.update({
                    "plus_trial": str(item.get("plus_trial") or "unknown"),
                    "plus_trial_detail": str(item.get("plus_trial_detail") or ""),
                    "plus_trial_evidence": str(item.get("plus_trial_evidence") or ""),
                })
            return verification
    return None


def _verified_records(platform: str, records: list[dict], source_for) -> list[dict]:
    verified = []
    for record in records:
        verification = _verification_for(platform, record.get("email", ""), source_for(record))
        if verification:
            verified.append({**record, "_verification": verification})
    if not verified:
        raise AssetUnverified("没有通过本次在线检测的正常资产，已拦截封禁、失效和凭据异常记录")
    return verified


def get_email(
    index: int | None = None,
    output_format: str = "json",
    verified_only: bool = False,
) -> dict:
    output_format = str(output_format or "json").strip().lower()
    if output_format not in {"json", "line"}:
        raise AssetError("邮箱 format 仅支持 json、line")
    records = [
        {**record, "_asset_source": f"emails.txt:{line_number}"}
        for line_number, record in enumerate(_mailboxes(), start=1)
    ]
    if verified_only:
        records = _verified_records("outlook", records, lambda record: record["_asset_source"])
        selected, record, total, remaining, advanced = _claim_record(
            records,
            "outlook",
            lambda record: _asset_identity(record.get("email", ""), record["_asset_source"]),
            index,
        )
        next_index = 0 if remaining else None
    else:
        selected, next_index, advanced = _select_index(len(records), "email", index)
        record = records[selected]
        total = len(records)
    data = record["line"] if output_format == "line" else {
        key: value for key, value in record.items() if key != "line" and not key.startswith("_")
    }
    result = {
        "kind": "email",
        "format": output_format,
        "index": selected,
        "total": total,
        "next_index": next_index,
        "cursor_advanced": advanced,
        "data": data,
    }
    if verified_only:
        result["verification"] = record["_verification"]
        result.update({
            "claim_recorded": True,
            "claim_scope": "outlook",
            "remaining": remaining,
        })
    return result


def _domain_allowed(domain: str, allowed: set[str]) -> bool:
    normalized = str(domain or "").lstrip(".").lower()
    return any(normalized == item or normalized.endswith(f".{item}") for item in allowed)


def _cookie_directories(platform: str) -> list[Path]:
    root = _data_root() / "cookies"
    directories = [root / platform]
    if platform == "claude":
        directories.append(root)
    return directories


def _account_map(directory: Path) -> dict[str, str]:
    path = directory / "accounts.txt"
    result = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.strip().split("|")
        if len(parts) >= 3 and parts[0] and parts[2]:
            result[parts[2]] = parts[0]
    return result


def _cookie_records(platform: str) -> list[dict]:
    config = _PLATFORMS[platform]
    records = []
    seen_paths = set()
    for directory in _cookie_directories(platform):
        accounts = _account_map(directory)
        paths = directory.glob("full_*.json") if directory.is_dir() else ()
        for path in paths:
            resolved = str(path.resolve()).lower()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                raw_cookies = _read_json(path)
            except Exception:
                continue
            if not isinstance(raw_cookies, list):
                continue
            cookies = [
                item for item in raw_cookies
                if isinstance(item, dict) and _domain_allowed(item.get("domain", ""), config["domains"])
            ]
            key_cookie = next(
                (item for item in cookies if item.get("name") in config["key_names"] and item.get("value")),
                None,
            )
            if not key_cookie:
                continue
            records.append({
                "path": path,
                "email": accounts.get(str(key_cookie["value"]), ""),
                "cookies": cookies,
            })
    return sorted(records, key=lambda item: (item["path"].stat().st_mtime, str(item["path"]).lower()))


def _token_records(platform: str) -> list[dict]:
    directory = _token_root() / platform
    pattern = "*.session.json" if platform == "chatgpt" else "*.account.json" if platform == "kiro" else "*.sso.json"
    records = []
    paths = directory.glob(pattern) if directory.is_dir() else ()
    for path in paths:
        try:
            data = _read_json(path)
        except Exception:
            continue
        if isinstance(data, dict):
            records.append({"path": path, "data": data})
    return sorted(records, key=lambda item: (item["path"].stat().st_mtime, str(item["path"]).lower()))


def _cookie_header(cookies: list[dict]) -> str:
    return "; ".join(
        f"{item.get('name')}={item.get('value')}"
        for item in cookies if item.get("name") and item.get("value") is not None
    )


def _standard_cookie(cookie: dict) -> dict:
    """Convert a stored Playwright cookie to a browser-extension import record."""
    same_site = {
        "none": "no_restriction",
        "no_restriction": "no_restriction",
        "lax": "lax",
        "strict": "strict",
        "unspecified": "unspecified",
    }.get(str(cookie.get("sameSite") or "").strip().lower(), "unspecified")
    secure = bool(cookie.get("secure", False))
    if same_site == "no_restriction":
        secure = True

    expiration = cookie.get("expirationDate", cookie.get("expires"))
    try:
        expiration = float(expiration)
        if expiration <= 0:
            expiration = None
    except (TypeError, ValueError):
        expiration = None

    domain = str(cookie.get("domain") or "")
    result = {
        "domain": domain,
        "hostOnly": bool(cookie.get("hostOnly", not domain.startswith("."))),
        "httpOnly": bool(cookie.get("httpOnly", False)),
        "name": str(cookie.get("name") or ""),
        "path": str(cookie.get("path") or "/"),
        "sameSite": same_site,
        "secure": secure,
        "session": bool(cookie.get("session", expiration is None)),
        "storeId": str(cookie.get("storeId", "0")),
        "value": str(cookie.get("value") or ""),
    }
    if expiration is not None:
        result["expirationDate"] = expiration
        result["session"] = False
    return result


def _email_from_session(session: dict, fallback: str = "") -> str:
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    return str(user.get("email") or session.get("email") or fallback).strip()


def get_platform_asset(
    platform: str,
    output_format: str = "raw",
    index: int | None = None,
    verified_only: bool = False,
) -> dict:
    platform = str(platform or "").strip().lower()
    output_format = str(output_format or "raw").strip().lower()
    if platform not in _PLATFORMS:
        raise AssetError("platform 仅支持 claude、chatgpt、grok、kiro")

    token_formats = {"session", "sub2api", "cpa", "chatgpt2api"}
    if output_format in {"raw", "cookies", "header"}:
        records = _cookie_records(platform)
        if verified_only:
            records = _verified_records(platform, records, lambda record: record["path"].name)
            selected, record, total, remaining, advanced = _claim_record(
                records,
                platform,
                lambda record: _asset_identity(record.get("email", ""), record["path"].name),
                index,
            )
            next_index = 0 if remaining else None
        else:
            cursor_key = f"cookie:{platform}:{output_format}"
            selected, next_index, advanced = _select_index(len(records), cursor_key, index)
            record = records[selected]
            total = len(records)
        if output_format == "raw":
            data = record["cookies"]
        elif output_format == "cookies":
            data = [_standard_cookie(cookie) for cookie in record["cookies"]]
        else:
            data = _cookie_header(record["cookies"])
        email = record["email"]
        source = record["path"].name
        extra = {}
    elif output_format in token_formats:
        if platform == "claude":
            raise AssetError("Claude 不支持 session、sub2api、cpa 或 chatgpt2api 格式，请使用 cookies/raw/header")
        if platform == "grok" and output_format not in {"session", "sub2api"}:
            raise AssetError("Grok 仅支持 cookies、raw、header、session、sub2api 格式")
        records = _token_records(platform)
        if verified_only:
            records = _verified_records(
                platform,
                records,
                lambda record: record["path"].name,
            )
            selected, record, total, remaining, advanced = _claim_record(
                records,
                platform,
                lambda record: _asset_identity(
                    _email_from_session(
                        record["data"], record["path"].stem.replace(".session", "")
                    ),
                    record["path"].name,
                ),
                index,
            )
            next_index = 0 if remaining else None
        else:
            cursor_key = f"cookie:{platform}:{output_format}"
            selected, next_index, advanced = _select_index(len(records), cursor_key, index)
            record = records[selected]
            total = len(records)
        session = record["data"]
        source = record["path"].name
        email = _email_from_session(session, record["path"].stem.replace(".session", ""))
        extra = {}
        if output_format == "session":
            data = session
        elif platform == "grok":
            data = {"sso_tokens": [str(session.get("sso") or "")], "name": email}
        elif platform == "kiro":
            data = session
        elif output_format == "sub2api":
            data = {
                "content": build_sub2api_content(session),
                "expires_at": sub2api_expires_at(session),
            }
        elif output_format == "cpa":
            converted = build_cpa_codex_json(session, email=email)
            data = converted["auth_json"]
            extra["file_name"] = converted["file_name"]
        else:
            data = build_chatgpt2api_account(session, email=email)
    else:
        raise AssetError("format 仅支持 raw、cookies、header、session、sub2api、cpa、chatgpt2api")

    result = {
        "kind": "platform_cookie",
        "platform": platform,
        "format": output_format,
        "index": selected,
        "total": total,
        "next_index": next_index,
        "cursor_advanced": advanced,
        "email": email,
        "source": source,
        "data": data,
        **extra,
    }
    if verified_only:
        result["verification"] = record["_verification"]
        result.update({
            "claim_recorded": True,
            "claim_scope": platform,
            "remaining": remaining,
        })
    return result


def summary() -> dict:
    claims = _read_claims()
    return {
        "emails": len(_mailboxes()),
        "platforms": {
            platform: {
                "cookies": len(_cookie_records(platform)),
                "sessions": len(_token_records(platform)) if platform in {"chatgpt", "grok", "kiro"} else 0,
            }
            for platform in _PLATFORMS
        },
        "cursors": _read_cursors(),
        "claims": {scope: len(items) for scope, items in sorted(claims.items())},
    }
