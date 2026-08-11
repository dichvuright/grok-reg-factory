"""Bridge Outlook asset scan results with unlock and Graph RT recovery tools."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import os

from common import asset_scanner, asset_store


def _data_root() -> Path:
    return Path(os.environ.get("REG_FACTORY_DATA_DIR") or Path.cwd()).resolve()


def load_scan_candidates(statuses=("unlock", "expired")) -> list[dict]:
    """Return password-bearing emails.txt records matching cached Outlook statuses."""
    requested = {str(status).strip().lower() for status in statuses if str(status).strip()}
    report = asset_scanner.get_report()
    status_by_email = {
        str(item.get("email") or "").strip().lower(): item
        for item in report.get("items", [])
        if item.get("platform") == "outlook" and item.get("email")
    }
    candidates = []
    seen = set()
    for mailbox in asset_store._mailboxes():
        email = str(mailbox.get("email") or "").strip()
        identity = email.lower()
        if not identity or identity in seen or not mailbox.get("password"):
            continue
        outcome = status_by_email.get(identity)
        if not outcome:
            continue
        status = str(outcome.get("status") or "unknown").lower()
        evidence = str(outcome.get("evidence") or "")
        selected = status in requested
        if status == "unknown" and status in requested:
            selected = evidence == "local:missing_refresh_token"
        if not selected:
            continue
        seen.add(identity)
        candidates.append({
            "email": email,
            "password": mailbox.get("password") or "",
            "line": mailbox.get("line") or f"{email}----{mailbox.get('password') or ''}",
            "status": status,
            "evidence": evidence,
        })
    return candidates


def candidate_counts(candidates: list[dict]) -> dict[str, int]:
    return dict(sorted(Counter(item.get("status") or "unknown" for item in candidates).items()))


def _clear_error_entries(identities: set[str]) -> int:
    cleared = 0
    token_failure_markers = {
        "service_abuse",
        "tenant_mismatch",
        "invalid_grant",
        "missing_refresh_token",
        "refresh_token_unusable",
    }
    for path in _data_root().glob("emails_error_*.txt"):
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8", errors="replace").splitlines()
        kept = []
        for line in original:
            parts = line.split("----")
            email = parts[0].strip().lower() if parts else ""
            reason = "----".join(parts[2:]).strip().lower() if len(parts) > 2 else ""
            token_failure = any(marker in reason for marker in token_failure_markers)
            if email in identities and token_failure:
                cleared += 1
            else:
                kept.append(line)
        if len(kept) != len(original):
            temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
            temporary.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            temporary.replace(path)
    return cleared


def upsert_refresh_tokens(results: list[dict]) -> dict:
    """Atomically write recovered RTs to emails.txt and refresh cached asset state."""
    updates = {
        str(item.get("email") or "").strip().lower(): item
        for item in results
        if item.get("email") and item.get("refresh_token")
    }
    if not updates:
        return {"updated": 0, "appended": 0, "errors_cleared": 0}

    path = _data_root() / "emails.txt"
    original = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    output = []
    found = set()
    for raw in original:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            output.append(raw)
            continue
        parts = raw.split("----")
        identity = parts[0].strip().lower() if parts else ""
        update = updates.get(identity)
        if not update:
            output.append(raw)
            continue
        password = str(update.get("password") or (parts[1] if len(parts) > 1 else ""))
        extras = parts[4:] if len(parts) > 4 else []
        output.append("----".join([
            str(update.get("email") or parts[0]).strip(),
            password,
            str(update.get("refresh_token") or "").strip(),
            str(update.get("client_id") or "").strip(),
            *extras,
        ]))
        found.add(identity)

    for identity, update in updates.items():
        if identity in found:
            continue
        output.append("----".join([
            str(update.get("email") or "").strip(),
            str(update.get("password") or ""),
            str(update.get("refresh_token") or "").strip(),
            str(update.get("client_id") or "").strip(),
        ]))

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.replace(path)
    identities = set(updates)
    errors_cleared = _clear_error_entries(identities)
    asset_scanner.update_cached_outlook_statuses({
        identity: {
            "status": "normal",
            "detail": "Graph refresh token 已重新提取",
            "evidence": "recovery:refresh_token_updated",
        }
        for identity in identities
    })
    return {
        "updated": len(found),
        "appended": len(updates) - len(found),
        "errors_cleared": errors_cleared,
    }
