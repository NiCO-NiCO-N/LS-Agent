"""技能相关工具：让模型发现、读取、执行技能。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from .. import skills as skills_mod
from . import ToolContext, tool


@tool(
    "list_skills",
    "列出本机所有技能（含未启用的），返回名称、描述、是否启用、可用脚本。",
    {"type": "object", "properties": {}},
    label="列出技能",
)
def list_skills_tool(args: Dict[str, Any], ctx: ToolContext) -> str:
    items = skills_mod.list_skills()
    if not items:
        return "当前没有任何技能。可以在右侧「技能」面板新建。"
    lines: List[str] = []
    for s in items:
        flag = "已启用" if s["enabled"] else "未启用"
        scripts = f"；脚本：{', '.join(s['scripts'])}" if s["scripts"] else ""
        lines.append(f"- {s['slug']}（{s['name']}，{flag}）：{s['description'] or '无描述'}{scripts}")
    return "\n".join(lines)


load_skill_schema = {
    "type": "object",
    "properties": {
        "skill": {"type": "string", "description": "技能 slug，例如 research-brief"},
    },
    "required": ["skill"],
}


@tool(
    "load_skill",
    "读取指定技能的完整 SKILL.md 说明，然后按其中的步骤执行。执行技能相关任务前必须先调用。",
    load_skill_schema,
    label="加载技能",
)
def load_skill(args: Dict[str, Any], ctx: ToolContext) -> str:
    slug = str(args.get("skill") or "").strip()
    data = skills_mod.read_skill(slug)
    scripts = f"\n\n可用脚本（用 run_skill_script 运行）：{', '.join(data['scripts'])}" if data["scripts"] else ""
    return f"# 技能：{data['name']}（{data['slug']}）\n\n{data['body'].strip()}{scripts}"


@tool(
    "run_skill_script",
    "运行某个技能目录下的 scripts/*.py 脚本，返回 stdout/stderr。",
    {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "技能 slug"},
            "script": {"type": "string", "description": "脚本文件名，例如 fetch.py"},
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "传给脚本的命令行参数（可选）",
            },
        },
        "required": ["skill", "script"],
    },
    label="运行技能脚本",
)
def run_skill_script(args: Dict[str, Any], ctx: ToolContext) -> str:
    slug = str(args.get("skill") or "").strip()
    script = str(args.get("script") or "").strip()
    data = skills_mod.read_skill(slug)
    if script not in data["scripts"]:
        return f"技能 {slug} 下没有脚本 {script}，可用：{', '.join(data['scripts']) or '无'}"

    skill_dir = Path(data["path"]).parent
    script_path = (skill_dir / "scripts" / script).resolve()
    try:
        script_path.relative_to(skill_dir.resolve())
    except ValueError:
        return "错误：脚本路径非法"

    extra: List[str] = [str(a) for a in (args.get("args") or [])]
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(script_path), *extra],
            cwd=str(skill_dir),
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "脚本执行超时（>60s），已终止。"
    except Exception as exc:
        return f"启动脚本失败：{type(exc).__name__}: {exc}"

    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    parts = [f"退出码 {proc.returncode}"]
    if out:
        parts.append(f"--- stdout ---\n{out[:6000]}")
    if err:
        parts.append(f"--- stderr ---\n{err[:3000]}")
    return "\n".join(parts)
