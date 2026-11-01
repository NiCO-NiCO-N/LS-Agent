"""全局配置：模型提供商、系统提示词、工具/技能开关。

配置持久化在 data/config.json，API Key 只保存在本地，接口返回时会被脱敏。
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
SKILLS_DIR = ROOT / "skills"
WORKSPACE_DIR = ROOT / "workspace"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_SYSTEM_PROMPT = """你是 ls agent，一个运行在用户本机上的可扩展 AI 助手。

工作原则：
1. 先判断是否需要工具。需要实时信息、读写文件、执行代码、调用技能时，直接调用工具，不要凭空编造结果。
2. 文件与代码工具被限制在 workspace 目录内，路径写相对路径即可（例如 notes/todo.md）。
3. 已启用的技能会列在下方。任务和某个技能相关时，先调用 load_skill 读取该技能的完整说明，再严格按说明执行。
4. 多个任务可以并行调用工具，但不要重复调用同一个工具拿同样的结果。
5. 结束时用用户使用的语言简洁总结：做了什么、结果如何、下一步建议。"""

DEFAULT_CONFIG: Dict[str, Any] = {
    "assistant_name": "ls agent",
    "active_provider": "mock",
    "providers": [
        {
            "id": "mock",
            "name": "Mock 演示模型",
            "type": "mock",
            "base_url": "",
            "api_key": "",
            "model": "mock-agent",
            "temperature": 0.7,
            "max_tokens": 1024,
        },
        {
            "id": "openai",
            "name": "OpenAI",
            "type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "api_key_env": "OPENAI_API_KEY",
            "model": "gpt-4o-mini",
            "temperature": 0.7,
            "max_tokens": 2048,
        },
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "type": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "api_key_env": "DEEPSEEK_API_KEY",
            "model": "deepseek-chat",
            "temperature": 0.7,
            "max_tokens": 2048,
        },
        {
            "id": "ollama",
            "name": "Ollama 本地",
            "type": "openai",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key": "ollama",
            "model": "qwen2.5:7b",
            "temperature": 0.7,
            "max_tokens": 2048,
        },
        {
            "id": "anthropic",
            "name": "Anthropic Claude",
            "type": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": "",
            "api_key_env": "ANTHROPIC_API_KEY",
            "model": "claude-sonnet-4-20250514",
            "temperature": 0.7,
            "max_tokens": 4096,
        },
    ],
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "max_steps": 8,
    "enabled_skills": ["research-brief", "web-search"],
    "disabled_tools": [],
    "max_tool_result_chars": 12000,
    # 日志：级别 / 控制台 / 文件（相对项目根，留空=不写文件）/ 轮转 / 是否记录对话全文
    "log_level": "INFO",
    "log_console": True,
    "log_file": "data/logs/ls-agent.log",
    "log_max_mb": 2,
    "log_backups": 5,
    "log_prompts": False,
}

ALLOWED_CONFIG_KEYS = {
    "assistant_name",
    "active_provider",
    "system_prompt",
    "max_steps",
    "enabled_skills",
    "disabled_tools",
    "max_tool_result_chars",
    "log_level",
    "log_console",
    "log_file",
    "log_max_mb",
    "log_backups",
    "log_prompts",
}

_lock = threading.RLock()
_cache: Optional[Dict[str, Any]] = None


def ensure_dirs() -> None:
    for d in (DATA_DIR, SESSIONS_DIR, SKILLS_DIR, WORKSPACE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _with_defaults(raw: Dict[str, Any]) -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for key, value in (raw or {}).items():
        if key in ("providers", "system_prompt") or key in ALLOWED_CONFIG_KEYS:
            cfg[key] = value
    # 保证内置 provider 结构完整
    normalized: List[Dict[str, Any]] = []
    for item in cfg.get("providers") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        merged = {
            "id": item["id"],
            "name": item.get("name") or item["id"],
            "type": item.get("type") or "openai",
            "base_url": item.get("base_url") or "",
            "api_key": item.get("api_key") or "",
            "api_key_env": item.get("api_key_env") or "",
            "model": item.get("model") or "",
            "temperature": float(item.get("temperature") or 0.7),
            "max_tokens": int(item.get("max_tokens") or 2048),
        }
        normalized.append(merged)
    if not normalized:
        normalized = copy.deepcopy(DEFAULT_CONFIG["providers"])
    cfg["providers"] = normalized
    return cfg


def load_config() -> Dict[str, Any]:
    global _cache
    with _lock:
        if _cache is None:
            ensure_dirs()
            if CONFIG_PATH.exists():
                try:
                    _cache = _with_defaults(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
                except Exception:
                    _cache = copy.deepcopy(DEFAULT_CONFIG)
            else:
                _cache = copy.deepcopy(DEFAULT_CONFIG)
                save_config(_cache)
        return _cache


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    global _cache
    with _lock:
        ensure_dirs()
        cfg = _with_defaults(cfg)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        _cache = cfg
        return cfg


def update_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        cfg = load_config()
        for key, value in (patch or {}).items():
            if key in ALLOWED_CONFIG_KEYS:
                cfg[key] = value
        return save_config(cfg)


def get_provider(provider_id: Optional[str]) -> Optional[Dict[str, Any]]:
    cfg = load_config()
    if provider_id:
        for p in cfg["providers"]:
            if p["id"] == provider_id:
                return p
    for p in cfg["providers"]:
        if p["id"] == cfg.get("active_provider"):
            return p
    return cfg["providers"][0] if cfg["providers"] else None


def upsert_provider(provider: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        cfg = load_config()
        providers = cfg["providers"]
        pid = provider["id"]
        for i, p in enumerate(providers):
            if p["id"] == pid:
                merged = dict(p)
                for key, value in provider.items():
                    if key == "api_key" and value == "":
                        continue  # 空值表示保持原 Key
                    merged[key] = value
                providers[i] = merged
                break
        else:
            providers.append(provider)
        return save_config(cfg)


def delete_provider(provider_id: str) -> None:
    with _lock:
        cfg = load_config()
        cfg["providers"] = [p for p in cfg["providers"] if p["id"] != provider_id]
        if not cfg["providers"]:
            cfg["providers"] = copy.deepcopy(DEFAULT_CONFIG["providers"])
        if cfg["active_provider"] == provider_id:
            cfg["active_provider"] = cfg["providers"][0]["id"]
        save_config(cfg)


def resolve_api_key(provider: Dict[str, Any]) -> str:
    key = (provider.get("api_key") or "").strip()
    if key:
        return key
    env_name = (provider.get("api_key_env") or "").strip()
    if env_name:
        return (os.environ.get(env_name) or "").strip()
    return ""


def public_config() -> Dict[str, Any]:
    """给前端用的配置：隐藏明文 Key，只告知是否已配置。"""
    cfg = copy.deepcopy(load_config())
    for p in cfg["providers"]:
        p["has_key"] = bool(resolve_api_key(p))
        p["api_key"] = ""
    cfg["workspace"] = str(WORKSPACE_DIR)
    cfg["skills_dir"] = str(SKILLS_DIR)
    return cfg
