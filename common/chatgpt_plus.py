"""Local handoff from ChatGPT registration to the Plus workbench."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from common.session_export import chatgpt_session_path


_QUEUE_LOCK = threading.Lock()


def plus_runtime_dir() -> Path:
    data_root = Path(os.environ.get("REG_FACTORY_DATA_DIR") or os.getcwd()).resolve()
    return data_root / "runtime" / "chatgpt_plus"


def plus_queue_path() -> Path:
    configured = os.environ.get("REG_FACTORY_PLUS_QUEUE_FILE", "").strip()
    return Path(configured).resolve() if configured else plus_runtime_dir() / "registration_queue.json"


def queue_registered_account(email: str) -> dict:
    """Queue one saved session without duplicating its access token."""
    session_path = Path(chatgpt_session_path(email)).resolve()
    if not session_path.is_file():
        raise FileNotFoundError(f"ChatGPT session not found: {session_path}")

    queue_path = plus_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": uuid.uuid4().hex,
        "email": str(email or "").strip(),
        "session_path": str(session_path),
        "status": "pending",
        "source": "chatgpt_registration",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "max_concurrency": 27,
    }
    with _QUEUE_LOCK:
        payload = {"version": 1, "max_concurrency": 27, "items": []}
        if queue_path.is_file():
            try:
                current = json.loads(queue_path.read_text(encoding="utf-8"))
                if isinstance(current, dict) and isinstance(current.get("items"), list):
                    payload = current
            except (OSError, ValueError):
                pass
        items = [item for item in payload.get("items", []) if item.get("email") != entry["email"]]
        payload.update({"version": 1, "max_concurrency": 27, "items": [entry, *items][:100]})
        temporary = queue_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, queue_path)
    return entry
