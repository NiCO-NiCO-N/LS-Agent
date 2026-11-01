"""技能系统。

一个技能 = skills/<slug>/SKILL.md（可带 scripts/*.py）。
SKILL.md 用 YAML 风格的 frontmatter 描述元信息：

    ---
    name: 深度调研简报
    description: 把一个问题拆成可检索的子问题并输出结构化简报
    tags: research, writing
    always: false
    ---

    # 正文：给模型看的完整操作说明

技能的「说明」会随系统提示词下发，正文由模型按需调用 load_skill 读取，
脚本由 run_skill_script 在技能目录里执行。
"""
from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import SKILLS_DIR, load_config
from .logging_setup import get_logger

log = get_logger("skills")

_lock = threading.RLock()
_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")


class SkillError(ValueError):
    pass


def valid_slug(slug: str) -> bool:
    return bool(slug) and bool(_SLUG_RE.match(slug))


def _skill_dir(slug: str) -> Path:
    if not valid_slug(slug):
        raise SkillError("技能名只能是字母、数字、- 和 _，且不超过 64 字符")
    return SKILLS_DIR / slug


def _split_frontmatter(text: str) -> tuple[Dict[str, str], str]:
    meta: Dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            raw = parts[1]
            body = parts[2].lstrip("\n")
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                value = value.strip().strip('"').strip("'")
                meta[key.strip().lower()] = value
    return meta, body


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def read_skill(slug: str) -> Dict[str, Any]:
    path = _skill_dir(slug) / "SKILL.md"
    if not path.exists():
        raise SkillError(f"技能 {slug} 不存在")
    text = path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(text)
    cfg = load_config()
    enabled = slug in (cfg.get("enabled_skills") or [])
    scripts_dir = _skill_dir(slug) / "scripts"
    scripts = sorted(p.name for p in scripts_dir.glob("*.py")) if scripts_dir.exists() else []
    return {
        "slug": slug,
        "name": meta.get("name") or slug,
        "description": meta.get("description") or "",
        "tags": [t.strip() for t in (meta.get("tags") or "").split(",") if t.strip()],
        "always": _truthy(meta.get("always")),
        "enabled": enabled,
        "body": body,
        "content": text,
        "scripts": scripts,
        "path": str(path),
    }


def list_skills() -> List[Dict[str, Any]]:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    items: List[Dict[str, Any]] = []
    for d in sorted(SKILLS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir() or not (d / "SKILL.md").exists():
            continue
        try:
            items.append(read_skill(d.name))
        except Exception:
            continue
    return items


def create_skill(slug: str, name: str, description: str, body: str = "", enable: bool = True) -> Dict[str, Any]:
    d = _skill_dir(slug)
    if (d / "SKILL.md").exists():
        raise SkillError(f"技能 {slug} 已存在")
    d.mkdir(parents=True, exist_ok=True)
    (d / "scripts").mkdir(exist_ok=True)
    if not body.strip():
        body = (
            f"# {name}\n\n"
            "## 适用场景\n\n- 说明什么情况下应该使用这个技能\n\n"
            "## 执行步骤\n\n1. 第一步\n2. 第二步\n\n"
            "## 输出要求\n\n- 用 Markdown 输出，结构清晰\n- 关键结论给出依据\n"
        )
    content = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "tags: \n"
        "always: false\n"
        "---\n\n" + body.strip() + "\n"
    )
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    if enable:
        cfg = load_config()
        enabled = list(cfg.get("enabled_skills") or [])
        if slug not in enabled:
            enabled.append(slug)
        cfg["enabled_skills"] = enabled
        from .config import save_config

        save_config(cfg)
    return read_skill(slug)


def update_skill(slug: str, content: str) -> Dict[str, Any]:
    d = _skill_dir(slug)
    if not (d / "SKILL.md").exists():
        raise SkillError(f"技能 {slug} 不存在")
    if not content.strip():
        raise SkillError("技能内容不能为空")
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    return read_skill(slug)


def delete_skill(slug: str) -> None:
    d = _skill_dir(slug)
    if not d.exists():
        raise SkillError(f"技能 {slug} 不存在")
    shutil.rmtree(d)
    log.info("删除技能 %s", slug)
    cfg = load_config()
    if slug in (cfg.get("enabled_skills") or []):
        cfg["enabled_skills"] = [s for s in cfg["enabled_skills"] if s != slug]
        from .config import save_config

        save_config(cfg)


def set_enabled(slug: str, enabled: bool) -> Dict[str, Any]:
    read_skill(slug)  # 校验存在
    cfg = load_config()
    current = [s for s in (cfg.get("enabled_skills") or []) if s != slug]
    if enabled:
        current.append(slug)
    cfg["enabled_skills"] = current
    from .config import save_config

    save_config(cfg)
    log.info("技能 %s 已%s", slug, "启用" if enabled else "停用")
    return read_skill(slug)


def build_system_prompt(cfg: Dict[str, Any]) -> str:
    """基础提示词 + 已启用技能清单（含 always 技能正文）。"""
    base = (cfg.get("system_prompt") or "").strip()
    parts: List[str] = [base]

    enabled_slugs = cfg.get("enabled_skills") or []
    skills: List[Dict[str, Any]] = []
    for slug in enabled_slugs:
        try:
            skills.append(read_skill(slug))
        except Exception:
            continue

    if skills:
        lines = ["", "## 已启用的技能", ""]
        for s in skills:
            desc = s["description"] or "（无描述）"
            lines.append(f"- `{s['slug']}` — {s['name']}：{desc}")
        lines.append("")
        lines.append(
            "当任务与某个技能相关时，先调用 `load_skill` 读取该技能的完整说明，再按说明执行；"
            "技能目录内若带 `scripts/*.py`，用 `run_skill_script` 运行。"
        )
        parts.append("\n".join(lines))

        always_skills = [s for s in skills if s.get("always")]
        for s in always_skills:
            parts.append(f"\n## 技能正文（始终生效）：{s['name']}\n\n{s['body'].strip()}")

    parts.append(f"\n## 工作区\n\n文件与代码工具只能操作：`{_workspace()}`（相对路径即可）。")

    all_skills = list_skills()
    if all_skills:
        parts.append(
            "\n其他未启用的技能（如需要可提示用户启用）："
            + "、".join(s["slug"] for s in all_skills if not s["enabled"])
            if any(not s["enabled"] for s in all_skills)
            else ""
        )

    return "\n".join(p for p in parts if p and p.strip())


def _workspace() -> str:
    from .config import WORKSPACE_DIR

    return str(WORKSPACE_DIR)
