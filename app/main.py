"""FastAPI 应用：REST + SSE + 静态前端。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Body, FastAPI, HTTPException

from . import router, worker
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, agent, llm, logging_setup, skills as skills_mod, storage
from .config import (
    DEFAULT_CONFIG,
    WORKSPACE_DIR,
    delete_provider,
    get_provider,
    load_config,
    public_config,
    save_config,
    update_config,
    upsert_provider,
)
from .tools import tool_catalog

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

@asynccontextmanager
async def lifespan(_: FastAPI):
    cfg = load_config()
    if not cfg.get("providers"):
        save_config(dict(DEFAULT_CONFIG))
    info = logging_setup.setup_logging(cfg)
    log = logging_setup.get_logger("server")
    log.info("ls agent %s 启动 · provider=%s · workspace=%s", __version__, cfg.get("active_provider"), WORKSPACE_DIR)
    log.info("日志文件：%s（%d 字节）", info.get("file") or "仅内存/控制台", info.get("file_size") or 0)
    yield
    log.info("ls agent 已退出")


app = FastAPI(title="ls agent", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

PROVIDER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,39}$")

_QUIET_PATHS = ("/static/", "/favicon.ico", "/api/logs")  # 日志面板自己轮询的请求不记，避免自我刷屏


@app.middleware("http")
async def log_requests(request, call_next):
    """给每个请求带上 rid 并记录耗时；静态资源不记，免得刷屏。"""
    path = request.url.path
    if path.startswith(_QUIET_PATHS):
        return await call_next(request)

    log = logging_setup.get_logger("http")
    token = logging_setup.new_request_id()
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("%s %s 未处理异常（%.0f ms）", request.method, path, (time.perf_counter() - start) * 1000)
        logging_setup.reset_request_id(token)
        raise

    ms = (time.perf_counter() - start) * 1000
    status = getattr(response, "status_code", 0)
    log.log(logging.WARNING if status >= 400 else logging.INFO, "%s %s → %s · %.0f ms", request.method, path, status, ms)
    logging_setup.reset_request_id(token)
    return response


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/info")
def info() -> Dict[str, Any]:
    cfg = load_config()
    return {
        "name": cfg.get("assistant_name") or "ls agent",
        "version": __version__,
        "workspace": str(WORKSPACE_DIR),
        "provider": (get_provider(None) or {}).get("id"),
    }


# --------------------------------------------------------------------------- #
# 配置 / 提供商
# --------------------------------------------------------------------------- #
@app.get("/api/config")
def read_config() -> Dict[str, Any]:
    return public_config()


@app.put("/api/config")
def write_config(patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    if "enabled_skills" in patch and not isinstance(patch["enabled_skills"], list):
        raise HTTPException(400, "enabled_skills 必须是数组")
    if "disabled_tools" in patch and not isinstance(patch["disabled_tools"], list):
        raise HTTPException(400, "disabled_tools 必须是数组")
    if "max_steps" in patch:
        try:
            patch["max_steps"] = max(1, min(int(patch["max_steps"]), 30))
        except Exception:
            raise HTTPException(400, "max_steps 必须是数字")
    if "log_level" in patch:
        level = str(patch["log_level"] or "").upper()
        if level not in logging_setup.LEVELS:
            raise HTTPException(400, "log_level 只能是 DEBUG / INFO / WARNING / ERROR")
        patch["log_level"] = level
    for key, low, high, label in (
        ("log_max_mb", 1, 50, "log_max_mb"),
        ("log_backups", 0, 20, "log_backups"),
    ):
        if key in patch:
            try:
                patch[key] = max(low, min(int(patch[key]), high))
            except Exception:
                raise HTTPException(400, f"{label} 必须是数字")
    for key in ("log_console", "log_prompts"):
        if key in patch:
            patch[key] = bool(patch[key])
    update_config(patch)
    if any(k.startswith("log_") for k in patch):
        logging_setup.setup_logging(load_config(), force=True)
        logging_setup.get_logger("config").info(
            "日志配置已更新：级别 %s，控制台 %s，文件 %s",
            load_config().get("log_level"),
            "开" if load_config().get("log_console") else "关",
            logging_setup.resolve_log_file(load_config()) or "关闭",
        )
    return public_config()


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
@app.get("/api/logs")
def read_logs(limit: int = 200, level: str = "", search: str = "") -> Dict[str, Any]:
    cfg = load_config()
    return {
        "entries": logging_setup.recent(limit=limit, level=level, search=search),
        **logging_setup.stats(cfg),
    }


@app.delete("/api/logs")
def clear_logs() -> Dict[str, Any]:
    cfg = load_config()
    return {**logging_setup.clear(cfg), "entries": []}


@app.get("/api/logs/download", include_in_schema=False)
def download_logs():
    cfg = load_config()
    path = logging_setup.resolve_log_file(cfg)
    if path is None or not path.exists():
        raise HTTPException(404, "没有日志文件（可能已关闭文件日志或还没写入）")
    for handler in logging.getLogger(logging_setup.LOGGER_NAME).handlers:
        try:
            handler.flush()
        except Exception:
            pass
    return FileResponse(path, filename=path.name, media_type="text/plain")


def _normalize_provider(payload: Dict[str, Any], existing_id: Optional[str] = None) -> Dict[str, Any]:
    pid = (payload.get("id") or existing_id or "").strip()
    if not pid:
        pid = re.sub(r"[^a-zA-Z0-9]+", "-", str(payload.get("name") or "provider")).strip("-").lower() or "provider"
    if not PROVIDER_ID_RE.match(pid):
        raise HTTPException(400, "provider id 只能是字母、数字、- 和 _，且不超过 40 字符")
    ptype = (payload.get("type") or "openai").lower()
    if ptype not in ("openai", "anthropic", "mock"):
        raise HTTPException(400, "type 只能是 openai / anthropic / mock")
    try:
        temperature = float(payload.get("temperature", 0.7))
        max_tokens = int(payload.get("max_tokens", 2048))
    except Exception:
        raise HTTPException(400, "temperature / max_tokens 必须是数字")
    return {
        "id": pid,
        "name": str(payload.get("name") or pid),
        "type": ptype,
        "base_url": str(payload.get("base_url") or "").strip(),
        "api_key": str(payload.get("api_key") or "").strip(),
        "api_key_env": str(payload.get("api_key_env") or "").strip(),
        "model": str(payload.get("model") or "").strip(),
        "temperature": max(0.0, min(temperature, 2.0)),
        "max_tokens": max(64, min(max_tokens, 32000)),
    }


@app.get("/api/providers")
def list_providers() -> List[Dict[str, Any]]:
    return public_config()["providers"]


@app.post("/api/providers")
def create_provider(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    provider = _normalize_provider(payload)
    cfg = load_config()
    if any(p["id"] == provider["id"] for p in cfg["providers"]):
        raise HTTPException(400, f"provider {provider['id']} 已存在")
    cfg["providers"].append(provider)
    save_config(cfg)
    return public_config()


@app.put("/api/providers/{provider_id}")
def modify_provider(provider_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    cfg = load_config()
    existing = next((p for p in cfg["providers"] if p["id"] == provider_id), None)
    if existing is None:
        raise HTTPException(404, "provider 不存在")
    merged = dict(existing)
    merged.update(_normalize_provider(payload, existing_id=provider_id))
    if not merged.get("api_key"):
        merged["api_key"] = existing.get("api_key") or ""  # 空字符串表示沿用旧 Key
    upsert_provider(merged)
    return public_config()


@app.delete("/api/providers/{provider_id}")
def remove_provider(provider_id: str) -> Dict[str, Any]:
    cfg = load_config()
    if not any(p["id"] == provider_id for p in cfg["providers"]):
        raise HTTPException(404, "provider 不存在")
    if len(cfg["providers"]) <= 1:
        raise HTTPException(400, "至少保留一个模型提供商")
    delete_provider(provider_id)
    return public_config()


@app.post("/api/providers/models")
async def preview_models(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """按填好的 Base URL / Key 拉一遍可用模型列表，供设置弹窗里挑选。"""
    base = str(payload.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise HTTPException(400, "请先填写 Base URL")
    ptype = str(payload.get("type") or "openai").strip().lower()
    key = str(payload.get("api_key") or "").strip()
    pid = str(payload.get("provider_id") or "").strip()
    if pid:
        old = get_provider(pid)
        if old:
            if not key:
                key = str(old.get("api_key") or "")
            if not payload.get("type"):
                ptype = str(old.get("type") or "openai").lower()
    env = str(payload.get("api_key_env") or "").strip()
    if not key and env:
        key = os.environ.get(env, "")
    if ptype == "mock":
        return {"ok": True, "models": ["mock-model"], "message": "Mock 模型无需拉取"}
    headers = {"Accept": "application/json"}
    if key:
        if ptype == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"
    url = base + ("/models" if base.endswith("/v1") else ("/v1/models" if ptype == "anthropic" else "/v1/models"))
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        logging.getLogger(logging_setup.LOGGER_NAME).warning("拉取模型列表失败 · %s · %s", url, exc)
        return {"ok": False, "models": [], "message": f"{type(exc).__name__}: {exc}"}
    if resp.status_code >= 400:
        return {"ok": False, "models": [], "message": f"HTTP {resp.status_code}：{resp.text[:200]}"}
    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "models": [], "message": "返回内容不是 JSON，检查 Base URL 是否为 /v1 接口地址"}
    items = data.get("data") or data.get("models") or []
    ids: List[str] = []
    for it in (items if isinstance(items, list) else []):
        if isinstance(it, str):
            ids.append(it)
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("name")
            if mid:
                ids.append(str(mid))
    ids = sorted(set(ids))
    logging.getLogger(logging_setup.LOGGER_NAME).info("模型列表预览 · %s · 共 %d 个", url, len(ids))
    return {"ok": bool(ids), "models": ids, "message": f"共 {len(ids)} 个可用模型" if ids else "接口可用但没返回模型"}


@app.post("/api/providers/{provider_id}/test")
async def test_provider(provider_id: str) -> Dict[str, Any]:
    provider = get_provider(provider_id)
    if provider is None or provider["id"] != provider_id:
        raise HTTPException(404, "provider 不存在")
    if provider.get("type") == "mock":
        return {"ok": True, "message": "Mock 模型无需连接测试"}
    chunks: List[str] = []
    try:
        async for event in llm.astream(
            provider,
            [{"role": "user", "content": "ping，请只回复 pong"}],
            [],
            "你是连通性测试助手，只回复 pong。",
        ):
            if event.get("type") == "delta":
                chunks.append(event.get("text", ""))
            if sum(len(c) for c in chunks) > 200:
                break
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    reply = "".join(chunks).strip()
    return {"ok": bool(reply), "message": reply[:200] or "连接成功但没有返回文本"}


# --------------------------------------------------------------------------- #
# 工具 / 技能
# --------------------------------------------------------------------------- #
@app.get("/api/tools")
def list_tools() -> List[Dict[str, Any]]:
    return tool_catalog()


@app.get("/api/skills")
def list_skills() -> List[Dict[str, Any]]:
    return skills_mod.list_skills()


@app.get("/api/skills/{slug}")
def get_skill(slug: str) -> Dict[str, Any]:
    try:
        return skills_mod.read_skill(slug)
    except skills_mod.SkillError as exc:
        raise HTTPException(404, str(exc))


class SkillCreateIn(BaseModel):
    slug: str
    name: str = ""
    description: str = ""
    body: str = ""
    enable: bool = True


@app.post("/api/skills")
def create_skill(payload: SkillCreateIn) -> Dict[str, Any]:
    try:
        return skills_mod.create_skill(
            payload.slug.strip(),
            payload.name.strip() or payload.slug.strip(),
            payload.description.strip(),
            payload.body,
            payload.enable,
        )
    except skills_mod.SkillError as exc:
        raise HTTPException(400, str(exc))


class SkillUpdateIn(BaseModel):
    content: Optional[str] = None
    body: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None


@app.put("/api/skills/{slug}")
def update_skill(slug: str, payload: SkillUpdateIn) -> Dict[str, Any]:
    try:
        current = skills_mod.read_skill(slug)
        if payload.content is not None:
            return skills_mod.update_skill(slug, payload.content)
        content = current["content"]
        if payload.body is not None:
            meta, _ = skills_mod._split_frontmatter(content)
            front = ["---"]
            front.append(f"name: {payload.name or meta.get('name') or current['name']}")
            front.append(f"description: {payload.description if payload.description is not None else current['description']}")
            front.append(f"tags: {', '.join(current['tags'])}")
            front.append(f"always: {'true' if current['always'] else 'false'}")
            front.append("---")
            content = "\n".join(front) + "\n\n" + payload.body.strip() + "\n"
        elif payload.name or payload.description is not None:
            meta, body_text = skills_mod._split_frontmatter(content)
            front = ["---"]
            front.append(f"name: {payload.name or current['name']}")
            front.append(f"description: {payload.description if payload.description is not None else current['description']}")
            front.append(f"tags: {', '.join(current['tags'])}")
            front.append(f"always: {'true' if current['always'] else 'false'}")
            front.append("---")
            content = "\n".join(front) + "\n\n" + body_text.strip() + "\n"
        return skills_mod.update_skill(slug, content)
    except skills_mod.SkillError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/skills/{slug}")
def remove_skill(slug: str) -> Dict[str, Any]:
    try:
        skills_mod.delete_skill(slug)
    except skills_mod.SkillError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "skills": skills_mod.list_skills()}


class SkillToggleIn(BaseModel):
    enabled: bool


@app.post("/api/skills/{slug}/toggle")
def toggle_skill(slug: str, payload: SkillToggleIn) -> Dict[str, Any]:
    try:
        return skills_mod.set_enabled(slug, payload.enabled)
    except skills_mod.SkillError as exc:
        raise HTTPException(404, str(exc))


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
@app.get("/api/sessions")
def list_sessions() -> List[Dict[str, Any]]:
    return storage.list_sessions()


class SessionIn(BaseModel):
    title: str = "新对话"
    provider_id: Optional[str] = None


@app.post("/api/sessions")
def create_session(payload: Optional[SessionIn] = None) -> Dict[str, Any]:
    provider_id = payload.provider_id if payload else None
    return storage.create_session((payload.title if payload else "新对话") or "新对话", provider_id)


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    session = storage.get_session(session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    return session


class SessionPatch(BaseModel):
    title: Optional[str] = None
    provider_id: Optional[str] = None


@app.patch("/api/sessions/{session_id}")
def patch_session(session_id: str, payload: SessionPatch) -> Dict[str, Any]:
    session = storage.update_session(session_id, payload.model_dump(exclude_none=True))
    if session is None:
        raise HTTPException(404, "会话不存在")
    return session


@app.delete("/api/sessions/{session_id}")
def remove_session(session_id: str) -> Dict[str, Any]:
    if not storage.delete_session(session_id):
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


@app.post("/api/sessions/{session_id}/clear")
def clear_session(session_id: str) -> Dict[str, Any]:
    session = storage.clear_messages(session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    return session


# --------------------------------------------------------------------------- #
# 对话（SSE 流式）
# --------------------------------------------------------------------------- #
class ChatIn(BaseModel):
    session_id: Optional[str] = None
    message: str = ""
    provider_id: Optional[str] = None


def _sse(event: Dict[str, Any]) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


@app.post("/api/chat")
async def chat(payload: ChatIn):
    cfg = load_config()
    session_id = payload.session_id

    if session_id:
        if storage.get_session(session_id) is None:
            raise HTTPException(404, "会话不存在")
        if payload.provider_id:
            storage.update_session(session_id, {"provider_id": payload.provider_id})
    else:
        session = storage.create_session("新对话", payload.provider_id or cfg.get("active_provider"))
        session_id = session["id"]

    if not payload.message.strip():
        raise HTTPException(400, "消息不能为空")

    # Router 判定：动手类请求派成工单（独立会话、独立上下文），其余就地回答
    dispatch = router.should_dispatch(payload.message)

    async def event_stream():
        # 流式响应在中间件返回后才真正执行，这里自己开一段 rid，方便把一轮对话的日志串起来
        token = logging_setup.new_request_id()
        chat_log = logging_setup.get_logger("chat")
        chat_log.info("对话开始 · session=%s · provider=%s · 派工=%s",
                      session_id, payload.provider_id or "默认", dispatch)
        yield _sse({"type": "session", "session_id": session_id})
        try:
            if dispatch:
                decision = router.decide(payload.message)
                ticket = worker.open_worker(payload.message, provider_id=payload.provider_id)
                chat_log.info("派工 · 工单=%s · 会话=%s · %s",
                              ticket["id"], ticket.get("session"), decision["reason"])
                # 主会话只留一行记录，不复制执行过程：历史不随工单变长
                storage.append_message(session_id, {"role": "user", "content": payload.message})
                storage.append_message(session_id, {
                    "role": "assistant",
                    "content": f"已派工 #{ticket['id']}（{decision['reason']}），执行在独立会话中进行。"})
                yield _sse({"type": "ticket", "id": ticket["id"], "session": ticket.get("session"),
                            "stage": "working/queued", "request": ticket.get("request", "")})
                async for event in worker.run(ticket["id"], payload.provider_id):
                    yield _sse(event)
            else:
                async for event in agent.run_turn(session_id, payload.message, payload.provider_id):
                    yield _sse(event)
        except asyncio.CancelledError:
            chat_log.warning("对话被中断（客户端断开或用户点停止）· session=%s", session_id)
            raise
        except Exception as exc:
            chat_log.exception("对话异常终止 · session=%s · %s", session_id, exc)
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            yield _sse({"type": "done", "session_id": session_id, "ok": False})
        finally:
            logging_setup.reset_request_id(token)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# 工单（Ticket）：面板只关心 person/* 的，健康状态不占地方
# --------------------------------------------------------------------------- #
from . import tasks as task_store


@app.get("/api/tickets")
async def tickets_list() -> Dict[str, Any]:
    items = [{**t, "stage": task_store.stage(t)} for t in task_store.list_tickets()]
    items.sort(key=lambda t: t.get("updated_at", 0), reverse=True)
    return {"tickets": items,
            "pending": sum(1 for i in items if str(i.get("stage", "")).startswith("person/"))}


@app.get("/api/tickets/{ticket_id}")
async def ticket_detail(ticket_id: str) -> Dict[str, Any]:
    ticket = task_store.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "工单不存在")
    return {**ticket, "stage": task_store.stage(ticket)}


@app.post("/api/tickets/{ticket_id}/stop")
async def ticket_stop(ticket_id: str) -> Dict[str, Any]:
    try:
        ticket = task_store.report(ticket_id, "dropped", "人工停止")
    except KeyError:
        raise HTTPException(404, "工单不存在")
    return {**ticket, "stage": task_store.stage(ticket)}
