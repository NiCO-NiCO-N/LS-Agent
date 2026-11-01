"""会话持久化：data/sessions/<id>.json。"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from .config import SESSIONS_DIR

_lock = threading.RLock()


def _path(session_id: str):
    return SESSIONS_DIR / (session_id + ".json")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _plain(text: str) -> str:
    """把 Markdown 压成一行纯文本，用于会话列表预览。"""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[#>*_`~\[\]]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _write(session: Dict[str, Any]) -> Dict[str, Any]:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(session["id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return session


def create_session(title: str = "新对话", provider_id: Optional[str] = None) -> Dict[str, Any]:
    with _lock:
        ts = time.time()
        session = {
            "id": new_id(),
            "title": title,
            "provider_id": provider_id,
            "created_at": ts,
            "updated_at": ts,
            "messages": [],
            # main = 主会话（与人对话的 Router）；worker = 某张工单的执行会话
            "kind": "main",
            "ticket": None,
        }
        return _write(session)


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        path = _path(session_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None


def save_session(session: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        session["updated_at"] = time.time()
        return _write(session)


def list_sessions() -> List[Dict[str, Any]]:
    with _lock:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        items: List[Dict[str, Any]] = []
        for path in SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            messages = data.get("messages") or []
            preview = ""
            for m in reversed(messages):
                if m.get("role") == "assistant" and m.get("content"):
                    preview = _plain(str(m["content"]))[:70]
                    break
            items.append(
                {
                    "id": data.get("id") or path.stem,
                    "title": data.get("title") or "未命名对话",
                    "provider_id": data.get("provider_id"),
                    "updated_at": data.get("updated_at") or 0,
                    "created_at": data.get("created_at") or 0,
                    "message_count": len(messages),
                    "preview": preview,
                }
            )
        items.sort(key=lambda s: s["updated_at"], reverse=True)
        return items


def delete_session(session_id: str) -> bool:
    with _lock:
        path = _path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False


def append_message(session_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        session = get_session(session_id)
        if session is None:
            raise KeyError("会话不存在")
        message = dict(message)
        message.setdefault("id", new_id())
        message["created_at"] = time.time()
        session["messages"].append(message)

        # 首条用户消息自动生成标题
        if message.get("role") == "user" and session.get("title") in (None, "", "新对话"):
            text = (message.get("content") or "").strip().replace("\n", " ")
            if text:
                session["title"] = text[:24] + ("…" if len(text) > 24 else "")
        save_session(session)
        return message


def clear_messages(session_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        session = get_session(session_id)
        if session is None:
            return None
        session["messages"] = []
        return save_session(session)


def update_session(session_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    with _lock:
        session = get_session(session_id)
        if session is None:
            return None
        for key in ("title", "provider_id"):
            if key in patch and patch[key] is not None:
                session[key] = patch[key]
        return save_session(session)
