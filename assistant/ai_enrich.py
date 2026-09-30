"""用 Gemini 免费版给页面加一层"理解"（通过 Google 官方开源 SDK google-genai，Apache-2.0）：
  - 作业标题翻译成中文
  - 公告翻译成中文标题 + 一两句摘要
  - 从公告里读出截止事项，把 "next Friday" "by Week 3" 这类说法换算成具体日期

每次生成页面只调用一次。没有 GEMINI_API_KEY、没装 SDK、额度用完或返回不对时返回 None，
页面自动退回规则方式（原文 + 正则识别日期），不会出错。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

# 免费档可用的模型，按顺序尝试；可以用环境变量 GEMINI_MODEL 指定优先使用的
MODELS = [m for m in [os.environ.get("GEMINI_MODEL")] if m] + [
    "gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-flash-latest"]

PROMPT = """你是 UCR（加州大学河滨分校）一年级学生 Wendy 的学业助理。现在是 {now}（美国太平洋时间）。
下面是她 Canvas 上的作业标题和最近的课程公告（JSON）。请完成：

1. tasks：把每个作业标题翻译成简洁自然的简体中文。不要在前面加课程名（页面上另有课程标签）。
   只保留编号、课程代码和平台/产品名的原文，描述性的词一律翻译。例如：
   "Week 2: Attendance Quiz" → "第 2 周：出勤测验"
   "Fall Mid-Quarter Assignment" → "秋季期中作业"
   "EVERYONE SUBMIT: Lecture Reflection 9/29" → "全员提交：9/29 课堂反思"
   "Lab 0: Cat's Meow" → "Lab 0：猫咪喵喵"
   "CHASS F1RST Library Tutorial" → "CHASS F1RST 图书馆教程"
   "Active Practice #2" → "课后练习 #2（Active Practice）"
2. announcements：给每条公告写中文标题 zh_title，和 1~2 句中文摘要 summary，重点说清楚要她做什么、什么时候。
3. 每条公告的 deadlines：列出公告里要求学生在某个时间前完成的事（交作业、填问卷、注册账号、考试、来上课等）。
   - 根据公告的发布时间，把 "tomorrow" "next Friday" "this Sunday" "by the end of Week 3" 这类说法换算成具体日期；
   - 没写时间的按当天 23:59；
   - due 用 "YYYY-MM-DDTHH:MM"（太平洋时间），what 用中文简短描述，quote 摘录原文中对应的那句话；
   - 只列出明确要求学生完成的事项；已经过去超过 1 天的不要列；没有就给空数组。

数据：
{data}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {"type": "array", "items": {
            "type": "object", "required": ["id", "zh"],
            "properties": {"id": {"type": "string"}, "zh": {"type": "string"}}}},
        "announcements": {"type": "array", "items": {
            "type": "object", "required": ["id", "zh_title", "summary", "deadlines"],
            "properties": {
                "id": {"type": "string"}, "zh_title": {"type": "string"}, "summary": {"type": "string"},
                "deadlines": {"type": "array", "items": {
                    "type": "object", "required": ["what", "due", "quote"],
                    "properties": {"what": {"type": "string"}, "due": {"type": "string"},
                                   "quote": {"type": "string"}}}}}}},
    },
    "required": ["tasks", "announcements"],
}


def enrich(now: datetime, tasks: list, announcements: list[dict]) -> dict | None:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or not (tasks or announcements):
        return None
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("  · 没装 google-genai，跳过 AI（pip install google-genai）")
        return None

    data = {
        "tasks": [{"id": t.id, "course": t.course, "title": t.title} for t in tasks],
        "announcements": [{"id": str(a["id"]), "course": a["course"], "posted": f"{a['when']:%Y-%m-%d %H:%M %a}",
                           "title": a["title"], "text": a.get("text", "")[:3000]} for a in announcements],
    }
    prompt = PROMPT.format(now=f"{now:%Y-%m-%d %H:%M %A}", data=json.dumps(data, ensure_ascii=False))
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_json_schema=SCHEMA, temperature=0.2,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    client = genai.Client(api_key=key)
    for model in MODELS:
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            out = json.loads(resp.text)
            if isinstance(out, dict):
                out["model"] = model
                return out
        except Exception as e:  # noqa: BLE001 - 模型下线、额度、网络问题都换下一个模型，最后退回规则方式
            print(f"  · Gemini {model} 不可用：{type(e).__name__} {str(e)[:160]}")
    return None


def parse_due(s: str, tz) -> datetime | None:
    """'2026-10-02T23:59' → 太平洋时间的 datetime；格式不对返回 None。"""
    try:
        return datetime.strptime(s.strip()[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
    except (ValueError, AttributeError):
        return None
