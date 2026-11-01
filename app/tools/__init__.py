"""工具注册表：内置工具 + 技能桥接工具都在这里汇总。"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import WORKSPACE_DIR
from ..logging_setup import get_logger

log = get_logger("tools")


@dataclass
class ToolContext:
    session_id: str = ""
    workspace: Path = field(default_factory=lambda: WORKSPACE_DIR)


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any], ToolContext], Any]
    label: str = ""


REGISTRY: Dict[str, Tool] = {}


def tool(name: str, description: str, parameters: Optional[Dict[str, Any]] = None, label: str = ""):
    def deco(fn: Callable):
        REGISTRY[name] = Tool(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            handler=fn,
            label=label or name,
        )
        return fn

    return deco


def tool_schemas(disabled: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    disabled_set = set(disabled or [])
    out: List[Dict[str, Any]] = []
    for name, t in REGISTRY.items():
        if name in disabled_set:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
        )
    return out


def tool_catalog() -> List[Dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "label": t.label,
            "parameters": t.parameters,
        }
        for t in REGISTRY.values()
    ]


async def execute_tool(
    name: str,
    arguments: Any,
    ctx: ToolContext,
    max_chars: int = 12000,
) -> tuple[str, bool]:
    t = REGISTRY.get(name)
    if t is None:
        log.warning("调用不存在的工具：%s", name)
        return f"错误：不存在名为 {name} 的工具", False

    if isinstance(arguments, str):
        raw = arguments.strip() or "{}"
        try:
            args = json.loads(raw)
        except Exception:
            log.warning("工具 %s 参数不是合法 JSON：%s", name, raw[:200])
            return f"错误：工具参数不是合法 JSON：{raw[:300]}", False
    elif isinstance(arguments, dict):
        args = arguments
    elif arguments is None:
        args = {}
    else:
        log.warning("工具 %s 参数类型不支持：%s", name, type(arguments).__name__)
        return "错误：工具参数格式不支持", False

    if not isinstance(args, dict):
        log.warning("工具 %s 参数不是 JSON 对象：%s", name, type(args).__name__)
        return "错误：工具参数必须是 JSON 对象", False

    try:
        if inspect.iscoroutinefunction(t.handler):
            result = await t.handler(args, ctx)
        else:
            result = await asyncio.to_thread(t.handler, args, ctx)
    except Exception as exc:  # 工具自身的异常不应该打断整个 Agent 循环
        log.exception("工具 %s 执行失败：%s: %s", name, type(exc).__name__, exc)
        return f"工具 {name} 执行失败：{type(exc).__name__}: {exc}", False

    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…（结果已截断，共 {len(text)} 字符）"
    return text, True


# 导入即注册
from . import builtin  # noqa: E402,F401
from . import skill_tools  # noqa: E402,F401
