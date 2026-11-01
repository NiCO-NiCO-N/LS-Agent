"""执行器（Worker）：一张工单一个独立会话、一个独立目录、一段独立上下文。

与过去唯一的区别是入口：过去 run_turn 既当唯一入口又当引擎；
现在它是引擎，由这里按工单启动，历史天然隔离，不在主会话里累积。
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional

from . import agent, storage, tasks

EXECUTOR = "worker-1"


def open_worker(request: str, *, trigger: str = "", provider_id: Optional[str] = None) -> Dict[str, Any]:
    """建工单 + 建对应的 worker 会话（幂等：同 trigger 复用同一张工单）。"""
    ticket = tasks.create_ticket(request, trigger=trigger, source="chat")
    session_id = ticket.get("session") or ""
    if not session_id:
        session = storage.create_session(title=f"工单 {ticket['id']}", provider_id=provider_id)
        session["kind"] = "worker"
        session["ticket"] = ticket["id"]
        session = storage.save_session(session)
        session_id = session["id"]
        ticket = tasks.update_ticket(ticket["id"], {"session": session_id})
    return ticket


async def run(ticket_id: str, provider_id: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    """执行一张工单，边跑边把工单阶段推给前端。"""
    ticket = tasks.claim(ticket_id, executor=EXECUTOR)
    session_id = ticket.get("session") or open_worker(ticket["request"], provider_id=provider_id)["session"]
    ticket = tasks.start(ticket_id)
    yield {"type": "ticket", "id": ticket_id, "stage": tasks.stage(ticket), "session": session_id}

    said, failed = [], False
    async for event in agent.run_turn(session_id, ticket["request"], provider_id):
        kind = event.get("type")
        if kind == "delta":
            said.append(event.get("text") or "")
        elif kind == "error":
            failed = True
        yield event

    text = "".join(said).strip()
    tasks.report(ticket_id, "failed" if failed else "ready", text[:4000])
    yield {"type": "ticket", "id": ticket_id,
           "stage": tasks.stage(tasks.get_ticket(ticket_id) or {}), "session": session_id}


async def follow_up(ticket_id: str, text: str, provider_id: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    """人话回到已结束或受阻的工单：走 add_input 唤起，不新开工单。"""
    ticket = tasks.add_input(ticket_id, text)
    session_id = ticket.get("session")
    if not session_id:
        yield {"type": "error", "message": "工单没有可用会话，请重新派工"}
        return
    tasks.claim(ticket_id, executor=EXECUTOR)
    tasks.start(ticket_id)
    yield {"type": "ticket", "id": ticket_id, "stage": tasks.stage(tasks.get_ticket(ticket_id) or {})}
    said, failed = [], False
    async for event in agent.run_turn(session_id, text, provider_id):
        if event.get("type") == "delta":
            said.append(event.get("text") or "")
        elif event.get("type") == "error":
            failed = True
        yield event
    tasks.report(ticket_id, "failed" if failed else "ready", "".join(said).strip()[:4000])
    yield {"type": "ticket", "id": ticket_id, "stage": tasks.stage(tasks.get_ticket(ticket_id) or {})}
