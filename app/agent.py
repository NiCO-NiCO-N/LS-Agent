"""Agent 循环：模型 -> 工具 -> 模型 ... 直到给出最终回答。"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Dict, List

from . import llm, logging_setup
from .config import WORKSPACE_DIR, get_provider, load_config
from .skills import build_system_prompt
from .storage import append_message, get_session, update_session
from .tools import ToolContext, execute_tool, tool_schemas

log = logging_setup.get_logger("agent")


def _preview(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _action(name: str, raw_args: str, result: str, ok: bool, verbose: bool = False) -> str:
    """把一次工具调用翻译成人话：做了什么、动的是哪个位置、写了多少。"""
    try:
        args = json.loads(raw_args or "{}")
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}
    path = str(args.get("path") or args.get("file") or args.get("filename") or "").strip()
    target = f" {path}" if path else ""
    verb = "" if ok else "尝试"

    if name == "write_file":
        body = args.get("content")
        body = body if isinstance(body, str) else ""
        head = _preview(body, 40) if (body and verbose) else ""
        return (
            f"{verb}写入文件{target} · {len(body)} 字 / {_size(len(body.encode('utf-8')))}"
            + (f" · 开头「{head}」" if head else " · 内容为空")
        )
    if name == "read_file":
        return f"{verb}读取文件{target} · 返回 {len(result)} 字"
    if name == "list_dir":
        return f"{verb}查看目录{target or '（工作区根）'} · 返回 {len(result)} 字"
    if name == "run_python":
        code = str(args.get("code") or "")
        return f"{verb}执行 Python 代码{('（' + _preview(code, 50) + '）') if code else ''} · 返回 {len(result)} 字"
    if name == "http_get":
        return f"{verb}打开网页 {args.get('url') or '-'} · 返回 {len(result)} 字"
    if name == "web_search":
        return f"{verb}联网搜索「{_preview(str(args.get('query') or ''), 40)}」 · 返回 {len(result)} 字"
    if name == "load_skill":
        return f"{verb}加载技能 {args.get('name') or '-'} · 返回 {len(result)} 字"
    if name == "run_skill_script":
        return f"{verb}运行技能脚本 {args.get('skill') or '-'} / {args.get('script') or '-'} · 返回 {len(result)} 字"
    if name == "list_skills":
        return f"{verb}列出技能 · 返回 {len(result)} 字"
    if name == "get_time":
        return f"{verb}读取本机时间"
    if name == "calculate":
        return f"{verb}计算 {args.get('expression') or '-'}"
    return f"{verb}调用 {name}{target} · 返回 {len(result)} 字"


def _assistant_message(content: str, tool_calls: List[Dict[str, Any]], reasoning: str = "") -> Dict[str, Any]:
    msg: Dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
            }
            for c in tool_calls
        ]
    if reasoning:
        msg["reasoning"] = reasoning
    return msg


async def run_turn(
    session_id: str,
    user_text: str,
    provider_id: str | None = None,
) -> AsyncIterator[Dict[str, Any]]:
    cfg = load_config()
    session = get_session(session_id)
    if session is None:
        yield {"type": "error", "message": "会话不存在，请新建对话"}
        return

    provider = get_provider(provider_id or session.get("provider_id") or cfg.get("active_provider"))
    if provider is None:
        log.error("会话 %s 没有可用的模型提供商", session_id)
        yield {"type": "error", "message": "没有可用的模型提供商，请到「设置 → 模型」添加"}
        return

    if provider_id and session.get("provider_id") != provider_id:
        update_session(session_id, {"provider_id": provider["id"]})

    text = (user_text or "").strip()
    if text:
        append_message(session_id, {"role": "user", "content": text})

    system_prompt = build_system_prompt(cfg)
    tool_schema = tool_schemas(cfg.get("disabled_tools") or [])
    max_steps = max(1, int(cfg.get("max_steps") or 8))
    max_result_chars = max(500, int(cfg.get("max_tool_result_chars") or 12000))
    ctx = ToolContext(session_id=session_id, workspace=WORKSPACE_DIR)

    usable_tools = {t["function"]["name"] for t in tool_schema}
    log.info(
        "会话 %s 开始一轮 · provider=%s(%s) · 工具 %d 个 · 上限 %d 步%s",
        session_id,
        provider["id"],
        provider.get("model") or "-",
        len(usable_tools),
        max_steps,
        f" · 用户输入「{_preview(text)}」" if cfg.get("log_prompts") else f" · 输入 {len(text)} 字",
    )

    for step in range(1, max_steps + 1):
        fresh = get_session(session_id) or session
        api_messages = llm.sanitize_messages(fresh.get("messages") or [])

        yield {"type": "step", "step": step, "max_steps": max_steps, "provider": provider["id"]}

        final: Dict[str, Any] | None = None
        started = time.perf_counter()
        first_token: float | None = None
        log.debug("第 %d/%d 步 · 上下文 %d 条消息，请求模型 %s", step, max_steps, len(api_messages), provider.get("model") or "-")
        try:
            async for event in llm.astream(provider, api_messages, tool_schema, system_prompt):
                etype = event.get("type")
                if etype == "delta":
                    if first_token is None:
                        first_token = time.perf_counter()
                    yield {"type": "delta", "text": event.get("text", "")}
                elif etype == "reasoning":
                    if first_token is None:
                        first_token = time.perf_counter()
                    yield {"type": "reasoning", "text": event.get("text", "")}
                elif etype == "final":
                    final = event
        except llm.LLMError as exc:
            log.error("第 %d 步调用模型失败：%s", step, exc)
            yield {"type": "error", "message": str(exc)}
            yield {"type": "done", "session_id": session_id, "ok": False}
            return
        except Exception as exc:  # 网络抖动、解析失败等
            log.exception("第 %d 步调用模型出现异常：%s", step, exc)
            yield {"type": "error", "message": f"调用模型失败：{type(exc).__name__}: {exc}"}
            yield {"type": "done", "session_id": session_id, "ok": False}
            return

        if final is None:
            log.error("第 %d 步模型没有返回内容", step)
            yield {"type": "error", "message": "模型没有返回内容"}
            yield {"type": "done", "session_id": session_id, "ok": False}
            return

        total_ms = (time.perf_counter() - started) * 1000
        first_ms = (first_token - started) * 1000 if first_token else total_ms
        log.info(
            "第 %d 步模型返回 · 文本 %d 字 · 工具调用 %d 个 · 首字 %.0f ms · 合计 %.0f ms",
            step,
            len(final.get("content") or ""),
            len(final.get("tool_calls") or []),
            first_ms,
            total_ms,
        )

        calls = [c for c in (final.get("tool_calls") or []) if c.get("name") in usable_tools]
        dropped = [c for c in (final.get("tool_calls") or []) if c.get("name") not in usable_tools]

        assistant = _assistant_message(final.get("content", ""), calls, final.get("reasoning", ""))
        stored = append_message(session_id, assistant)
        yield {"type": "assistant", "message": stored}

        if dropped:
            names = "、".join(sorted({c.get("name") or "?" for c in dropped}))
            log.warning("模型请求了不可用的工具：%s（已禁用或不存在）", names)
            yield {"type": "tool_result", "id": "dropped", "name": names, "ok": False, "result": "该工具已被禁用或不存在"}

        if not calls:
            latest = get_session(session_id) or {}
            log.info("会话 %s 本轮结束 · 共 %d 步 · 成功", session_id, step)
            yield {"type": "done", "session_id": session_id, "ok": True, "title": latest.get("title")}
            return

        for call in calls:
            call_id = call["id"]
            name = call["name"]
            raw_args = call.get("arguments") or "{}"
            yield {"type": "tool_call", "id": call_id, "name": name, "arguments": raw_args}

            tool_started = time.perf_counter()
            result, ok = await execute_tool(name, raw_args, ctx, max_chars=max_result_chars)
            tool_ms = (time.perf_counter() - tool_started) * 1000
            action = _action(name, raw_args, result, ok, bool(cfg.get("log_prompts")))
            if ok:
                log.info("行动 %s · 工具 %s · %.0f ms · 成功", action, name, tool_ms)
            else:
                log.warning("行动 %s · 工具 %s · %.0f ms · 失败：%s", action, name, tool_ms, _preview(result, 200))
            append_message(
                session_id,
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": result,
                    "ok": ok,
                },
            )
            yield {
                "type": "tool_result",
                "id": call_id,
                "name": name,
                "ok": ok,
                "action": action,
                "result": result if len(result) <= 8000 else result[:8000] + "\n…（前端展示已截断）",
            }

    latest = get_session(session_id) or {}
    log.warning("会话 %s 达到最大工具步数 %d，强行收尾", session_id, max_steps)
    yield {
        "type": "notice",
        "message": f"已达到最大工具步数（{max_steps}），先给出阶段性结论。可以继续追问或调高步数上限。",
    }
    yield {"type": "done", "session_id": session_id, "ok": True, "title": latest.get("title")}
