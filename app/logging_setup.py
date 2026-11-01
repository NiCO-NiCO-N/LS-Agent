"""日志：控制台 + 轮转文件 + 内存环形缓冲（前端「日志」面板读这个缓冲）。

三个出口：
- 控制台：方便直接看服务在干什么（可用 log_console 关掉）
- 文件  ：data/logs/ls-agent.log，按大小轮转（log_max_mb / log_backups）
- 内存  ：最近 500 条，给 GET /api/logs 用，不落盘

约定：
- 所有日志走 `get_logger("agent")` 这样的子 logger，名字就是面板里的「来源」。
- 写之前统一过 `redact()`，`sk-...`、`Bearer ...`、`api_key: ...` 一律打码，避免日志泄露 Key。
- 默认只记长度和摘要，不记完整对话内容；要记全文把 log_prompts 打开。
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ROOT, load_config

LOGGER_NAME = "lsagent"
DEFAULT_LOG_FILE = "data/logs/ls-agent.log"
RING_SIZE = 500
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

_FORMAT = "%(asctime)s %(levelname)-5s [%(rid)s] %(name)s: %(message)s"

_rid: contextvars.ContextVar[str] = contextvars.ContextVar("lsagent_rid", default="")
_buffer: "deque[Dict[str, Any]]" = deque(maxlen=RING_SIZE)
_lock = threading.RLock()
_signature: Optional[str] = None

# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
_SECRET_RES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]{8,})"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret)([\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9._\-]{8,})"),
)


def _mask(token: str) -> str:
    if len(token) <= 8:
        return "***"
    return token[:3] + "***" + token[-4:]


def redact(text: str) -> str:
    """把日志里可能出现的密钥打码，保留首尾几位方便对账。"""
    if not text:
        return text
    out = _SECRET_RES[0].sub(lambda m: _mask(m.group(0)), str(text))
    out = _SECRET_RES[1].sub(lambda m: m.group(1) + _mask(m.group(2)), out)
    out = _SECRET_RES[2].sub(lambda m: m.group(1) + m.group(2) + _mask(m.group(3)), out)
    return out


# --------------------------------------------------------------------------- #
# 处理器
# --------------------------------------------------------------------------- #
class _RidFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _RidFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "rid", ""):
            record.rid = _rid.get() or "-"
        return True


class _BufferHandler(logging.Handler):
    """把日志留在内存里，给前端面板查。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            created = record.created
            entry = {
                "ts": round(created, 3),
                "time": time.strftime("%m-%d %H:%M:%S", time.localtime(created)) + f".{int(record.msecs):03d}",
                "level": record.levelname,
                "source": record.name[len(LOGGER_NAME) + 1:] if record.name.startswith(LOGGER_NAME + ".") else record.name,
                "rid": getattr(record, "rid", "") or _rid.get() or "-",
                "message": redact(record.getMessage()),
            }
            with _lock:
                _buffer.append(entry)
        except Exception:  # 日志自身绝不能把主流程带崩
            pass


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #
def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except Exception:
        return default


def resolve_log_file(cfg: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """日志文件路径：环境变量 LSAGENT_LOG_FILE 优先，其次 log_file；相对路径按项目根算。"""
    cfg = cfg or {}
    override = (os.environ.get("LSAGENT_LOG_FILE") or "").strip()
    raw = override or str(cfg.get("log_file") or "").strip()
    if not raw or raw.lower() in ("none", "off", "-"):
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (ROOT / path)


def _safe_stream() -> Any:
    """控制台编码不是 utf-8 时（Windows 常见 cp936），别让中文日志把服务写崩。"""
    stream = sys.stdout
    try:
        stream.reconfigure(errors="replace")
    except Exception:
        pass
    return stream


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
def setup_logging(cfg: Optional[Dict[str, Any]] = None, force: bool = False) -> Dict[str, Any]:
    """按配置装配 handler；配置没变就复用，改了才重建。"""
    global _signature
    cfg = cfg if isinstance(cfg, dict) else load_config()

    level_name = str(cfg.get("log_level") or "INFO").upper()
    if level_name not in LEVELS:
        level_name = "INFO"
    level = getattr(logging, level_name)
    console = _as_bool(cfg.get("log_console"), True)
    if (os.environ.get("LSAGENT_LOG_CONSOLE") or "").strip().lower() in ("0", "false", "off", "no"):
        console = False  # 环境变量便于在容器/自测里关掉控制台输出
    max_mb = _as_int(cfg.get("log_max_mb"), 2, 1, 50)
    backups = _as_int(cfg.get("log_backups"), 5, 0, 20)
    path = resolve_log_file(cfg)

    signature = f"{level_name}|{console}|{path}|{max_mb}|{backups}"
    logger = logging.getLogger(LOGGER_NAME)
    with _lock:
        if _signature == signature and logger.handlers and not force:
            for handler in logger.handlers:
                if not isinstance(handler, _BufferHandler):
                    handler.setLevel(level)
            return stats(cfg)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

        logger.setLevel(logging.DEBUG)  # 先全收，再由各 handler 过滤
        logger.propagate = False

        if console:
            stream_handler = logging.StreamHandler(_safe_stream())
            stream_handler.setLevel(level)
            stream_handler.setFormatter(_RidFormatter(_FORMAT, "%Y-%m-%d %H:%M:%S"))
            stream_handler.addFilter(_RidFilter())
            logger.addHandler(stream_handler)

        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                file_handler = RotatingFileHandler(
                    path,
                    maxBytes=max_mb * 1024 * 1024,
                    backupCount=backups,
                    encoding="utf-8",
                )
                file_handler.setLevel(level)
                file_handler.setFormatter(_RidFormatter(_FORMAT, "%Y-%m-%d %H:%M:%S"))
                file_handler.addFilter(_RidFilter())
                logger.addHandler(file_handler)
            except Exception as exc:  # 路径不可写时降级为只走控制台
                logger.warning("日志文件不可用（%s）：%s，改为只输出到控制台", path, exc)
                path = None

        buffer_handler = _BufferHandler()
        buffer_handler.setLevel(level)  # 面板与文件/控制台保持同一级别，调到 DEBUG 才看得到步骤细节
        logger.addHandler(buffer_handler)
        _signature = signature

    # 第三方库默认太吵，压到 WARNING
    for noisy in ("httpx", "httpcore", "urllib3", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger.info(
        "日志就绪：级别 %s，文件 %s（%s MB × %s 份），控制台 %s",
        level_name,
        path or "关闭",
        max_mb,
        backups,
        "开" if console else "关",
    )
    return stats(cfg)


def _ensure() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        try:
            setup_logging()
        except Exception:  # 配置读不出来也要能打日志
            logger.addHandler(logging.NullHandler())
    return logger


def get_logger(name: str = "app") -> logging.Logger:
    _ensure()
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


# --------------------------------------------------------------------------- #
# 请求 id：一次请求（一轮对话）里的所有日志串起来
# --------------------------------------------------------------------------- #
def new_request_id() -> Any:
    return _rid.set(uuid.uuid4().hex[:8])


def request_id() -> str:
    return _rid.get()


def reset_request_id(token: Any) -> None:
    try:
        _rid.reset(token)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 查询 / 维护（给 /api/logs 用）
# --------------------------------------------------------------------------- #
def recent(limit: int = 200, level: Optional[str] = None, search: str = "") -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 200), RING_SIZE))
    threshold = logging.DEBUG
    if level:
        name = str(level).upper()
        if name in LEVELS:
            threshold = getattr(logging, name)
    keyword = (search or "").strip().lower()
    with _lock:
        items = list(_buffer)
    if threshold > logging.DEBUG:
        items = [e for e in items if getattr(logging, e["level"], 0) >= threshold]
    if keyword:
        items = [
            e
            for e in items
            if keyword in e["message"].lower() or keyword in e["source"].lower()
        ]
    return items[-limit:]


def clear(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg if isinstance(cfg, dict) else load_config()
    logger = _ensure()
    logger.info("日志已由用户清空")  # 先留痕，随后连同这条一起抹掉
    with _lock:
        path = resolve_log_file(cfg)
        for handler in list(logger.handlers):
            if isinstance(handler, RotatingFileHandler):
                try:
                    handler.acquire()
                    if handler.stream is not None:
                        handler.stream.seek(0)
                        handler.stream.truncate(0)
                except Exception:
                    pass
                finally:
                    handler.release()
        _buffer.clear()
    return stats(cfg)


def stats(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg if isinstance(cfg, dict) else {}
    path = resolve_log_file(cfg) if cfg else resolve_log_file(load_config())
    size = 0
    if path is not None:
        try:
            size = path.stat().st_size
        except Exception:
            size = 0
    with _lock:
        count = len(_buffer)
    return {
        "file": str(path) if path is not None else "",
        "file_size": size,
        "buffer_size": RING_SIZE,
        "buffered": count,
        "level": str(cfg.get("log_level") or "INFO").upper(),
        "console": _as_bool(cfg.get("log_console"), True),
    }
