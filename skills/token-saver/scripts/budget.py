#!/usr/bin/env python3
"""token 预算审计：估一个文件/一段文本/一个会话要花多少 token，并给读法建议。

用法：
    python budget.py app/agent.py README.md      # 估文件
    python budget.py --text "一段文字"            # 估文本
    python budget.py --session 65879c761ae1      # 估会话历史（每步重发量）

只用标准库，不联网、不写文件。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 粗略换算：中文约 1 token / 1.5 字，英文约 1 token / 3.8 字符。
CJK_PER_TOKEN = 1.5
LATIN_PER_TOKEN = 3.8


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
        or 0xF900 <= o <= 0xFAFF
        or 0x3000 <= o <= 0x303F
        or 0xFF00 <= o <= 0xFFEF
    )


def estimate(text: str) -> dict:
    cjk = sum(1 for ch in text if is_cjk(ch))
    other = max(0, len(text) - cjk)
    tokens = int(cjk / CJK_PER_TOKEN + other / LATIN_PER_TOKEN) + 1
    return {
        "chars": len(text),
        "bytes": len(text.encode("utf-8")),
        "cjk": cjk,
        "cjk_ratio": round(cjk / len(text), 2) if text else 0.0,
        "tokens": tokens,
    }


def advice(tokens: int, size_bytes: int) -> str:
    if tokens < 800:
        return "直接读全文"
    if tokens < 3000:
        return "直接读，或先读前 2000B 看结构"
    if tokens < 8000:
        return f"分段读：max_bytes 3000-4000 起步（文件 {size_bytes}B）"
    if tokens < 25000:
        return "脚本检索行号 → offset 精读命中段，别整篇进上下文"
    return "不要整体读：只允许脚本筛选后引用关键几行"


def fmt(n: int) -> str:
    return f"{n:,}"


def audit_text(label: str, text: str, indent: str = "  ") -> int:
    m = estimate(text)
    print(f"{indent}{label}")
    print(
        f"{indent}  {fmt(m['chars'])} 字 · {fmt(m['bytes'])} B · 中文 {int(m['cjk_ratio'] * 100)}% "
        f"· 约 {fmt(m['tokens'])} token"
    )
    print(f"{indent}  → {advice(m['tokens'], m['bytes'])}")
    return m["tokens"]


def project_root() -> Path | None:
    """脚本在 skills/<slug>/scripts/ 下时，往上第三层就是项目根。"""
    parents = Path(__file__).resolve().parents
    for cand in list(parents[3:4]) + [Path.cwd().resolve()]:
        if (cand / "data").is_dir() or (cand / "app").is_dir():
            return cand
    return None


def search_bases() -> list[Path]:
    """相对路径的查找顺序：当前目录 → 项目根 → workspace。"""
    bases = [Path.cwd().resolve()]
    root = project_root()
    if root is not None:
        bases += [root, root / "workspace"]
    return bases


def resolve_path(raw: str) -> Path | None:
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p if p.exists() else None
    for base in search_bases():
        cand = base / p
        if cand.exists():
            return cand
    return None


def find_sessions_dir() -> Path | None:
    # 1) 当前目录 / 祖先目录下的 data/sessions
    here = Path.cwd().resolve()
    for base in [here, *here.parents]:
        cand = base / "data" / "sessions"
        if cand.is_dir():
            return cand
    # 2) 从脚本位置往上找（skills/token-saver/scripts → 项目根）
    for base in Path(__file__).resolve().parents:
        cand = base / "data" / "sessions"
        if cand.is_dir():
            return cand
    env = os.environ.get("LS_AGENT_SESSIONS")
    if env and Path(env).is_dir():
        return Path(env)
    return None


def audit_session(session_id: str) -> int:
    d = find_sessions_dir()
    if d is None:
        print("找不到 data/sessions 目录；用 --sessions-dir 指定或设置 LS_AGENT_SESSIONS")
        return 2
    path = d / f"{session_id}.json"
    if not path.exists():
        print(f"会话文件不存在：{path}")
        return 2
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"读取失败：{type(exc).__name__}: {exc}")
        return 2

    msgs = data.get("messages") or []
    rows = []
    total = 0
    for i, m in enumerate(msgs, 1):
        content = m.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False) if content is not None else ""
        if m.get("tool_calls"):
            content += json.dumps(m["tool_calls"], ensure_ascii=False)
        t = estimate(content)["tokens"]
        total += t
        rows.append((t, i, str(m.get("role") or "?"), len(content)))

    print(f"会话 {session_id} · {len(msgs)} 条消息 · 历史约 {fmt(total)} token")
    print("注意：每多走一步工具，这份历史就要再重发一次；5 步 ≈ 5 倍。")
    if not rows:
        return 0
    rows.sort(reverse=True)
    print("最贵的 5 条：")
    for t, i, role, chars in rows[:5]:
        share = t / total * 100 if total else 0
        print(f"  #{i:<4} {role:<9} 约 {fmt(t):>7} token（{share:4.1f}%）· {fmt(chars)} 字")
    if total > 20000:
        print("建议：开新会话（把结论写进 notes/ 再重新开始），或让工具结果先落盘。")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(add_help=True, description="估 token 并给读法建议")
    ap.add_argument("paths", nargs="*", help="要检查的文件")
    ap.add_argument("--text", default="", help="直接估一段文本")
    ap.add_argument("--session", default="", help="会话 id（读 data/sessions/<id>.json）")
    ap.add_argument("--sessions-dir", default="", help="会话目录")
    ap.add_argument("--max-files", type=int, default=12, help="最多检查几个文件")
    args = ap.parse_args(argv)

    if args.sessions_dir:
        os.environ["LS_AGENT_SESSIONS"] = args.sessions_dir
    if args.session:
        return audit_session(args.session)

    total = 0
    if args.text:
        total += audit_text("文本", args.text)

    files = args.paths[: max(1, args.max_files)]
    if len(args.paths) > len(files):
        print(f"（只检查前 {len(files)} 个文件，其余跳过）")

    for raw in files:
        p = resolve_path(raw)
        if p is None:
            print(f"  跳过（找不到）：{raw}（已试过 cwd / 项目根 / workspace）")
            continue
        if p.is_dir():
            print(f"  跳过（目录，先用 list_dir 过滤）：{raw}")
            continue
        size = p.stat().st_size
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"  跳过（{type(exc).__name__}）：{raw}")
            continue
        total += audit_text(str(raw), text)

    if total:
        print(f"\n合计约 {fmt(total)} token。")
        if total > 8000:
            print("超过 8000 token：别一次性全读进上下文，按上面的建议分段或用脚本筛。")
    elif not files:
        print("没给文件也没给 --text / --session，无事可做。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
