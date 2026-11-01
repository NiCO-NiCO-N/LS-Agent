"""端到端自检：python tests/smoke_test.py

用 Mock 模型跑通「会话 -> 流式 -> 工具调用 -> 工具结果 -> 最终回答」，
并检查配置、技能、静态资源的接口是否正常。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 自测期间的日志写到临时文件，不污染 data/logs/ls-agent.log
SMOKE_LOG = Path(tempfile.gettempdir()) / "lsagent-smoke.log"
os.environ["LSAGENT_LOG_FILE"] = str(SMOKE_LOG)
os.environ["LSAGENT_LOG_CONSOLE"] = "0"  # 自测时不把日志混进检查结果

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.config import DEFAULT_CONFIG  # noqa: E402


def parse_sse(text: str):
    events = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    events.append(json.loads(payload))
    return events


def main() -> int:
    failures = []
    total = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(("  PASS  " if condition else "  FAIL  ") + name + (f"  ({detail})" if detail and not condition else ""))
        total.append(name)
        if not condition:
            failures.append(name)

    with TestClient(app) as client:
        print("\n[静态资源]")
        check("GET / 返回页面", client.get("/").status_code == 200)
        check("GET /static/app.js", client.get("/static/app.js").status_code == 200)
        check("GET /static/style.css", client.get("/static/style.css").status_code == 200)

        print("\n[配置]")
        cfg = client.get("/api/config").json()
        # 不依赖本机状态：只验证 active provider 有效，以及出厂默认是 mock 起步
        ids = [p["id"] for p in cfg["providers"]]
        check("active provider 有效", cfg["active_provider"] in ids, str(cfg.get("active_provider")))
        check(
            "出厂默认用 mock 起步",
            DEFAULT_CONFIG["active_provider"] == "mock" and DEFAULT_CONFIG["providers"][0]["type"] == "mock",
        )
        check("API Key 已脱敏", all(p["api_key"] == "" for p in cfg["providers"]))
        check("工作区存在", bool(cfg["workspace"]))

        # 自测不依赖本机状态：没有 mock provider 就临时建一个，跑完删掉
        mock = next((p for p in cfg["providers"] if p["type"] == "mock"), None)
        temp_mock = mock is None
        if temp_mock:
            resp = client.post("/api/providers", json={
                "id": "smoke-mock", "name": "冒烟 Mock", "type": "mock",
                "base_url": "", "api_key": "", "api_key_env": "",
                "model": "mock-agent", "temperature": 0.7, "max_tokens": 1024,
            })
            temp_mock = resp.status_code == 200  # 已存在就不算我们建的
            mock = {"id": "smoke-mock"}
        mock_id = mock["id"]
        check("有可用的 mock provider", bool(mock_id), str([p["id"] for p in cfg["providers"]]))

        tools = client.get("/api/tools").json()
        names = {t["name"] for t in tools}
        check("内置工具齐全", {"get_time", "calculate", "read_file", "write_file", "run_python", "http_get", "load_skill"} <= names, str(sorted(names)))

        print("\n[技能]")
        skills = client.get("/api/skills").json()
        slugs = {s["slug"] for s in skills}
        check("示例技能已加载", {"research-brief", "code-review", "data-analyst"} <= slugs, str(slugs))

        created = client.post("/api/skills", json={
            "slug": "smoke-temp",
            "name": "临时技能",
            "description": "冒烟测试用",
            "body": "# 临时\n\n测试正文",
            "enable": True,
        })
        check("创建技能", created.status_code == 200, created.text[:200])
        detail = client.get("/api/skills/smoke-temp").json()
        check("技能正文可读", "测试正文" in detail["body"])
        toggled = client.post("/api/skills/smoke-temp/toggle", json={"enabled": False}).json()
        check("技能可停用", toggled["enabled"] is False)
        check("删除技能", client.delete("/api/skills/smoke-temp").status_code == 200)

        print("\n[会话与 Agent 循环]")
        session = client.post("/api/sessions", json={"title": "冒烟测试", "provider_id": mock_id}).json()
        sid = session["id"]
        check("创建会话", bool(sid))

        with client.stream("POST", "/api/chat", json={
            "session_id": sid,
            "message": "现在几点了？",
            "provider_id": mock_id,
        }) as resp:
            check("SSE 状态码 200", resp.status_code == 200, str(resp.status_code))
            check("SSE content-type", "text/event-stream" in resp.headers.get("content-type", ""))
            body = "".join(resp.iter_text())

        events = parse_sse(body)
        types = [e["type"] for e in events]
        check("收到 session 事件", types[0] == "session", str(types[:3]))
        check("有文本增量", "delta" in types)
        check("有工具调用", "tool_call" in types, str(types))
        check("有工具结果", "tool_result" in types, str(types))
        check("正常结束", types[-1] == "done", str(types[-3:]))
        tool_events = [e for e in events if e["type"] == "tool_result"]
        check("工具执行成功", bool(tool_events) and tool_events[0]["ok"] is True, str(tool_events[:1]))

        stored = client.get(f"/api/sessions/{sid}").json()
        roles = [m["role"] for m in stored["messages"]]
        check("消息已持久化（user/assistant/tool）", roles[:3] == ["user", "assistant", "tool"], str(roles))
        check("自动生成标题", stored["title"] not in ("", "新对话"), stored["title"])

        print("\n[工具直连]")
        from app.tools import ToolContext, execute_tool

        ctx = ToolContext(session_id=sid)
        out, ok = __import__("asyncio").run(execute_tool("calculate", {"expression": "(12*3+4)/2"}, ctx))
        check("calculate 结果正确", ok and "= 20.0" in out, out)
        out, ok = __import__("asyncio").run(execute_tool("write_file", {"path": "smoke/hello.txt", "content": "hi ls"}, ctx))
        check("write_file", ok, out)
        out, ok = __import__("asyncio").run(execute_tool("read_file", {"path": "smoke/hello.txt"}, ctx))
        check("read_file", ok and "hi ls" in out, out)
        out, ok = __import__("asyncio").run(execute_tool("read_file", {"path": "../config.py"}, ctx))
        check("路径穿越被拦截", (not ok) or "超出工作区" in out, out)
        out, ok = __import__("asyncio").run(execute_tool("run_python", {"code": "print(6*7)"}, ctx))
        check("run_python", ok and "42" in out, out)

        print("\n[联网检索]")
        from app.tools import REGISTRY
        from app.tools.builtin import _html_to_text, _parse_bing_rss

        check("web_search 已注册", "web_search" in REGISTRY)
        check(
            "web_search 参数正确",
            "query" in REGISTRY["web_search"].parameters.get("properties", {}),
        )
        rss = (
            "<rss><channel><item><title>T1 &amp; co</title>"
            "<link>https://example.org/a</link>"
            "<description><![CDATA[摘要 <b>一</b>]]></description></item></channel></rss>"
        )
        items = _parse_bing_rss(rss, 5)
        check(
            "Bing RSS 解析",
            len(items) == 1
            and items[0]["url"] == "https://example.org/a"
            and items[0]["title"] == "T1 & co"
            and "摘要" in items[0]["snippet"],
            str(items),
        )
        filler = "正文内容。" * 240  # 以真实文章长度验证正文区提取
        page = (
            "<html><body><nav>导航菜单</nav>"
            f"<article><h1>标题</h1><p>{filler}</p></article><footer>页脚</footer></body></html>"
        )
        text = _html_to_text(page)
        check(
            "HTML 取正文区",
            "正文内容" in text and "导航菜单" not in text and "页脚" not in text,
            text[:120],
        )
        check("内置技能 web-search 存在", any(s["slug"] == "web-search" for s in client.get("/api/skills").json()))

        print("\n[日志]")
        from app import logging_setup

        check("日志文件已创建", SMOKE_LOG.exists(), str(SMOKE_LOG))
        payload = client.get("/api/logs").json()
        entries = payload.get("entries") or []
        check("GET /api/logs 有条目", bool(entries), str(payload)[:200])
        check(
            "日志条目字段完整",
            bool(entries) and {"time", "level", "source", "rid", "message"} <= set(entries[0]),
            str(entries[:1])[:300],
        )
        check("日志文件大小可读", payload.get("file_size", 0) >= 0 and payload.get("buffer_size", 0) > 0)

        fake_key = "sk" + "-" + "".join(["abc", "def", "0" * 12, "wxyz"])  # 拼出来，避免被隐私扫描当成真 Key
        logging_setup.get_logger("smoke").info("故意写一个假 Key：%s", fake_key)
        tail = logging_setup.recent(20)
        redacted = "".join(e["message"] for e in tail)
        check(
            "日志里密钥被打码",
            fake_key not in redacted and "sk-***" in redacted,
            redacted[-120:],
        )

        err_only = client.get("/api/logs?level=ERROR").json()["entries"]
        check("级别过滤生效", all(e["level"] == "ERROR" for e in err_only), str(err_only[:2]))
        found = client.get("/api/logs?search=工具").json()["entries"]
        check("关键字搜索生效", any("工具" in e["message"] for e in found), str(len(found)))
        chat_logged = any(e["source"] in ("agent", "chat") for e in client.get("/api/logs").json()["entries"])
        check("对话过程写入了日志", chat_logged)

        bad = client.put("/api/config", json={"log_level": "TRACE"})
        check("非法日志级别被拒", bad.status_code == 400, bad.text[:120])
        ok_level = client.put("/api/config", json={"log_level": "DEBUG"})
        check("日志级别可修改", ok_level.status_code == 200 and ok_level.json()["log_level"] == "DEBUG")
        client.put("/api/config", json={"log_level": "INFO"})

        download = client.get("/api/logs/download")
        check("日志可下载", download.status_code == 200 and len(download.content) > 0, str(download.status_code))

        cleared = client.delete("/api/logs").json()
        check(
            "清空日志",
            cleared["entries"] == [] and cleared["file_size"] == 0,
            str(cleared)[:200],
        )

        print("\n[会话清理]")
        check("清空消息", client.post(f"/api/sessions/{sid}/clear").json()["messages"] == [])
        check("删除会话", client.delete(f"/api/sessions/{sid}").status_code == 200)
        if temp_mock:
            check("清理临时 mock provider", client.delete(f"/api/providers/{mock_id}").status_code == 200)

    print()
    if failures:
        print(f"{len(failures)} 项失败：{', '.join(failures)}")
        return 1
    print(f"全部检查通过（{len(total)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
