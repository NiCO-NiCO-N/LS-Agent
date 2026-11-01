"""批量抓取网页正文并存到工作区，供模型逐篇阅读（仅标准库）。

用法：python fetch_pages.py <url> [<url> ...]
输出：web-cache/<时间戳>/ 下的若干 .md 文件 + 一份索引
"""
from __future__ import annotations

import html as htmllib
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # <项目根>/skills/<slug>/scripts/x.py
CACHE_ROOT = ROOT / "workspace" / "web-cache"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 lsagent/0.1"
)
MAX_PAGES = 8
MAX_CHARS = 40_000

_TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)


def to_text(raw: str) -> str:
    raw = _TAG_RE.sub(" ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = htmllib.unescape(raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def decode(raw: bytes, ctype: str) -> str:
    match = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    if match:
        try:
            return raw.decode(match.group(1), "replace")
        except LookupError:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def slugify(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc or "page"
    safe = re.sub(r"[^a-zA-Z0-9.-]+", "-", host).strip("-") or "page"
    return safe[:40]


def fetch(url: str) -> tuple[str, str]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read(2_000_000)
        ctype = resp.headers.get("content-type", "")
    text = decode(raw, ctype)
    if "html" in ctype.lower() or text.lstrip().startswith("<"):
        text = to_text(text)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n\n…（已截断，原始长度 {len(text)} 字符）"
    return text, ctype


def main() -> int:
    urls = [u.strip() for u in sys.argv[1:] if u.strip()]
    if not urls:
        print("用法：python fetch_pages.py <url> [<url> ...]")
        return 2
    bad = [u for u in urls if not u.startswith(("http://", "https://"))]
    if bad:
        print("错误：只接受 http/https 地址：" + ", ".join(bad))
        return 2
    if len(urls) > MAX_PAGES:
        print(f"提示：一次最多抓 {MAX_PAGES} 页，多余的已忽略")
        urls = urls[:MAX_PAGES]

    out_dir = CACHE_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    ok, failed = 0, []
    for idx, url in enumerate(urls, 1):
        try:
            text, ctype = fetch(url)
        except Exception as exc:  # 单页失败不影响整批
            failed.append(f"{idx}. {url} -> {type(exc).__name__}: {exc}")
            print(f"[失败] {idx}. {url} ({type(exc).__name__})")
            continue
        title = next((line.strip() for line in text.splitlines() if line.strip()), url)[:120]
        name = f"{idx:02d}-{slugify(url)}.md"
        path = out_dir / name
        path.write_text(
            f"# {title}\n\n- 来源：{url}\n- 抓取时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"- Content-Type：{ctype or '未知'}\n\n---\n\n{text}\n",
            encoding="utf-8",
        )
        rel = path.relative_to(ROOT / "workspace").as_posix()
        preview = " ".join(text.split())[:200]
        print(f"[{idx}] {title}")
        print(f"    字数 {len(text)} · 文件 {rel}")
        print(f"    预览 {preview}")
        ok += 1

    print(f"\n完成：成功 {ok} 页，失败 {len(failed)} 页，目录 web-cache/{out_dir.name}/")
    if failed:
        print("失败清单：")
        for line in failed:
            print("  " + line)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
