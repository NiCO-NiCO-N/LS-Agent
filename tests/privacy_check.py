"""发布前隐私自检：确认仓库里没有 Key、本机路径、邮箱等个人信息。

用法：
    python tests/privacy_check.py          # 检查
    python tests/privacy_check.py --all    # 连同 data/、workspace/ 等运行时目录一起检查

退出码 0 表示可以安全发布，1 表示发现可疑内容。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 不会进入仓库的目录（.gitignore 已覆盖），默认跳过
RUNTIME_DIRS = {"data", "workspace", ".agnes", "__pycache__", ".git", ".venv", "venv", "node_modules"}
TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".md", ".txt", ".json", ".csv", ".yml", ".yaml", ".toml", ".example"}

PATTERNS = [
    ("真实 API Key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("OpenAI/Anthropic 风格密钥", re.compile(r"\b(?:sk-ant-|sk-proj-)[A-Za-z0-9_\-]{10,}")),
    ("疑似 Bearer Token", re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}")),
    ("本机绝对路径", re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\s\"'<>]+|/Users/[A-Za-z0-9._-]+/")),
    ("用户目录名", re.compile(r"C:[\\/]+Users[\\/]+([A-Za-z0-9._-]+)", re.I)),
    ("邮箱地址", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(?:com|cn|net|org|io|dev)")),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# 这些是文档里正常出现的示例，不算泄露
ALLOW = {
    "https://api.openai.com/v1",
    "https://api.deepseek.com/v1",
    "https://api.anthropic.com",
    "http://127.0.0.1:11434/v1",
    "sk-xxxxxxxx",
    "sk-你的key",
    "C:\\Users\\...",  # 文档里的占位写法
    "git@github.com",  # 文档里的远端地址模板
}


def iter_files(include_runtime: bool):
    self_path = Path(__file__).resolve()
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if path.resolve() == self_path:  # 不检查自己（规则本身含示例字符串）
            continue
        parts = set(path.relative_to(ROOT).parts[:-1])
        if not include_runtime and (parts & RUNTIME_DIRS):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield path


def main() -> int:
    include_runtime = "--all" in sys.argv
    hits = []
    for path in iter_files(include_runtime):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(a in line for a in ALLOW):
                continue
            for label, pattern in PATTERNS:
                m = pattern.search(line)
                if not m:
                    continue
                snippet = m.group(0)
                hits.append((path.relative_to(ROOT).as_posix(), lineno, label, snippet[:60]))

    scope = "全部文件（含运行时目录）" if include_runtime else "将要提交的文件（已排除 data/、workspace/、.agnes/ 等）"
    print(f"扫描范围：{scope}")
    if not hits:
        print("未发现 API Key、本机路径或邮箱，可以发布。")
        return 0

    print(f"发现 {len(hits)} 处可疑内容：\n")
    for f, ln, label, snippet in hits:
        print(f"  {f}:{ln}  [{label}]  {snippet}")
    print("\n请逐条确认：真实密钥要立刻作废并换新的，本机路径改成相对路径或占位符。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
