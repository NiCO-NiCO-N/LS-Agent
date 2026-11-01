"""模型接入层。

支持三类 provider：
- openai     : 所有 OpenAI 兼容接口（OpenAI / DeepSeek / Moonshot / 通义 / Ollama / vLLM ...）
- anthropic  : Claude Messages API
- mock       : 本地演示模型，用来在没有 Key 的情况下跑通「工具调用」全链路

对外只暴露 astream()：异步产出 delta / reasoning / final 事件。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from .config import resolve_api_key
from .logging_setup import get_logger

log = get_logger("llm")

TIMEOUT = httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=20.0)

ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    pass


# 常见 HTTP 错误的可操作解释：把上游那句看不懂的报错翻成人话
_HTTP_HINTS: Dict[int, str] = {
    400: "请求被拒绝：模型名或 Base URL 与这个服务不匹配",
    401: "认证失败：API Key 无效/过期，或该网关不认当前的鉴权方式",
    403: "无权限：这个 Key 不能访问该模型，或账户未开通/已欠费",
    404: "接口路径不存在：检查 Base URL 是否多了或少了 /v1",
    408: "上游处理超时",
    422: "参数不被接受：检查模型名、max_tokens 等字段",
    429: "触发限流或额度用尽：稍后重试，或换 Key / 降低频率",
    500: "上游服务内部错误",
    502: "上游网关错误",
    503: "上游服务不可用或过载",
    504: "上游网关超时",
}


def explain_http_error(status: int, body: str, provider: Dict[str, Any]) -> str:
    """把上游错误包装成「原因 + 当前配置 + 下一步」三行，方便直接照着改。"""
    hint = _HTTP_HINTS.get(status) or ("上游不可用" if status >= 500 else "请求失败")
    ptype = (provider.get("type") or "openai").lower()
    auth_header = "x-api-key" if ptype == "anthropic" else "Authorization: Bearer"
    detail = " ".join((body or "").split())[:300]
    lines = [
        f"模型接口返回 HTTP {status}：{hint}",
        f"当前 provider：{provider.get('id')}（type={ptype}，鉴权头 {auth_header}，"
        f"base={provider.get('base_url') or '-'}，model={provider.get('model') or '-'}）",
    ]
    if status in (401, 403):
        lines.append(
            "下一步：在「设置 → 模型」里重新粘贴 Key 并点「测试连接」；"
            "如果 Key 确认正确，多半是该网关要求另一种鉴权方式——"
            "OpenAI 兼容网关请把 provider 类型改成 openai（用 Bearer），Claude 官方则用 anthropic。"
        )
    elif status == 404:
        lines.append("下一步：Base URL 通常以 /v1 结尾（Claude 官方为 https://api.anthropic.com），改完再点「测试连接」。")
    elif status == 429:
        lines.append("下一步：等几十秒再发一次；长期超限请升级套餐或换一个 Key。")
    elif status >= 500:
        lines.append("下一步：这是上游的问题，稍后重试即可。")
    if detail:
        lines.append(f"上游原始返回：{detail}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 消息格式转换
# --------------------------------------------------------------------------- #
def sanitize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """内部存储的消息 -> OpenAI 格式（去掉 UI 用的额外字段）。"""
    out: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant":
            item: Dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                item["tool_calls"] = m["tool_calls"]
            out.append(item)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id") or "",
                    "content": m.get("content") or "",
                }
            )
    return out


def to_anthropic_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            text = m.get("content") or ""
            if not text:
                continue
            out.append({"role": "user", "content": [{"type": "text", "text": text}]})
        elif role == "assistant":
            blocks: List[Dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except Exception:
                    args = {"_raw": raw}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or "",
                        "name": fn.get("name") or "",
                        "input": args if isinstance(args, dict) else {"value": args},
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content": m.get("content") or "",
            }
            last = out[-1] if out else None
            if (
                last
                and last.get("role") == "user"
                and isinstance(last.get("content"), list)
                and last["content"]
                and last["content"][0].get("type") == "tool_result"
            ):
                last["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


def to_anthropic_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for t in tools or []:
        fn = t.get("function") or {}
        out.append(
            {
                "name": fn.get("name"),
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return [t for t in out if t["name"]]


# --------------------------------------------------------------------------- #
# OpenAI 兼容流
# --------------------------------------------------------------------------- #
async def _stream_openai(
    provider: Dict[str, Any],
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    system_prompt: str,
) -> AsyncIterator[Dict[str, Any]]:
    base = (provider.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    url = base + "/chat/completions"
    key = resolve_api_key(provider)

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key

    payload: Dict[str, Any] = {
        "model": provider.get("model") or "gpt-4o-mini",
        "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else [])
        + sanitize_messages(messages),
        "stream": True,
        "temperature": float(provider.get("temperature") or 0.7),
    }
    if provider.get("max_tokens"):
        payload["max_tokens"] = int(provider["max_tokens"])
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    calls: Dict[int, Dict[str, str]] = {}

    log.debug("→ POST %s · model=%s · messages=%d · tools=%d · key=%s", url, payload.get("model"), len(payload["messages"]), len(tools or []), "有" if key else "无")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                log.warning("模型接口返回 HTTP %s · provider=%s · url=%s · body=%s", resp.status_code, provider.get("id"), url, " ".join(body.split())[:300])
                raise LLMError(explain_http_error(resp.status_code, body, provider))

            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data in ("[DONE]", ""):
                    if data == "[DONE]":
                        break
                    continue
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue

                if isinstance(chunk, dict) and chunk.get("error"):
                    err = chunk["error"]
                    log.error("模型流式返回错误块：%s", err)
                    raise LLMError(str(err.get("message") if isinstance(err, dict) else err))

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield {"type": "reasoning", "text": reasoning}

                text = delta.get("content")
                if text:
                    content_parts.append(text)
                    yield {"type": "delta", "text": text}

                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index") or 0)
                    slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]

    tool_calls = []
    for idx in sorted(calls):
        slot = calls[idx]
        if not slot["name"]:
            continue
        tool_calls.append(
            {
                "id": slot["id"] or f"call_{idx}",
                "name": slot["name"],
                "arguments": slot["arguments"] or "{}",
            }
        )

    yield {
        "type": "final",
        "content": "".join(content_parts),
        "reasoning": "".join(reasoning_parts),
        "tool_calls": tool_calls,
    }


# --------------------------------------------------------------------------- #
# Anthropic 流
# --------------------------------------------------------------------------- #
async def _stream_anthropic(
    provider: Dict[str, Any],
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    system_prompt: str,
) -> AsyncIterator[Dict[str, Any]]:
    base = (provider.get("base_url") or "https://api.anthropic.com").rstrip("/")
    url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    key = resolve_api_key(provider)
    if not key:
        log.error("Anthropic provider %s 没有 API Key", provider.get("id"))
        raise LLMError("未配置 Anthropic API Key（设置里填写，或设置环境变量 ANTHROPIC_API_KEY）")

    payload: Dict[str, Any] = {
        "model": provider.get("model") or "claude-sonnet-4-20250514",
        "max_tokens": int(provider.get("max_tokens") or 4096),
        "messages": to_anthropic_messages(messages),
        "stream": True,
        "temperature": float(provider.get("temperature") or 0.7),
    }
    if system_prompt:
        payload["system"] = system_prompt
    if tools:
        payload["tools"] = to_anthropic_tools(tools)

    headers = {
        "Content-Type": "application/json",
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
    }

    content_parts: List[str] = []
    blocks: Dict[int, Dict[str, Any]] = {}

    log.debug("→ POST %s · model=%s · messages=%d · tools=%d", url, payload.get("model"), len(payload.get("messages") or []), len(tools or []))

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                log.warning("Claude 接口返回 HTTP %s · provider=%s · body=%s", resp.status_code, provider.get("id"), " ".join(body.split())[:300])
                raise LLMError(explain_http_error(resp.status_code, body, provider))

            async for line in resp.aiter_lines():
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except Exception:
                    continue

                etype = event.get("type")
                if etype == "error":
                    raise LLMError(str(event.get("error") or event))

                if etype == "content_block_start":
                    idx = int(event.get("index") or 0)
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        blocks[idx] = {
                            "id": block.get("id") or f"tool_{idx}",
                            "name": block.get("name") or "",
                            "arguments": "",
                        }
                    elif block.get("type") == "text" and block.get("text"):
                        content_parts.append(block["text"])
                        yield {"type": "delta", "text": block["text"]}

                elif etype == "content_block_delta":
                    idx = int(event.get("index") or 0)
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        content_parts.append(delta["text"])
                        yield {"type": "delta", "text": delta["text"]}
                    elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                        yield {"type": "reasoning", "text": delta["thinking"]}
                    elif delta.get("type") == "input_json_delta":
                        slot = blocks.setdefault(idx, {"id": f"tool_{idx}", "name": "", "arguments": ""})
                        slot["arguments"] += delta.get("partial_json") or ""

                elif etype == "message_stop":
                    break

    tool_calls = []
    for idx in sorted(blocks):
        slot = blocks[idx]
        if not slot.get("name"):
            continue
        tool_calls.append(
            {
                "id": slot["id"],
                "name": slot["name"],
                "arguments": slot["arguments"] or "{}",
            }
        )

    yield {
        "type": "final",
        "content": "".join(content_parts),
        "reasoning": "",
        "tool_calls": tool_calls,
    }


# --------------------------------------------------------------------------- #
# Mock 流（无需 Key 即可体验完整 Agent 流程）
# --------------------------------------------------------------------------- #
async def _stream_mock(
    provider: Dict[str, Any],
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    system_prompt: str,
) -> AsyncIterator[Dict[str, Any]]:
    last = messages[-1] if messages else {}
    already_called = last.get("role") == "tool"

    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = (m.get("content") or "").strip()
            break

    tool_names = {((t.get("function") or {}).get("name") or "") for t in tools or []}

    if not already_called and user_text and "get_time" in tool_names:
        reply = "我先调用时间工具确认一下当前时间。"
        for ch in reply:
            yield {"type": "delta", "text": ch}
            await asyncio.sleep(0.008)
        yield {
            "type": "final",
            "content": reply,
            "reasoning": "",
            "tool_calls": [{"id": "call_mock_time", "name": "get_time", "arguments": "{}"}],
        }
        return

    reply = (
        "我是 **Mock 演示模型**，当前没有接入真实大模型，但整条链路（前端 → Agent 循环 → 工具调用 → 技能加载）都是通的。\n\n"
        "接下来可以：\n"
        "1. 打开右侧 **设置 → 模型**，填入 OpenAI / DeepSeek / Claude / Ollama 的 Base URL、Model、API Key；\n"
        "2. 在 **技能** 面板里启用或新建 SKILL.md 技能；\n"
        f"3. 你刚才说：「{user_text[:60]}」。"
    )
    for ch in reply:
        yield {"type": "delta", "text": ch}
        await asyncio.sleep(0.006)
    yield {"type": "final", "content": reply, "reasoning": "", "tool_calls": []}


# --------------------------------------------------------------------------- #
# 统一出口
# --------------------------------------------------------------------------- #
async def astream(
    provider: Dict[str, Any],
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    system_prompt: str,
) -> AsyncIterator[Dict[str, Any]]:
    if not provider:
        raise LLMError("没有可用的模型提供商，请先在设置里添加")

    ptype = (provider.get("type") or "openai").lower()
    if ptype == "mock":
        gen = _stream_mock(provider, messages, tools, system_prompt)
    elif ptype == "anthropic":
        gen = _stream_anthropic(provider, messages, tools, system_prompt)
    else:
        gen = _stream_openai(provider, messages, tools, system_prompt)

    async for event in gen:
        yield event
