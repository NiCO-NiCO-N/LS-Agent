"""内置工具：时间、计算、文件、HTTP、联网搜索、Python 执行。"""
from __future__ import annotations

import ast
import html
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx

from ..config import WORKSPACE_DIR
from . import ToolContext, tool

MAX_FILE_BYTES = 200_000


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #
def safe_path(raw: str, ctx: ToolContext) -> Path:
    root = Path(ctx.workspace or WORKSPACE_DIR).resolve()
    text = (raw or ".").strip().replace("\\", "/")
    if text in ("", "."):
        target = root
    else:
        candidate = Path(text)
        target = candidate.resolve() if candidate.is_absolute() else (root / text).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise PermissionError(f"路径超出工作区：{raw}（只允许操作 {root}）")
    return target


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #
@tool(
    "get_time",
    "获取当前本地时间、UTC 时间与时间戳。",
    {"type": "object", "properties": {}},
    label="当前时间",
)
def get_time(args: Dict[str, Any], ctx: ToolContext) -> str:
    now = datetime.now().astimezone()
    return (
        f"本地时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"UTC 时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"ISO8601：{now.isoformat()}\n"
        f"时间戳：{int(now.timestamp())}\n"
        f"星期：{'一二三四五六日'[now.weekday()]}"
    )


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #
_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}
_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "int": int,
    "float": float,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "pi": math.pi,
    "e": math.e,
}


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.Name) and node.id in _FUNCS:
        return _FUNCS[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        fn = _FUNCS[node.func.id]
        if not callable(fn):
            raise ValueError(f"{node.func.id} 不是函数")
        return fn(*[_eval_node(a) for a in node.args])
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval_node(e) for e in node.elts]
    raise ValueError("表达式包含不支持的语法")


@tool(
    "calculate",
    "计算数学表达式，例如 (12*3+4)/2、sqrt(2)、sum([1,2,3])。",
    {
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "要计算的数学表达式"},
        },
        "required": ["expression"],
    },
    label="计算",
)
def calculate(args: Dict[str, Any], ctx: ToolContext) -> str:
    expression = str(args.get("expression") or "").strip()
    if not expression:
        return "错误：expression 不能为空"
    if len(expression) > 500:
        return "错误：表达式过长"
    tree = ast.parse(expression, mode="eval")
    value = _eval_node(tree)
    return f"{expression} = {value}"


# --------------------------------------------------------------------------- #
# 文件系统
# --------------------------------------------------------------------------- #
@tool(
    "list_dir",
    "列出工作区中某个目录的文件与子目录。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的目录，默认 ."},
        },
    },
    label="列目录",
)
def list_dir(args: Dict[str, Any], ctx: ToolContext) -> str:
    target = safe_path(str(args.get("path") or "."), ctx)
    if not target.exists():
        return f"目录不存在：{args.get('path')}"
    if not target.is_dir():
        return f"不是目录：{args.get('path')}"
    rows = []
    for p in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))[:200]:
        if p.is_dir():
            rows.append(f"[dir]  {p.name}/")
        else:
            rows.append(f"[file] {p.name}  ({p.stat().st_size} B)")
    if not rows:
        return f"{target} 是空目录"
    return "\n".join(rows)


@tool(
    "read_file",
    "读取工作区中的文本文件内容。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "max_bytes": {"type": "integer", "description": "最多读取字节数，默认 20000"},
        },
        "required": ["path"],
    },
    label="读文件",
)
def read_file(args: Dict[str, Any], ctx: ToolContext) -> str:
    target = safe_path(str(args.get("path") or ""), ctx)
    if not target.exists():
        return f"文件不存在：{args.get('path')}"
    if target.is_dir():
        return f"{args.get('path')} 是目录，请用 list_dir"
    limit = min(int(args.get("max_bytes") or 20000), MAX_FILE_BYTES)
    data = target.read_bytes()[:limit]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"{target.name} 不是 UTF-8 文本文件，共 {target.stat().st_size} 字节"
    suffix = "" if target.stat().st_size <= limit else f"\n…（仅显示前 {limit} 字节）"
    return text + suffix


@tool(
    "write_file",
    "把文本写入工作区中的文件（默认覆盖，append=true 时追加）。会自动创建上级目录。",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "content": {"type": "string", "description": "要写入的文本内容"},
            "append": {"type": "boolean", "description": "是否追加写入，默认 false"},
        },
        "required": ["path", "content"],
    },
    label="写文件",
)
def write_file(args: Dict[str, Any], ctx: ToolContext) -> str:
    target = safe_path(str(args.get("path") or ""), ctx)
    if target == Path(ctx.workspace).resolve():
        return "错误：path 必须是文件路径"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = str(args.get("content") or "")
    mode = "a" if args.get("append") else "w"
    with open(target, mode, encoding="utf-8", newline="") as fh:
        fh.write(content)
    return f"已{'追加' if mode == 'a' else '写入'} {target.relative_to(Path(ctx.workspace).resolve())}（{len(content)} 字符）"


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #
_FETCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 lsagent/0.1"
)
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


def _decode_body(resp: httpx.Response) -> str:
    """按响应头声明的编码解码；没声明就在 utf-8 / gb18030 里挑能解通的那个。"""
    if "charset=" in resp.headers.get("content-type", "").lower():
        return resp.text
    for enc in ("utf-8", "gb18030"):
        try:
            return resp.content.decode(enc)
        except UnicodeDecodeError:
            continue
    return resp.content.decode("utf-8", "replace")


def _html_to_text(raw: str) -> str:
    # 优先取正文区（<article>/<main>），减少导航和侧栏噪声
    for pattern in (r"<article[^>]*>(.*?)</article>", r"<main[^>]*>(.*?)</main>"):
        match = re.search(pattern, raw, re.S | re.I)
        if match and len(match.group(1)) > 800:
            raw = match.group(1)
            break
    raw = _TAG_RE.sub(" ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    lines = [line.strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


@tool(
    "http_get",
    "抓取一个公开网页或 API 的文本内容（HTML 会自动转成纯文本）。",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http/https 地址"},
            "max_chars": {"type": "integer", "description": "最多返回字符数，默认 6000"},
        },
        "required": ["url"],
    },
    label="抓网页",
)
async def http_get(args: Dict[str, Any], ctx: ToolContext) -> str:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "错误：url 必须是 http/https 地址"
    limit = min(int(args.get("max_chars") or 6000), 20000)
    headers = {"User-Agent": _FETCH_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        return f"请求失败：{type(exc).__name__}: {exc}"

    ctype = resp.headers.get("content-type", "")
    body = _decode_body(resp)
    if "html" in ctype or body.lstrip().startswith("<"):
        body = _html_to_text(body)
    if len(body) > limit:
        body = body[:limit] + f"\n…（已截断，原始长度 {len(resp.text)} 字符）"
    return f"HTTP {resp.status_code}  {url}\n\n{body}"


# --------------------------------------------------------------------------- #
# 联网搜索（Bing：RSS 优先，HTML 兜底）
# --------------------------------------------------------------------------- #
_SPACE_RE = re.compile(r"\s+")


def _plain(raw: str) -> str:
    """去标签、去 CDATA、反转义实体、压平空白。"""
    text = (raw or "").replace("<![CDATA[", "").replace("]]>", "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return _SPACE_RE.sub(" ", text).strip()


def _parse_bing_rss(xml: str, limit: int) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for chunk in re.findall(r"<item>(.*?)</item>", xml, re.S):
        link = re.search(r"<link>(.*?)</link>", chunk, re.S)
        url = _plain(link.group(1)) if link else ""
        if not url.startswith("http"):
            continue
        title = re.search(r"<title>(.*?)</title>", chunk, re.S)
        desc = re.search(r"<description>(.*?)</description>", chunk, re.S)
        items.append(
            {
                "title": _plain(title.group(1)) if title else url,
                "url": url,
                "snippet": _plain(desc.group(1)) if desc else "",
            }
        )
        if len(items) >= limit:
            break
    return items


def _parse_bing_html(text: str, limit: int) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for block in re.split(r'<li class="b_algo', text)[1:]:
        link = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url = html.unescape(link.group(1))
        title = _plain(link.group(2))
        if not title or not url.startswith("http") or "bing.com" in url:
            continue
        snip = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        items.append({"title": title, "url": url, "snippet": _plain(snip.group(1)) if snip else ""})
        if len(items) >= limit:
            break
    return items


def _format_results(query: str, items: List[Dict[str, str]]) -> str:
    lines = [f"搜索：{query} · {len(items)} 条结果"]
    for idx, item in enumerate(items, 1):
        lines.append(f"\n{idx}. {item['title']}")
        lines.append(f"   {item['url']}")
        if item["snippet"]:
            lines.append(f"   {item['snippet'][:300]}")
    lines.append("\n（摘要来自搜索结果页，引用前用 http_get 打开原文核对）")
    return "\n".join(lines)


@tool(
    "web_search",
    "联网搜索关键词，返回标题、真实链接和摘要。需要正文时再用 http_get 打开链接。",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，像用户那样写，中英文都行"},
            "max_results": {"type": "integer", "description": "结果条数，默认 6，最多 10"},
            "site": {"type": "string", "description": "只搜某个站点，例如 github.com"},
        },
        "required": ["query"],
    },
    label="联网搜索",
)
async def web_search(args: Dict[str, Any], ctx: ToolContext) -> str:
    query = _SPACE_RE.sub(" ", str(args.get("query") or "")).strip()
    if not query:
        return "错误：query 不能为空"
    limit = max(1, min(int(args.get("max_results") or 6), 10))
    site = str(args.get("site") or "").strip()
    if site:
        query = f"site:{site} {query}"
    headers = {"User-Agent": _FETCH_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    problems: List[str] = []
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0), follow_redirects=True, headers=headers
        ) as client:
            # 首选 RSS：结构干净，标题/链接/摘要都不会被页面样式污染
            try:
                resp = await client.get(
                    "https://www.bing.com/search",
                    params={"q": query, "format": "rss", "count": str(limit)},
                )
                if resp.status_code == 200:
                    items = _parse_bing_rss(_decode_body(resp), limit)
                    if items:
                        return _format_results(query, items)
                    problems.append("Bing RSS：0 条结果")
                else:
                    problems.append(f"Bing RSS：HTTP {resp.status_code}")
            except Exception as exc:
                problems.append(f"Bing RSS：{type(exc).__name__}: {exc}")

            # 兜底：解析搜索结果页 HTML
            try:
                resp = await client.get(
                    "https://www.bing.com/search", params={"q": query, "count": str(limit + 2)}
                )
                if resp.status_code == 200:
                    items = _parse_bing_html(_decode_body(resp), limit)
                    if items:
                        return _format_results(query, items)
                    problems.append("Bing 网页：0 条结果（可能被反爬拦截）")
                else:
                    problems.append(f"Bing 网页：HTTP {resp.status_code}")
            except Exception as exc:
                problems.append(f"Bing 网页：{type(exc).__name__}: {exc}")
    except Exception as exc:
        problems.append(f"网络不可用：{type(exc).__name__}: {exc}")
    return (
        f"搜索失败：{'；'.join(problems) or '未知原因'}\n"
        "可以：① 换更短、更口语的关键词重试；② 换成英文关键词；"
        "③ 已知网址时直接用 http_get 抓取。"
    )


# --------------------------------------------------------------------------- #
# 代码执行
# --------------------------------------------------------------------------- #
@tool(
    "run_python",
    "在工作区中用 Python 执行一段代码（无网络限制、有 20 秒超时），返回 stdout/stderr。适合做数据处理、生成文件。",
    {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 20，最大 60"},
        },
        "required": ["code"],
    },
    label="执行 Python",
)
def run_python(args: Dict[str, Any], ctx: ToolContext) -> str:
    code = str(args.get("code") or "")
    if not code.strip():
        return "错误：code 不能为空"
    timeout = max(1, min(int(args.get("timeout") or 20), 60))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code],
            cwd=str(ctx.workspace or WORKSPACE_DIR),
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"执行超时（>{timeout}s），已终止。"
    except Exception as exc:
        return f"启动 Python 失败：{type(exc).__name__}: {exc}"

    elapsed = time.time() - started
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    chunks = [f"退出码 {proc.returncode}，耗时 {elapsed:.2f}s"]
    if out:
        chunks.append(f"--- stdout ---\n{out[:6000]}")
    if err:
        chunks.append(f"--- stderr ---\n{err[:3000]}")
    if not out and not err:
        chunks.append("（无输出）")
    return "\n".join(chunks)
