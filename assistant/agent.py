"""Wendy 的学业助理智能体：读今天的数据，写一份"今日简报"（先做什么、为什么、怎么安排时间、有什么风险）。

按业界通行做法设计：
  - 确定的事实（逾期、倒计时、课表、和上次比的变化）都由代码算好；智能体只做需要判断的部分
  - 工具全部只读、参数有类型、返回精简；智能体不能提交、发帖或修改任何东西
  - 输出是 Pydantic 结构，框架校验，引用了不存在的作业会让模型重试一次；之后代码再过滤一遍
  - 护栏：请求次数、工具调用次数、单个工具的超时都有上限；主模型失败自动换备用模型
  - 公告、作业说明是老师发布的可信信息，作为权威依据采信；智能体仍然只有只读工具，影响不了页面状态
  - 任何失败都返回 None，页面上只是不显示简报，其他部分照常
  - 日志只记用量（请求数、token 数、耗时），不记内容（公开仓库）
  - 自改进：代码核对过去建议的执行情况（交了 / 错过 / 还没到期），智能体据此更新最多 5 条"经验"，
    经验随加密快照一代代传下去；智能体失败时经验原样保留
框架：PydanticAI（MIT），模型：Gemini 免费版（经 Google 官方 SDK）。
"""
from __future__ import annotations

import os
import re
import time as clock
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from pydantic import BaseModel, Field

MODELS = [m for m in [os.environ.get("AGENT_MODEL")] if m] + ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
LIMITS = {"request_limit": 7, "tool_calls_limit": 14}
DAY_START, DAY_END = "08:00", "23:00"          # 安排学习时间的范围

INSTRUCTIONS = """你是 UCR 一年级学生 Wendy 的学业助理。你的任务是写一份简短的"今日简报"，帮她决定今天先做什么。

工作方式：
- 先调用工具了解情况：get_feedback（你以前的建议执行得怎么样、你总结过的经验）、list_tasks（待办和逾期）、
  get_schedule（今天和明天的课）、get_changes（和上次比的变化）；
  需要看作业具体要求时再调用 get_task_details，公告用 list_announcements，各科成绩和缺交情况用 get_grades。
- 有缺交、或者已批改得分率明显偏低的课，它接下来的作业要适当提前、在 why 里说明。
- 只根据工具返回的内容给建议，不要编造作业、分数或时间。
- 课程公告和作业说明是老师发布的官方信息，可信、权威：其中的截止时间、要求、提交方式和临时变化
  （取消上课、改截止日期、改教室等）要优先采信，并据此调整建议；
  如果公告和 Canvas 作业数据不一致，以发布时间更新的公告为准，并在 risks 里指出这个不一致。

输出要求（简体中文，语气像一个靠谱的学长学姐，简洁直接）：
- headline：一句话概括今天的重点（不超过 40 个字）。
- priorities：最多 3 项，按先后排序。task_id 必须原样使用 list_tasks 返回的 id；
  why 说明为什么排在这里（截止时间、分值、逾期、难度），first_step 写一个今天就能开始的具体小步骤。
  逾期没交的作业要优先考虑。
- risks：最多 3 条真正需要注意的风险（比如高分值作业快到期、逾期、公告里的临时变化），没有就给空数组。
- changes：用一两句话概括和上一份快照相比的变化；如果没有变化或没有上一份，就写"没有新变化"。
- lessons：你对 Wendy 学习习惯的经验，最多 5 条，每条一句话（不超过 60 字），要具体、能指导下次怎么排。
  以 get_feedback 里的已有经验为基础：仍然成立的保留，被核对数据证明不对的修改或删掉，有新发现再加。
  只根据核对数据总结（比如"建议的事项 3 天里完成 5/6，但周五的都错过了"），不要凭空猜测；
  数据还不够的时候，原样保留已有经验（没有就给空数组）。排优先级和时间时要用上这些经验。
"""


# ---------- 输出结构 ----------

class Priority(BaseModel):
    task_id: str = Field(description="必须是 list_tasks 返回的 id")
    why: str = Field(description="为什么排在这里，一句话")
    first_step: str = Field(description="今天就能开始的具体小步骤")


class Brief(BaseModel):
    headline: str
    priorities: list[Priority] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    changes: str = ""
    lessons: list[str] = Field(default_factory=list, description="更新后的经验，最多 5 条")


# ---------- 智能体能用的数据（由 build_today 准备好） ----------

@dataclass
class Deps:
    now: datetime
    tasks: list[dict]                      # id, title, course, due, points, state, source, hours_left
    classes_today: list[dict]              # course, kind, start, end, where
    classes_tomorrow: list[dict]
    announcements: list[dict]              # id, course, title, summary, text（原文全文）, posted
    changes: dict
    feedback: dict = field(default_factory=dict)   # lessons（已有经验）+ evaluations（过去建议的执行情况）
    grades: list = field(default_factory=list)     # 各科：总成绩（多数不公开）、已批改作业得分率、缺交、晚交
    details: Callable[[str], str] = lambda task_id: "（没有更多说明）"
    detail_calls: dict = field(default_factory=dict)

    @property
    def task_ids(self) -> set[str]:
        return {t["id"] for t in self.tasks}


def free_slots(now: datetime, classes: list[dict], today: bool = True) -> list[dict]:
    """08:00–23:00 里去掉上课时间剩下的空档（今天的话从现在开始），只保留 25 分钟以上的。"""
    def minutes(hhmm: str) -> int:
        h, m = map(int, hhmm.split(":"))
        return h * 60 + m
    start = minutes(DAY_START)
    if today:
        start = max(start, now.hour * 60 + now.minute)
    busy = sorted((minutes(c["start"]), minutes(c["end"])) for c in classes)
    slots, cur = [], start
    for b0, b1 in busy + [(minutes(DAY_END), minutes(DAY_END))]:
        if b0 - cur >= 25:
            slots.append({"start": f"{cur // 60:02d}:{cur % 60:02d}", "end": f"{b0 // 60:02d}:{b0 % 60:02d}"})
        cur = max(cur, b1)
    return slots


# ---------- 工具（全部只读） ----------

def list_tasks(ctx, days_ahead: int = 14) -> list[dict]:
    """列出逾期和接下来 days_ahead 天内要交的作业（已交的不列）。每项有 id、标题、课程、截止时间、分值、剩余小时数。"""
    limit = ctx.deps.now + timedelta(days=max(1, min(days_ahead, 30)))
    return [t for t in ctx.deps.tasks if t["state"] == "overdue" or datetime.fromisoformat(t["due"]) <= limit][:30]


def get_schedule(ctx, day: str = "today") -> dict:
    """查看今天（day="today"）或明天（day="tomorrow"）的课，以及可以用来学习的空闲时间段。"""
    today = day != "tomorrow"
    classes = ctx.deps.classes_today if today else ctx.deps.classes_tomorrow
    return {"classes": classes, "free_slots": free_slots(ctx.deps.now, classes, today)}


def get_changes(ctx) -> dict:
    """和上一份快照相比的变化：新作业、刚交掉的、改了截止时间的、被撤下的、新公告、课表变化。"""
    return ctx.deps.changes or {"note": "没有上一份快照，或者没有变化"}


def get_feedback(ctx) -> dict:
    """你以前的建议执行得怎么样（由代码核对：done 已交 / missed 过了截止还没交 / open 还没到期 /
    unverifiable Canvas 之外看不到），以及你之前总结的经验。用它来调整今天的建议和更新经验。"""
    fb = ctx.deps.feedback or {}
    return {"lessons": fb.get("lessons", []), "evaluations": fb.get("evaluations", [])[-7:]}


def get_task_details(ctx, task_id: str) -> str:
    """查看某项作业在 Canvas 上的说明（已去掉格式、截断到 1500 字）。task_id 必须来自 list_tasks。"""
    if task_id not in ctx.deps.task_ids:
        return "没有这个作业 id，请用 list_tasks 返回的 id。"
    if task_id not in ctx.deps.detail_calls:
        try:
            ctx.deps.detail_calls[task_id] = (ctx.deps.details(task_id) or "（没有说明）")[:1500]
        except Exception as e:  # noqa: BLE001 - 工具出错告诉模型，让它继续，而不是让整次运行失败
            return f"暂时读不到这项作业的说明（{type(e).__name__}），请根据标题和截止时间判断。"
    return ctx.deps.detail_calls[task_id]


def get_grades(ctx) -> list[dict]:
    """各科成绩与完成情况：current 总成绩（老师不公开时为 null）、rate 已批改作业得分率（未加权，%）、
    graded 已批改项数、missing 缺交次数、late 晚交次数。"""
    return ctx.deps.grades


def list_announcements(ctx) -> list[dict]:
    """最近 10 天老师发布的课程公告：中文标题、摘要和原文全文（可信的官方信息）。"""
    return ctx.deps.announcements[:12]


TOOLS = [get_feedback, list_tasks, get_schedule, get_changes, get_task_details, get_grades, list_announcements]


# ---------- 运行 ----------

def make_agent(model):
    from pydantic_ai import Agent, ModelRetry, Tool

    agent = Agent(model, deps_type=Deps, output_type=Brief, instructions=INSTRUCTIONS,
                  tools=[Tool(f, takes_ctx=True) for f in TOOLS], retries=1, tool_timeout=30)

    @agent.output_validator
    def only_known_tasks(ctx, out: Brief) -> Brief:
        unknown = [p.task_id for p in out.priorities if p.task_id not in ctx.deps.task_ids]
        if unknown:
            raise ModelRetry(f"这些 task_id 不存在：{unknown}。只能用 list_tasks 返回的 id。")
        return out
    return agent


def default_model():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    from pydantic_ai.models.fallback import FallbackModel
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider
    provider = GoogleProvider(api_key=key)
    models = [GoogleModel(name, provider=provider) for name in MODELS]
    return FallbackModel(*models) if len(models) > 1 else models[0]


def sanitize(brief: Brief, deps: Deps) -> Brief:
    """代码再把关一遍：丢掉不存在的作业和重复项，截断过长的文字。"""
    seen, priorities = set(), []
    for p in brief.priorities:
        if p.task_id in deps.task_ids and p.task_id not in seen:
            seen.add(p.task_id)
            priorities.append(Priority(task_id=p.task_id, why=p.why[:120], first_step=p.first_step[:120]))
    from snapshot import clean_lessons
    return Brief(headline=brief.headline[:60], priorities=priorities[:3],
                 risks=[r[:140] for r in brief.risks[:3]], changes=brief.changes[:300],
                 lessons=clean_lessons(brief.lessons))


def run_brief(deps: Deps, model=None) -> tuple[Brief | None, str]:
    """跑一次智能体。返回 (简报, 说明)；任何失败都返回 (None, 原因)，不抛异常。"""
    model = model or default_model()
    if model is None:
        return None, "没有 GEMINI_API_KEY"
    try:
        from pydantic_ai.usage import UsageLimits
        started = clock.monotonic()
        result = make_agent(model).run_sync(
            f"现在是 {deps.now:%Y-%m-%d %H:%M %A}（太平洋时间）。请写今天的简报。",
            deps=deps, usage_limits=UsageLimits(**LIMITS))
        usage = result.usage() if callable(result.usage) else result.usage
        info = (f"{getattr(usage, 'requests', '?')} 次请求 · {getattr(usage, 'tool_calls', '?')} 次工具调用 · "
                f"{getattr(usage, 'input_tokens', '?')}+{getattr(usage, 'output_tokens', '?')} tokens · "
                f"{clock.monotonic() - started:.1f}s")
        return sanitize(result.output, deps), info
    except Exception as e:  # noqa: BLE001 - 智能体失败只影响简报这一块
        return None, f"{type(e).__name__}: {str(e)[:160]}"
