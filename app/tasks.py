"""工单与「先落盘，再执行」。

借鉴 DeepSeek Super-agent 的两条设计：
1. Session 是实现细节，任务是独立实体（Ticket 自己定义状态，不跟随某次会话）；
2. 任何有外部副作用的动作先写进队列、再做，失败按指数退避重试，重启不丢。

存储是 data/tasks/<ticket_id>.json 与 data/tasks/queue.json，写入用临时文件 + 替换。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DATA_DIR

TASKS_DIR: Path = DATA_DIR / "tasks"
QUEUE_PATH: Path = TASKS_DIR / "queue.json"

# 报告状态：进展 / 等人 / 就绪 / 受阻 / 失败 / 无需处理
REPORT_STATES = {"progress", "waiting", "ready", "blocked", "failed", "dropped"}

_lock = threading.RLock()


def _now() -> float:
    return round(time.time(), 3)


def _write_json(path: Path, data: Any) -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _ticket_path(ticket_id: str) -> Path:
    if not ticket_id or any(c in ticket_id for c in "\\/."):
        raise ValueError(f"非法工单 id：{ticket_id!r}")
    return TASKS_DIR / f"{ticket_id}.json"


# ---------------------------------------------------------------- 工单

def stage(ticket: Dict[str, Any]) -> str:
    """按 003 的判定顺序，返回工单当前阶段（面板直接显示这个）。"""
    if ticket.get("state") == "done":
        return "done"
    if ticket.get("attention"):
        return "person/answer"
    if (ticket.get("assignment") or {}).get("state") == "pending":
        return "working/queued"
    last = (ticket.get("reports") or [{}])[-1]
    report = last.get("state")
    # 人的新消息解锁 blocked / failed：003 里「a new human message releases it」
    released = bool(ticket.get("inputs")) and (ticket["inputs"][-1]["at"] > last.get("at", 0))
    if report == "ready":
        return "working" if released else "person/review"
    if report in ("blocked", "failed") and not released:
        return f"person/{report}"
    if report == "waiting" or not ticket.get("assignment"):
        return "idle"
    return "working"


def create_ticket(request: str, *, scope: str = "", trigger: str = "",
                  source: str = "local", policy_version: str = "v1") -> Dict[str, Any]:
    """Eval 接单后建工单。同一 trigger 重复接单只产生一个工单（幂等）。"""
    with _lock:
        if trigger:
            for item in list_tickets():
                if item.get("trigger") == trigger:
                    return item
        ticket_id = time.strftime("%y%m%d") + "-" + uuid.uuid4().hex[:6]
        ticket = {
            "id": ticket_id,
            "source": source,
            "trigger": trigger,
            "request": request.strip(),
            "scope": scope,
            # Mandate：授权凭据，不可变；范围变大只能开工单
            "mandate": {"policy_version": policy_version, "scope": scope,
                        "strength": "explicit", "granted_at": _now()},
            "state": "open",
            "attention": "",
            "assignment": None,
            "reports": [],
            "inputs": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        _write_json(_ticket_path(ticket_id), ticket)
        return ticket


def list_tickets() -> List[Dict[str, Any]]:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(TASKS_DIR.glob("*.json")):
        if path.name == QUEUE_PATH.name:
            continue
        item = _read_json(path, None)
        if isinstance(item, dict) and item.get("id"):
            items.append(item)
    return items


def get_ticket(ticket_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(_ticket_path(ticket_id), None)


def update_ticket(ticket_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        ticket = get_ticket(ticket_id)
        if not ticket:
            raise KeyError(f"工单不存在：{ticket_id}")
        ticket.update(patch)
        ticket["updated_at"] = _now()
        _write_json(_ticket_path(ticket_id), ticket)
        return ticket


def claim(ticket_id: str, executor: str) -> Dict[str, Any]:
    """派工：把工单绑到一个执行器实例上。"""
    with _lock:
        ticket = get_ticket(ticket_id)
        if not ticket:
            raise KeyError(f"工单不存在：{ticket_id}")
        if ticket.get("state") == "done":
            raise ValueError("工单已结束，不能派工")
        ticket["assignment"] = {"id": uuid.uuid4().hex[:8], "executor": executor,
                                "state": "pending", "at": _now()}
        ticket["updated_at"] = _now()
        _write_json(_ticket_path(ticket_id), ticket)
        return ticket


def start(ticket_id: str) -> Dict[str, Any]:
    with _lock:
        ticket = get_ticket(ticket_id)
        if not ticket or not ticket.get("assignment"):
            raise KeyError("没有可启动的派工")
        ticket["assignment"]["state"] = "running"
        ticket["updated_at"] = _now()
        _write_json(_ticket_path(ticket_id), ticket)
        return ticket


def report(ticket_id: str, state: str, text: str = "", *, attention: str = "") -> Dict[str, Any]:
    """执行器回报。ready 交人审、blocked/failed 等人解锁、dropped 静默结束。"""
    if state not in REPORT_STATES:
        raise ValueError(f"state 只能是 {sorted(REPORT_STATES)}")
    with _lock:
        ticket = get_ticket(ticket_id)
        if not ticket:
            raise KeyError(f"工单不存在：{ticket_id}")
        ticket["reports"].append({"state": state, "text": text, "at": _now()})
        ticket["attention"] = attention
        assignment = ticket.get("assignment")
        if assignment:
            assignment["state"] = "ended"
        if state in ("ready", "dropped"):
            ticket["state"] = "done"
        ticket["updated_at"] = _now()
        _write_json(_ticket_path(ticket_id), ticket)
        return ticket


def add_input(ticket_id: str, text: str, kind: str = "text") -> Dict[str, Any]:
    with _lock:
        ticket = get_ticket(ticket_id)
        if not ticket:
            raise KeyError(f"工单不存在：{ticket_id}")
        ticket["inputs"].append({"kind": kind, "text": text, "at": _now()})
        if ticket["state"] == "done" and ticket["mandate"]:
            ticket["state"] = "open"  # 人再说话就恢复（mandate 仍有效）
        ticket["attention"] = ""
        ticket["updated_at"] = _now()
        _write_json(_ticket_path(ticket_id), ticket)
        return ticket


# ---------------------------------------------------------------- 队列

def enqueue(kind: str, payload: Dict[str, Any], *, key: str = "") -> Dict[str, Any]:
    """先落盘：把要做的事写进队列，由调用方在写完后再执行。key 相同视为同一件事。"""
    with _lock:
        queue = _read_json(QUEUE_PATH, [])
        if not isinstance(queue, list):
            queue = []
        if key:
            for item in queue:
                if item.get("key") == key and item.get("state") in ("pending", "failed"):
                    return item
        item = {"id": uuid.uuid4().hex[:10], "kind": kind, "payload": payload,
                "key": key, "state": "pending", "attempts": 0,
                "next_at": 0.0, "error": "", "created_at": _now()}
        queue.append(item)
        _write_json(QUEUE_PATH, queue)
        return item


def due(limit: int = 20) -> List[Dict[str, Any]]:
    """取到点可执行的任务（失败的按 2s 起翻倍退避）。"""
    with _lock:
        queue = _read_json(QUEUE_PATH, [])
        now = _now()
        return [i for i in queue if i.get("state") == "pending" and i.get("next_at", 0) <= now][:limit]


def done(item_id: str) -> None:
    with _lock:
        queue = _read_json(QUEUE_PATH, [])
        for item in queue:
            if item["id"] == item_id:
                item["state"] = "done"
                item["attempts"] = item.get("attempts", 0) + 1
        _write_json(QUEUE_PATH, [i for i in queue if i.get("state") != "done"])


def fail(item_id: str, error: str) -> Dict[str, Any]:
    """失败不丢：次数 +1，退避 2s × 2^n，封顶 60s；连续 3 次会被前端标成系统问题。"""
    with _lock:
        queue = _read_json(QUEUE_PATH, [])
        target = {}
        for item in queue:
            if item["id"] == item_id:
                attempts = item.get("attempts", 0) + 1
                item["attempts"] = attempts
                item["state"] = "pending" if attempts < 5 else "dead"
                item["next_at"] = _now() + min(2.0 * (2 ** (attempts - 1)), 60.0)
                item["error"] = error[:300]
                target = item
        _write_json(QUEUE_PATH, queue)
        return target


def pending_count() -> int:
    queue = _read_json(QUEUE_PATH, [])
    return len([i for i in queue if i.get("state") == "pending"]) if isinstance(queue, list) else 0
