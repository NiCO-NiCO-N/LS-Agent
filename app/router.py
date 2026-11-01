"""路由（主会话 / orchestrator）：只判断，不执行。

对应 Super-agent 的 Gate + Assessment 简化版：
- Gate 用代码写死（确定性、零成本）：空请求、纯问句、单步查询直接回答；
- Assessment 留给模型（下一步接），当前先用可解释的规则，命中未知则默认回答，
  宁可少派工，也不要把一句闲聊变成一张工单。
"""
from __future__ import annotations

import re
from typing import Any, Dict

# 明确的动手类动词：出现即倾向派工
WORK_VERBS = (
    "改", "实现", "重构", "修复", "修一下", "加上", "增加", "删掉", "删了",
    "重写", "写一个", "写个", "做一个", "做个", "排查", "定位", "优化",
    "迁移", "升级", "补上", "拆", "抽", "接上", "接线", "跑一遍", "跑一下",
    "refactor", "implement", "fix", "add ", "remove", "migrate", "rewrite",
)
# 问句/闲聊：即使含动词也不派工（"怎么改" 是在问，不是让干）
QUESTION = re.compile(r"(吗|呢|么|怎么|如何|为什么|为何|是否|能不能|可不可以|要不要|\?|？)\s*$")
INTERROGATIVE = re.compile(r"(怎么|如何|为什么|为何|是不是|有没有|能不能|可不可以)")
CHITCHAT = ("你好", "hi", "hello", "谢谢", "在吗", "ok", "收到", "嗯")

MIN_LEN = 4  # 太短的输入不当任务


def decide(text: str) -> Dict[str, Any]:
    """返回 {"action": "answer"|"dispatch", "reason": str, "scope": str}。"""
    raw = (text or "").strip()
    if not raw:
        return {"action": "answer", "reason": "空请求", "scope": ""}
    if len(raw) < MIN_LEN:
        return {"action": "answer", "reason": "过短，按回答处理", "scope": ""}
    low = raw.lower()
    if any(low.startswith(k) or low == k for k in CHITCHAT):
        return {"action": "answer", "reason": "寒暄", "scope": ""}
    if QUESTION.search(raw) or INTERROGATIVE.search(raw):
        return {"action": "answer", "reason": "问句，先回答再决定要不要动手", "scope": ""}
    hit = [v for v in WORK_VERBS if v in raw]
    if not hit:
        return {"action": "answer", "reason": "没有明确动手意图", "scope": ""}
    return {"action": "dispatch", "reason": f"命中动手动词：{'、'.join(hit[:3])}",
            "scope": raw[:120], "request": raw}


def should_dispatch(text: str) -> bool:
    return decide(text)["action"] == "dispatch"
