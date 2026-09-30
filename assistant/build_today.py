#!/usr/bin/env python3
"""生成 Wendy 的"今日助理"页面：今天要上的课、要交的作业和截止时间，每项带倒计时。

数据来源：
  1. Canvas API（环境变量 CANVAS_TOKEN）：作业、测验、讨论的截止时间和提交状态，最近的公告，
     以及选了哪些班（课号里带班号，比如 CS_005_002_26F）
  2. UCR 选课系统 Banner 的公开课程查询：按班号实时读取上课时间、教室和老师，所以课表是自动更新的
  3. assistant/schedule.toml：给课加备注/链接，以及 Canvas 读不到的固定作业（Top Hat、iMath）

用法：
  python3 assistant/build_today.py --out _build/today/index.html
只用标准库。拿不到 Canvas 数据时照样生成页面，并在页面顶部显示原因。
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import agent as brief_agent
from ai_enrich import enrich, parse_due
from snapshot import diff, load_prev_state, make_state

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("America/Los_Angeles")
CANVAS = "https://elearn.ucr.edu"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
KINDS = {"assignment": "作业", "quiz": "测验", "discussion_topic": "讨论", "wiki_page": "阅读",
         "planner_note": "备忘", "calendar_event": "日程", "assessment_request": "互评"}


@dataclass
class Task:
    id: str
    title: str
    course: str
    due: datetime
    url: str
    source: str            # "Canvas" 或平台名（Top Hat、iMath…）
    kind: str = "作业"
    points: float | None = None
    done: bool = False
    note: str = ""
    zh: str = ""           # Gemini 翻译的中文标题（没有就显示原文）

    @property
    def manual(self) -> bool:  # Canvas 之外的任务，完成状态由 Wendy 在页面上自己勾
        return self.source != "Canvas"


# ---------- Canvas ----------

def canvas_get(path: str, params: dict | None = None):
    """GET Canvas API，列表结果自动翻页。"""
    url = f"{CANVAS}/api/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"Authorization": f"Bearer {os.environ['CANVAS_TOKEN']}"}
    out: list = []
    while url:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
            data = json.load(resp)
            link = resp.headers.get("Link", "")
        if not isinstance(data, list):
            return data
        out += data
        m = re.search(r'<([^>]+)>; rel="next"', link)
        url = m.group(1) if m else None
    return out


def course_label(context_name: str) -> str:
    """'CS_005_002_26F - INTRO…' → 'CS 005'"""
    m = re.match(r"([A-Z]+)_(\w+?)_\d+_", context_name or "")
    return f"{m[1]} {m[2]}" if m else (context_name or "").split(" - ")[0]


def canvas_tasks(now: datetime, names: dict[int, str]) -> list[Task]:
    items = canvas_get("planner/items", {
        "start_date": (now - timedelta(days=120)).date().isoformat(),   # 逾期很久没交的也要留着
        "end_date": (now + timedelta(days=60)).date().isoformat(),
        "per_page": 100,
    })
    tasks = []
    for it in items:
        p = it.get("plannable") or {}
        kind = it.get("plannable_type")
        when = parse_iso(it.get("plannable_date"))
        if kind == "announcement" or when is None:  # 公告单独处理，见 canvas_announcements
            continue
        if not isinstance(it.get("submissions"), dict) and when < now:   # 不需要在 Canvas 上提交的，过了就算了
            continue
        subs = it["submissions"] if isinstance(it.get("submissions"), dict) else {}
        override = it.get("planner_override") or {}
        done = bool(subs.get("submitted") or subs.get("excused") or override.get("marked_complete"))
        tasks.append(Task(id=f"canvas-{kind}-{it.get('plannable_id')}", title=p.get("title") or p.get("name") or "（无标题）",
                          course=names.get(it.get("course_id")) or course_label(it.get("context_name", "")),
                          due=when, url=urllib.parse.urljoin(CANVAS, it.get("html_url") or ""),
                          source="Canvas", kind=KINDS.get(kind, "任务"), points=p.get("points_possible"), done=done))
    return tasks


# ---------- 公告：列出来，并从正文里找截止日期 ----------

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DATE_RE = re.compile(
    r"\b(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<d1>\d{1,2})(?:st|nd|rd|th)?\b"
    r"|(?P<m2>1[0-2]|0?[1-9])/(?P<d2>3[01]|[12]\d|0?[1-9])(?:/\d{2,4})?(?![\d/]))", re.I)
TIME_RE = re.compile(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\b", re.I)
DEADLINE_WORDS = re.compile(r"\bdue\b|deadline|\bby\b|submit|complete|before|\bclose|\bexam|\bquiz|midterm|\bfinal", re.I)


def strip_html(h: str) -> str:
    t = re.sub(r"<(br|/p|/li|/h\d|/div|/tr)[^>]*>|<li[^>]*>", "\n", h or "")
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def nearest_date(month: int, day: int, now: datetime) -> date | None:
    """公告里的日期通常不写年份：取离现在最近、落在合理范围里的那一年。"""
    for y in (now.year, now.year + 1, now.year - 1):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if now.date() - timedelta(days=120) <= d <= now.date() + timedelta(days=240):
            return d
    return None


def deadline_mentions(text: str, now: datetime) -> list[dict]:
    """找出"含截止类字眼 + 日期"的句子，返回 (截止时间, 原句)。没写时间的按 23:59 算。"""
    found = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if not DEADLINE_WORDS.search(sentence):
            continue
        for m in DATE_RE.finditer(sentence):
            month = MONTHS.index(m["mon"][:3].lower()) + 1 if m["mon"] else int(m["m2"])
            d = nearest_date(month, int(m["d1"] or m["d2"]), now)
            if not d:
                continue
            t = TIME_RE.search(sentence)
            if t:
                hour = int(t[1]) % 12 + (12 if t[3].lower() == "p" else 0)
                at = time(hour, int(t[2] or 0))
            else:
                at = time(23, 59)
            found.append({"due": datetime.combine(d, at, TZ), "what": "", "quote": sentence.strip()[:200]})
    return found


def canvas_announcements(now: datetime, names: dict[int, str], courses: list[dict]) -> list[dict]:
    anns = canvas_get("announcements", {
        "context_codes[]": [f"course_{c['id']}" for c in courses],
        "start_date": (now - timedelta(days=14)).date().isoformat(),
        "end_date": (now + timedelta(days=1)).date().isoformat(),
        "per_page": 50,
    })
    code = {c["id"]: c.get("course_code", "") for c in courses}
    out = []
    for a in anns:
        cid = int(str(a.get("context_code", "_0")).split("_")[-1])
        text = strip_html(a.get("message", ""))
        out.append({"id": a["id"], "title": a.get("title", ""), "url": a.get("html_url", ""),
                    "course": names.get(cid) or course_label(code.get(cid, "")),
                    "when": parse_iso(a.get("posted_at")), "unread": a.get("read_state") == "unread",
                    "preview": text[:220] + ("…" if len(text) > 220 else ""), "text": text,
                    "mentions": deadline_mentions(text, now)})
    return sorted(out, key=lambda a: a["when"], reverse=True)


def announcement_tasks(anns: list[dict], canvas: list[Task], now: datetime) -> list[Task]:
    """公告里提到的未来 30 天的截止日期 → 待办；同一门课同一天已有 Canvas 作业的不重复添加。"""
    taken = {(t.course, t.due.date()) for t in canvas}
    out, seen = [], set()
    for a in anns:
        for m in a["mentions"]:
            due = m["due"]
            key = (a["course"], due.date())
            if not now <= due <= now + timedelta(days=30) or key in taken or key in seen:
                continue
            seen.add(key)
            how = "AI 从公告里读出来的" if m["what"] else "日期是从公告里自动读的"
            out.append(Task(id=f"ann-{a['id']}-{due:%m%d}", title=m["what"] or f"公告提到：{a['title']}",
                            course=a["course"], due=due, url=a["url"], source="公告",
                            note=f"“{m['quote']}” —— {how}，请点开核对"))
    return out


def task_details(task: Task) -> str:
    """智能体的只读工具用：从 Canvas 读作业说明（去掉格式）。"""
    m = re.search(r"/courses/(\d+)/(assignments|quizzes|discussion_topics)/(\d+)", task.url or "")
    if not m or task.source != "Canvas":
        return task.note or "（Canvas 之外的任务，没有更多说明）"
    data = canvas_get(f"courses/{m[1]}/{m[2]}/{m[3]}")
    return strip_html(data.get("description") or data.get("message") or "") if isinstance(data, dict) else ""


def agent_deps(now: datetime, cfg: dict, tasks: list[Task], classes: list[dict], announcements: list[dict],
               changes: dict) -> "brief_agent.Deps":
    holidays = {str(h) for h in cfg["term"].get("holidays", [])}
    overdue, pending, _ = classify(now, tasks, cfg["term"])
    by_id = {t.id: t for t in overdue + pending}
    views = [{"id": t.id, "title": t.zh or t.title, "course": t.course, "due": t.due.isoformat(),
              "points": t.points, "source": t.source, "state": "overdue" if t in overdue else "pending",
              "hours_left": round((t.due - now).total_seconds() / 3600, 1)} for t in overdue + pending]

    def cls(day):
        return [{"course": c["course"], "kind": c.get("kind", ""), "where": c.get("where", ""),
                 "start": f"{c['start_dt']:%H:%M}", "end": f"{c['end_dt']:%H:%M}"} for c in classes_on(day, classes, holidays)]
    anns = [{"id": str(a["id"]), "course": a["course"], "title": a.get("zh_title") or a["title"],
             "summary": a.get("summary") or a["preview"], "text": a.get("text", "")[:3000],
             "posted": a["when"].isoformat()}
            for a in announcements if a["when"] and a["when"] >= now - timedelta(days=10)]
    return brief_agent.Deps(now=now, tasks=views, classes_today=cls(now.date()),
                            classes_tomorrow=cls(now.date() + timedelta(days=1)), announcements=anns,
                            changes=changes, details=lambda tid: task_details(by_id[tid]) if tid in by_id else "")


def strip_course_prefix(title: str, course: str) -> str:
    """'CS 005：Lab 0' / 'CS005 - Lab 0' → 'Lab 0'（课程名已经用标签显示了）。"""
    pattern = r"^\s*" + r"\s*".join(map(re.escape, course.split())) + r"\s*[:：\-–·|]?\s*"
    rest = re.sub(pattern, "", title, flags=re.I)
    return rest or title


def apply_ai(ai: dict, tasks: list[Task], announcements: list[dict]) -> None:
    """把 Gemini 的结果并进来：中文标题、公告摘要；AI 读出的截止事项替换掉正则识别的结果。"""
    zh = {str(x.get("id")): x.get("zh", "") for x in ai.get("tasks") or [] if isinstance(x, dict)}
    for t in tasks:
        t.zh = strip_course_prefix((zh.get(t.id) or "").strip(), t.course)
    by_id = {str(x.get("id")): x for x in ai.get("announcements") or [] if isinstance(x, dict)}
    for a in announcements:
        x = by_id.get(str(a["id"]))
        if not x:
            continue
        a["zh_title"], a["summary"] = x.get("zh_title", ""), x.get("summary", "")
        mentions = []
        for d in x.get("deadlines") or []:
            due = parse_due(str(d.get("due", "")), TZ) if isinstance(d, dict) else None
            if due:
                mentions.append({"due": due, "what": str(d.get("what", ""))[:80], "quote": str(d.get("quote", ""))[:200]})
        a["mentions"] = mentions


# ---------- 课表：从 UCR 选课系统（Banner）实时读取 ----------

BANNER = "https://registrationssb.ucr.edu/StudentRegistrationSsb/ssb"
SECTION_RE = re.compile(r"^([A-Z]+)_(\w+?)_(\d{3})_(\d{2})([WSUF])")   # CS_005_002_26F
TERM_SUFFIX = {"W": "10", "S": "20", "U": "30", "F": "40"}              # 26F → 202640
KIND_CN = {"Lecture": "大课", "Discussion": "讨论课", "Laboratory": "Lab", "Workshop": "工作坊",
           "Seminar": "研讨课", "Studio": "Studio", "Activity": "活动"}
BANNER_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def enrolled_sections(courses: list[dict]) -> set[tuple[str, str, str, str]]:
    """从 Canvas 课号和子班名里认出 (科目, 课号, 班号, 学期代码)。PSYC 讨论课 021 这种子班也会被认出来。"""
    out = set()
    for c in courses:
        for name in [c.get("course_code", "")] + [s.get("name", "") for s in c.get("sections") or []]:
            m = SECTION_RE.match(name or "")
            if m:
                out.add((m[1], m[2], m[3], f"20{m[4]}{TERM_SUFFIX[m[5]]}"))
    return out


def person(display_name: str) -> str:
    """'Wood, William' → 'William Wood'"""
    last, _, first = display_name.partition(", ")
    return f"{first} {last}".strip()


def banner_opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor())


def banner_classes(sections: set[tuple[str, str, str, str]]) -> tuple[list[dict], list[dict]]:
    """返回 (有固定上课时间的课, 没有固定时间的线上课)。"""
    opener = banner_opener()
    call = lambda path, data=None: opener.open(f"{BANNER}/{path}", data=data, timeout=30).read()  # noqa: E731
    call("term/termSelection?mode=search")
    timed, online, selected_term = [], [], None
    for subj, num, term in sorted({(s, n, t) for s, n, _, t in sections}):
        if term != selected_term:
            call("term/search?mode=search", f"term={term}".encode())
            selected_term = term
        call("classSearch/resetDataForm", b"")
        query = urllib.parse.urlencode({"txt_subject": subj, "txt_courseNumber": num, "txt_term": term,
                                        "pageOffset": 0, "pageMaxSize": 500, "sortColumn": "subjectDescription",
                                        "sortDirection": "asc"})
        wanted = {seq for s, n, seq, t in sections if (s, n, t) == (subj, num, term)}
        for sec in json.loads(call(f"searchResults/searchResults?{query}")).get("data") or []:
            if sec.get("courseNumber") != num or sec.get("sequenceNumber") not in wanted:
                continue
            base = KIND_CN.get(sec.get("scheduleTypeDescription", ""), sec.get("scheduleTypeDescription", ""))
            kind = base if base == "大课" else f"{base} {sec['sequenceNumber']}"
            teacher = next((person(f["displayName"]) for f in sec.get("faculty", []) if f.get("primaryIndicator")), "")
            for mf in sec.get("meetingsFaculty", []):
                m = mf.get("meetingTime") or {}
                online_only = (m.get("building") or "").upper() == "ONLINE"
                where = "线上" if online_only else f"{m.get('buildingDescription', '')} {m.get('room', '')}".strip()
                entry = {"course": f"{subj} {num}", "kind": kind, "where": where,
                         "note": f"{'TA' if base in ('讨论课', 'Lab') else '老师'} {teacher}" if teacher else ""}
                days = [WEEKDAYS[i] for i, d in enumerate(BANNER_DAYS) if m.get(d)]
                if not (days and m.get("beginTime") and m.get("endTime")):
                    online.append(entry)
                    continue
                if "exam" in (m.get("meetingTypeDescription") or "").lower():
                    entry["kind"] = "考试"
                entry.update(days=days, start=f"{m['beginTime'][:2]}:{m['beginTime'][2:]}",
                             end=f"{m['endTime'][:2]}:{m['endTime'][2:]}",
                             **{"from": datetime.strptime(m["startDate"], "%m/%d/%Y").date(),
                                "until": datetime.strptime(m["endDate"], "%m/%d/%Y").date()})
                timed.append(entry)
    return timed, online


TERM_NAME = {"10": "Winter", "20": "Spring", "30": "Summer", "40": "Fall"}


def derive_term(term_cfg: dict, timed: list[dict], sections: set, today: date) -> dict:
    """学期信息：schedule.toml 里写了就用写的，没写的从选课系统推出来，换学期不用改配置。
    Week 1 = 开课那天当天或之后的第一个周一（UCR 秋季周四开课，那几天算 Week 0）。"""
    term = dict(term_cfg)
    starts = [as_date(c["from"]) for c in timed if c.get("from")]
    ends = [as_date(c["until"]) for c in timed if c.get("until")]
    if "week1_monday" not in term:
        first = min(starts) if starts else today - timedelta(days=today.weekday())
        term["week1_monday"] = first + timedelta(days=(7 - first.weekday()) % 7)
    if "last_class_day" not in term:
        term["last_class_day"] = max(ends) if ends else as_date(term["week1_monday"]) + timedelta(weeks=10)
    if "name" not in term:
        code = max((s[3] for s in sections), default="")
        term["name"] = f"{TERM_NAME.get(code[4:], '')} {code[:4]}".strip()
    return term

def apply_notes(classes: list[dict], notes: list[dict]) -> list[dict]:
    """schedule.toml 里的 [[notes]]：按 课程 + 类型开头 匹配，补充备注、链接，或推迟开始日期。"""
    out = []
    for c in classes:
        c = dict(c)
        for n in notes:
            if n.get("course") == c["course"] and c.get("kind", "").startswith(n.get("kind", "")):
                if n.get("note"):
                    c["note"] = " · ".join(x for x in (c.get("note"), n["note"]) if x)
                for key in ("link", "from", "until", "where"):
                    if key in n:
                        c[key] = n[key]
        out.append(c)
    return out


# ---------- schedule.toml ----------

def as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def hm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ) if s else None


def week_number(day: date, term: dict) -> int:
    w1 = as_date(term["week1_monday"])
    return 0 if day < w1 else (day - w1).days // 7 + 1


def classes_on(day: date, classes: list[dict], holidays: set[str]) -> list[dict]:
    if day.isoformat() in holidays:
        return []
    out = []
    for c in classes:
        if WEEKDAYS[day.weekday()] not in c.get("days", []):
            continue
        if "from" in c and day < as_date(c["from"]) or "until" in c and day > as_date(c["until"]):
            continue
        start = datetime.combine(day, hm(c["start"]), TZ)
        end = datetime.combine(day, hm(c["end"]), TZ) if c.get("end") else start + timedelta(minutes=50)
        out.append({**c, "start_dt": start, "end_dt": end})
    return sorted(out, key=lambda c: c["start_dt"])


def recurring_tasks(now: datetime, cfg: dict) -> list[Task]:
    out = []
    for r in cfg.get("recurring", []):
        first, last = as_date(r["from"]), as_date(r["until"])
        for i in range(-1, 21):
            day = now.date() + timedelta(days=i)
            if WEEKDAYS[day.weekday()] != r["weekday"] or not first <= day <= last:
                continue
            due = datetime.combine(day, hm(r.get("due", "23:59")), TZ)
            if due < now - timedelta(hours=12):
                continue
            out.append(Task(id=f"{r['platform']}-{r['course']}-{day.isoformat()}", title=r["title"],
                            course=r["course"], due=due, url=r.get("url", ""), source=r["platform"],
                            note=r.get("note", "")))
    return out


# ---------- rendering：Tabler（UI 套件）+ FullCalendar（课程表），外壳在 template.html ----------

TEMPLATE = HERE / "template.html"
CAT = HERE / "cat.jpg"   # "魔法猫猫小提示"表情包，生成时直接嵌进页面
# Tabler 自带的颜色名；每门课固定分到一个，页面标签和日历用同一个颜色
COLORS = ["blue", "pink", "teal", "orange", "purple", "green", "indigo", "red", "cyan", "yellow", "lime", "azure"]
HEX = {"blue": "#066fd1", "pink": "#d6336c", "teal": "#0ca678", "orange": "#f76707", "purple": "#ae3ec9",
       "green": "#2fb344", "indigo": "#4263eb", "red": "#d63939", "cyan": "#17a2b8", "yellow": "#f59f00",
       "lime": "#74b816", "azure": "#4299e1", "secondary": "#667382"}
URGENCY_BADGE = {"u-over": "bg-red-lt", "u-24": "bg-red-lt", "u-72": "bg-orange-lt", "u-week": "bg-blue-lt",
                 "u-later": "bg-secondary-lt"}


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def meme_card(now: datetime) -> tuple[str, dict]:
    """魔法猫猫小提示：今年（加州时间 1 月 1 日零点起）已经过了几个整星期。"""
    if not CAT.exists():
        return "", {}
    start = datetime(now.year, 1, 1, tzinfo=TZ)
    nxt = datetime(now.year + 1, 1, 1, tzinfo=TZ)
    weeks = int((now - start).total_seconds() // (7 * 86400))
    pct = (now - start) / (nxt - start) * 100
    img = "data:image/jpeg;base64," + base64.b64encode(CAT.read_bytes()).decode()
    card = (f'<div class="col-12"><div class="card"><div class="card-body">'
            f'<div class="meme mx-auto" style="--cat:url({img})" role="img" id="meme" '
            f'aria-label="小提示：你已经在{now.year}浪费了{weeks}个星期了">'
            f'<div class="meme-cap" aria-hidden="true"><div>小提示：你已经在<span class="meme-year">{now.year}</span></div>'
            f'<div>浪费了<span class="meme-num">{weeks}</span>个星期了</div></div></div>'
            f'<div class="meme-foot mx-auto mt-3"><div class="d-flex justify-content-between text-secondary small mb-1">'
            f'<span>今年已过 <b class="meme-elapsed text-body">{weeks} 周</b></span>'
            f'<span>全年进度 <b class="meme-pct text-body">{pct:.1f}%</b></span></div>'
            f'<div class="progress progress-sm"><div class="progress-bar meme-bar" style="width:{pct:.2f}%"></div></div></div>'
            f'</div></div></div>')
    data = {"year": now.year, "start": int(start.timestamp() * 1000), "next": int(nxt.timestamp() * 1000)}
    return card, data


def course_colors(names) -> dict[str, str]:
    return {n: COLORS[i % len(COLORS)] for i, n in enumerate(sorted(set(names)))}


def rel_day(day: date, today: date) -> str:
    return {-1: "昨天", 0: "今天", 1: "明天", 2: "后天"}.get((day - today).days, WEEKDAYS_CN[day.weekday()])


def day_heading(day: date, today: date) -> str:
    label = f"{day.month}/{day.day} {WEEKDAYS_CN[day.weekday()]}"
    rel = {0: "今天", 1: "明天", 2: "后天"}.get((day - today).days)
    return f"{rel} · {label}" if rel else label


def countdown_text(delta: timedelta) -> str:
    """服务器端的初始文字；打开页面后由 JS 每秒刷新。"""
    s = int(delta.total_seconds())
    if s < 0:
        return "已截止"
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}天{h}小时" if d else f"{h}小时{m}分" if h else f"{m}分"


def urgency(delta: timedelta) -> str:
    s = delta.total_seconds()
    return "u-over" if s < 0 else "u-24" if s < 86400 else "u-72" if s < 3 * 86400 else "u-week" if s < 7 * 86400 else "u-later"


def course_badge(course: str, colors: dict) -> str:
    return f'<span class="badge bg-{colors.get(course, "secondary")}-lt">{esc(course)}</span>'


def link(text: str, url: str, cls: str = "text-reset") -> str:
    return f'<a class="{cls}" href="{esc(url)}" target="_blank" rel="noopener">{esc(text)}</a>' if url else esc(text)


def where_html(c: dict) -> str:
    where, url = c.get("where", ""), c.get("link", "")
    if where.startswith("http"):
        where, url = "", where
    parts = [f'<i class="ti ti-map-pin"></i> {esc(where)}'] if where else []
    if url:
        parts.append(f'<a href="{esc(url)}" target="_blank" rel="noopener"><i class="ti ti-video"></i> '
                     f'{"Zoom" if "zoom" in url else "链接"}</a>')
    return " · ".join(parts)


def task_html(t: Task, now: datetime, colors: dict, state: str = "") -> str:
    color = colors.get(t.course, "secondary")
    meta = [course_badge(t.course, colors), esc(t.source if t.manual else t.kind)]
    if t.points:
        meta.append(f"{t.points:g} 分")
    meta.append(f"{rel_day(t.due.date(), now.date())} {t.due:%H:%M}")
    if t.manual:
        lead = (f'<input class="form-check-input m-0 tick" type="checkbox" data-key="{esc(t.id)}" '
                f'aria-label="标记为已完成">')
    else:
        lead = f'<span class="status-dot status-{color if not t.done else "green"} d-block"></span>'
    note = f'<div class="text-secondary small mt-1">{esc(t.note)}</div>' if t.note else ""
    if t.done:
        badge = '<span class="badge bg-green-lt cd-done"><i class="ti ti-check"></i> 已交</span>'
    else:
        u = urgency(t.due - now)
        badge = f'<span class="badge {URGENCY_BADGE[u]} cd">{countdown_text(t.due - now)}</span>'
    return (f'<div class="list-group-item task{" done" if t.done else ""}" data-due="{t.due.isoformat()}" '
            f'data-id="{esc(t.id)}" data-state="{state}">'
            f'<div class="row align-items-center g-3"><div class="col-auto">{lead}</div>'
            f'<div class="col min-w-0"><div class="fw-medium title">{link(t.zh or t.title, t.url)}</div>'
            f'{f"<div class=\"text-secondary small original\">{esc(t.title)}</div>" if t.zh and t.zh != t.title else ""}'
            f'<div class="text-secondary small mt-1 d-flex flex-wrap gap-2 align-items-center">{" · ".join(meta)}</div>'
            f'{note}</div><div class="col-auto">{badge}</div></div></div>')


def class_html(c: dict, colors: dict) -> str:
    color = colors.get(c["course"], "secondary")
    warn = (f' <a class="badge bg-orange-lt" href="{esc(c["warn_url"])}" target="_blank" rel="noopener">'
            f'<i class="ti ti-alert-triangle"></i> 公告说可能取消</a>' if c.get("warn_url") else "")
    note = f' · {esc(c["note"])}' if c.get("note") else ""
    return (f'<div class="list-group-item cls" data-start="{c["start_dt"].isoformat()}" data-end="{c["end_dt"].isoformat()}">'
            f'<div class="row align-items-center g-3">'
            f'<div class="col-auto text-center time-col"><div class="fw-bold">{c["start_dt"]:%H:%M}</div>'
            f'<div class="text-secondary small">{c["end_dt"]:%H:%M}</div></div>'
            f'<div class="col-auto"><span class="status-dot status-{color} d-block"></span></div>'
            f'<div class="col min-w-0"><div class="fw-medium">{course_badge(c["course"], colors)} {esc(c.get("kind", ""))}{warn}</div>'
            f'<div class="text-secondary small mt-1">{where_html(c)}{note}</div></div>'
            f'<div class="col-auto"><span class="badge bg-secondary-lt cd"></span></div></div></div>')


def stat_card(icon: str, color: str, value: int, label: str, href: str) -> str:
    return (f'<div class="col-6 col-lg-3"><a class="card card-sm card-link text-reset" href="{href}"><div class="card-body">'
            f'<div class="row align-items-center"><div class="col-auto"><span class="bg-{color} text-white avatar">'
            f'<i class="ti ti-{icon} fs-2"></i></span></div><div class="col"><div class="h1 mb-0 lh-1">{value}</div>'
            f'<div class="text-secondary small">{label}</div></div></div></div></a></div>')


def next_card(id_: str, label: str, icon: str) -> str:
    return (f'<div class="col-md-6"><div class="card next" id="{id_}"><div class="card-status-start"></div>'
            f'<div class="card-body"><div class="subheader"><i class="ti ti-{icon}"></i> {label}</div>'
            f'<div class="h1 my-2 next-big">—</div><div class="fw-medium next-title">加载中…</div>'
            f'<div class="text-secondary small next-meta"></div></div></div></div>')


def calendar_payload(today: date, cfg: dict, classes: list[dict], tasks: list[Task], colors: dict) -> dict:
    """FullCalendar 用的数据：整个学期的课（节假日已排除）+ 截止时间点。"""
    holidays = {str(h) for h in cfg["term"].get("holidays", [])}
    start = as_date(cfg["term"]["week1_monday"]) - timedelta(days=7)
    end = as_date(cfg["term"].get("last_class_day", today + timedelta(days=90))) + timedelta(days=14)
    events, hours, weekend = [], [], False
    day = start
    while day <= end:
        for c in classes_on(day, classes, holidays):
            events.append({"title": f"{c['course']} {c.get('kind', '')}", "start": c["start_dt"].isoformat(),
                           "end": c["end_dt"].isoformat(), "color": HEX[colors.get(c["course"], "secondary")],
                           "extendedProps": {"where": c.get("where", "")}})
            hours += [c["start_dt"].hour, c["end_dt"].hour + (1 if c["end_dt"].minute else 0)]
            weekend |= day.weekday() >= 5
        day += timedelta(days=1)
    for t in tasks:
        if t.done:
            continue
        events.append({"title": f"{t.due:%H:%M} {t.course} {t.title}", "start": t.due.date().isoformat(),
                       "allDay": True, "url": t.url, "classNames": ["deadline"],
                       "color": HEX[colors.get(t.course, "secondary")]})
    lo, hi = (min(hours), max(hours)) if hours else (8, 18)
    return {"events": events, "slotMinTime": f"{max(lo - 1, 0):02d}:00:00", "slotMaxTime": f"{min(hi + 1, 24):02d}:00:00",
            "hiddenDays": [] if weekend else [0, 6]}


def classify(now: datetime, tasks: list[Task], term: dict) -> tuple[list[Task], list[Task], list[Task]]:
    """(逾期, 待办, 最近已交)。页面和智能体都用这一套判断。"""
    term_start = as_date(term["week1_monday"]) - timedelta(days=14)   # 上学期的旧账不算
    overdue = sorted((t for t in tasks if not t.done and not t.manual and t.due < now and t.due.date() >= term_start),
                     key=lambda t: t.due)
    pending = sorted((t for t in tasks if not t.done and t.due >= now - timedelta(hours=12) and t not in overdue),
                     key=lambda t: t.due)
    done = sorted((t for t in tasks if t.done and t.due >= now - timedelta(days=3)), key=lambda t: t.due)
    return overdue, pending, done


def brief_card(brief, tasks: list[Task], colors: dict) -> str:
    """智能体写的今日简报。所有引用的作业 id 都已经过校验。"""
    by_id = {t.id: t for t in tasks}
    pri = "".join(
        f'<div class="list-group-item"><div class="row g-3 align-items-start">'
        f'<div class="col-auto"><span class="brief-num">{i}</span></div><div class="col min-w-0">'
        f'<div class="fw-medium d-flex flex-wrap align-items-center gap-2">{course_badge(by_id[p.task_id].course, colors)}'
        f'<span>{link(by_id[p.task_id].zh or by_id[p.task_id].title, by_id[p.task_id].url)}</span></div>'
        f'<div class="text-secondary mt-2">{esc(p.why)}</div>'
        f'<div class="brief-step mt-2"><i class="ti ti-arrow-right"></i><span>{esc(p.first_step)}</span></div>'
        f'</div></div></div>'
        for i, p in enumerate(brief.priorities, 1))
    plan = "".join(
        f'<div class="list-group-item"><div class="row g-3 align-items-center">'
        f'<div class="col-auto"><span class="badge bg-blue-lt brief-time">{esc(b.start)}–{esc(b.end)}</span></div>'
        f'<div class="col min-w-0">{esc(b.activity)}</div></div></div>' for b in brief.plan)
    risks = "".join(f'<div class="alert alert-warning mb-2"><i class="ti ti-alert-triangle me-1"></i>{esc(r)}</div>'
                    for r in brief.risks)
    groups = ""
    if pri:
        groups += f'<div class="list-group-header">先做这几件</div>{pri}'
    if plan:
        groups += f'<div class="list-group-header">今天的时间安排</div>{plan}'
    return (f'<div class="col-12"><section id="brief" class="card"><div class="card-header">'
            f'<h3 class="card-title"><i class="ti ti-compass me-1"></i>今日简报</h3>'
            f'<div class="card-actions"><span class="badge bg-purple-lt">AI Agent 智能体生成 · 仅供参考</span></div></div>'
            f'<div class="card-body"><p class="brief-headline">{esc(brief.headline)}</p></div>'
            + (f'<div class="list-group list-group-flush">{groups}</div>' if groups else "")
            + (f'<div class="card-body">{risks}</div>' if risks else "")
            + (f'<div class="card-footer text-secondary"><i class="ti ti-history me-1"></i>{esc(brief.changes)}</div>'
               if brief.changes else "")
            + '</section></div>')


def changes_card(changes: dict, colors: dict) -> str:
    """和上一份快照比的变化（代码算出，不依赖 AI）。"""
    if not changes:
        return ""
    def when(iso):
        d = datetime.fromisoformat(iso)
        return f"{d.month}/{d.day} {WEEKDAYS_CN[d.weekday()]} {d:%H:%M}"
    rows = []
    for t in changes.get("new_tasks", []):
        rows.append(("plus", "green", "新作业", f'{course_badge(t["course"], colors)} {esc(t["title"])} · {when(t["due"])} 截止'))
    for t in changes.get("completed", []):
        rows.append(("check", "green", "已交", f'{course_badge(t["course"], colors)} {esc(t["title"])}'))
    for t in changes.get("due_changed", []):
        rows.append(("calendar-event", "orange", "截止改了",
                     f'{course_badge(t["course"], colors)} {esc(t["title"])} · {when(t["old_due"])} → <b>{when(t["due"])}</b>'))
    for t in changes.get("removed", []):
        rows.append(("trash", "secondary", "被撤下", f'{course_badge(t["course"], colors)} {esc(t["title"])}'))
    for a in changes.get("new_announcements", []):
        rows.append(("speakerphone", "blue", "新公告", f'{course_badge(a["course"], colors)} {esc(a["title"])}'))
    for c in changes.get("classes_added", []):
        rows.append(("calendar-plus", "orange", "课表新增", esc(c.replace("|", " · "))))
    for c in changes.get("classes_removed", []):
        rows.append(("calendar-minus", "orange", "课表去掉", esc(c.replace("|", " · "))))
    if not rows:
        return ""
    since = datetime.fromisoformat(changes["since"]) if changes.get("since") else None
    items = "".join(f'<div class="list-group-item py-2"><div class="row g-2 align-items-center"><div class="col-auto">'
                    f'<span class="badge bg-{c}-lt"><i class="ti ti-{i}"></i> {label}</span></div>'
                    f'<div class="col min-w-0 small">{body}</div></div></div>' for i, c, label, body in rows)
    return (f'<div class="col-12"><section id="changes" class="card"><div class="card-header"><h3 class="card-title">'
            f'<i class="ti ti-arrows-diff"></i> 和上次比</h3><div class="card-actions text-secondary small">'
            f'{"上次更新：" + when(since.isoformat()) if since else ""}</div></div>'
            f'<div class="list-group list-group-flush">{items}</div></section></div>')


def render(now: datetime, cfg: dict, classes: list[dict], online: list[dict], tasks: list[Task],
           announcements: list[dict], warnings: list[str], ai_model: str = "", brief=None, changes=None,
           state=None, archive=(), archive_base: str = "archive/") -> str:
    today = now.date()
    holidays = {str(h) for h in cfg["term"].get("holidays", [])}
    colors = course_colors([c["course"] for c in classes + online] + [t.course for t in tasks])
    week = week_number(today, cfg["term"])
    first, last = as_date(cfg["term"]["week1_monday"]), as_date(cfg["term"]["last_class_day"])
    total_weeks = (last - first).days // 7 + 1
    term_pct = max(0, min(100, round((today - first).days / max((last - first).days, 1) * 100)))
    overdue, pending, done = classify(now, tasks, cfg["term"])
    horizon = today + timedelta(days=14)
    soon = [t for t in pending if t.due.date() <= horizon]
    later = [t for t in pending if t.due.date() > horizon]
    today_cls = classes_on(today, classes, holidays)

    # 当天发的公告里提到 cancel 的课，在课表上提醒一下
    cancelled = {a["course"]: a["url"] for a in announcements
                 if a["when"] and a["when"].date() == today and re.search(r"cancel", a["title"] + a["preview"], re.I)}
    today_cls = [{**c, "warn_url": cancelled.get(c["course"], "")} for c in today_cls]

    p = [f'<div class="page-header"><div class="row g-2 align-items-center"><div class="col">'
         f'<div class="page-pretitle" id="greet">你好</div><h2 class="page-title">Wendy 的今日</h2>'
         f'<div class="text-secondary mt-1">{today.month}月{today.day}日 {WEEKDAYS_CN[today.weekday()]} · '
         f'{esc(cfg["term"]["name"])}</div></div><div class="col-auto">'
         f'<div class="text-end small text-secondary mb-1">Week {week} / {total_weeks}</div>'
         f'<div class="progress progress-sm term-progress"><div class="progress-bar" style="width:{term_pct}%"></div></div>'
         f'</div></div></div>']
    p += [f'<div class="alert alert-warning mt-3 mb-0" role="alert"><i class="ti ti-alert-triangle"></i> {esc(w)}</div>'
          for w in warnings]

    p.append('<nav class="nav-sticky"><div class="nav nav-pills">'
             '<a class="nav-link" href="#today"><i class="ti ti-sun"></i> 今天</a>'
             '<a class="nav-link" href="#todo"><i class="ti ti-checklist"></i> 待办</a>'
             '<a class="nav-link" href="#timetable"><i class="ti ti-calendar-week"></i> 课程表</a>'
             '<a class="nav-link" href="#news"><i class="ti ti-speakerphone"></i> 公告</a></div></nav>')

    p.append('<div class="row row-cards">')
    p.append(stat_card("school", "blue", len(today_cls), "今天的课", "#today"))
    p.append(stat_card("alarm", "red", sum(t.due.date() == today for t in pending), "今天截止", "#todo"))
    p.append(stat_card("hourglass-high", "orange", sum(t.due - now <= timedelta(days=3) for t in pending), "3 天内截止", "#todo"))
    p.append(stat_card("alert-triangle", "red" if overdue else "green", len(overdue), "逾期未交", "#todo"))
    p.append(next_card("next-class", "下一节课", "clock"))
    p.append(next_card("next-due", "最近的截止", "flag"))
    if brief:
        p.append(brief_card(brief, tasks, colors))
    p.append(changes_card(changes or {}, colors))
    meme_html, meme_data = meme_card(now)
    p.append(meme_html)

    # 今天的课 + 明天预告
    rows = "".join(class_html(c, colors) for c in today_cls) or '<div class="list-group-item text-secondary">今天没有课 🎉</div>'
    tmr = classes_on(today + timedelta(days=1), classes, holidays)
    tmr_html = ("、".join(f'{c["start_dt"]:%H:%M} {esc(c["course"])} {esc(c.get("kind", ""))}' for c in tmr)
                if tmr else "没有课")
    p.append(f'<div class="col-12"><section id="today" class="card"><div class="card-header"><h3 class="card-title">'
             f'<i class="ti ti-sun"></i> 今天的课</h3><div class="card-actions text-secondary small">'
             f'{WEEKDAYS_CN[today.weekday()]} · {len(today_cls)} 节</div></div>'
             f'<div class="list-group list-group-flush">{rows}</div>'
             f'<div class="card-footer text-secondary small"><i class="ti ti-arrow-right"></i> 明天：{tmr_html}</div></section></div>')

    # 待办
    body = []
    if overdue:
        body.append(f'<div class="list-group-header text-danger">逾期未交 · {len(overdue)}</div>')
        body += [task_html(t, now, colors, "overdue") for t in overdue]
    groups: dict[date, list[Task]] = {}
    for t in soon:
        groups.setdefault(t.due.date(), []).append(t)
    for day, ts in groups.items():
        body.append(f'<div class="list-group-header{" text-primary" if day == today else ""}">'
                    f'{day_heading(day, today)} · {len(ts)}</div>')
        body += [task_html(t, now, colors, "soon") for t in ts]
    if not body:
        body.append('<div class="list-group-item text-secondary">两周内没有要交的 🎉</div>')
    extra = ""
    if later:
        extra += (f'<details class="card-footer"><summary class="text-primary fw-medium">更远的 {len(later)} 项</summary>'
                  f'<div class="list-group list-group-flush mt-2">{"".join(task_html(t, now, colors, "later") for t in later)}</div></details>')
    if done:
        extra += (f'<details class="card-footer"><summary class="text-success fw-medium">最近已交 {len(done)} 项</summary>'
                  f'<div class="list-group list-group-flush mt-2">{"".join(task_html(t, now, colors, "done") for t in done)}</div></details>')
    p.append(f'<div class="col-12"><section id="todo" class="card"><div class="card-header"><h3 class="card-title">'
             f'<i class="ti ti-checklist"></i> 接下来两周要交的</h3><div class="card-actions text-secondary small">'
             f'Top Hat / iMath 做完请自己打勾</div></div><div class="list-group list-group-flush">{"".join(body)}</div>'
             f'{extra}</section></div>')

    # 课程表（FullCalendar）
    online_note = ""
    if online:
        names = "、".join(sorted({f'{c["course"]} {c["kind"]}' for c in online}))
        online_note = f'<div class="card-footer text-secondary small"><i class="ti ti-world"></i> 线上课，没有固定上课时间：{esc(names)}</div>'
    p.append(f'<div class="col-12"><section id="timetable" class="card"><div class="card-header"><h3 class="card-title">'
             f'<i class="ti ti-calendar-week"></i> 课程表</h3><div class="card-actions text-secondary small">'
             f'自动读取 UCR 选课系统 · 最上面一行是当天的截止</div></div>'
             f'<div class="card-body"><div id="calendar"></div></div>{online_note}</section></div>')

    # 公告
    recent = [a for a in announcements if a["when"] and a["when"] >= now - timedelta(days=10)]
    items = []
    for a in recent:
        dot = "status-dot status-blue status-dot-animated" if a["unread"] else "status-dot status-secondary"
        mentions = "".join(f'<div class="alert alert-warning py-2 px-3 mb-0 mt-2 small"><i class="ti ti-calendar-due"></i> '
                           f'<b>{d.month}/{d.day} {WEEKDAYS_CN[d.weekday()]} {d:%H:%M}</b>　{esc(s)}</div>'
                           for m in a["mentions"] for d, s in [(m["due"], m["what"] or m["quote"])])
        unread = ' <span class="badge bg-blue-lt ms-1">未读</span>' if a["unread"] else ""
        summary = (f'<div class="mt-2">{esc(a["summary"])}</div>'
                   f'<div class="text-secondary small mt-1 original">原标题：{esc(a["title"])}</div>' if a.get("summary") else "")
        items.append(f'<div class="list-group-item"><div class="row g-3"><div class="col-auto pt-1"><span class="{dot} d-block"></span></div>'
                     f'<div class="col min-w-0"><div class="fw-medium">{link(a.get("zh_title") or a["title"], a["url"])}'
                     f'{unread}</div>'
                     f'<div class="text-secondary small mt-1 d-flex gap-2 align-items-center">{course_badge(a["course"], colors)}'
                     f'{rel_day(a["when"].date(), today)} {a["when"]:%H:%M}</div>'
                     f'{summary}<div class="text-secondary small mt-2 preview">{esc(a["preview"])}</div>{mentions}</div></div></div>')
    if items:
        p.append(f'<div class="col-12"><section id="news" class="card"><div class="card-header"><h3 class="card-title">'
                 f'<i class="ti ti-speakerphone"></i> 最近的公告</h3><div class="card-actions text-secondary small">'
                 f'带日期的句子会自动标出来</div></div><div class="list-group list-group-flush">{"".join(items)}</div></section></div>')
    p.append('</div>')
    if archive:
        links = " · ".join(f'<a href="{esc(archive_base)}{d}.html">{int(d[5:7])}/{int(d[8:10])}</a>' for d in archive[:14])
        p.append(f'<div class="text-center small mt-4"><i class="ti ti-history"></i> 历史快照：{links}</div>')
    p.append(f'<footer class="text-center text-secondary small mt-4">数据更新于 {now:%-m/%-d %H:%M}（太平洋时间）· '
             f'每天自动更新 4 次 · 倒计时实时计算{" · 中文由 " + esc(ai_model) + " 翻译" if ai_model else ""}<br>'
             f'用 <a href="https://tabler.io" target="_blank" rel="noopener">Tabler</a> 和 '
             f'<a href="https://fullcalendar.io" target="_blank" rel="noopener">FullCalendar</a> 构建</footer>')

    # 给前端的数据："下一节课 / 最近截止"卡片和课程表
    upcoming = []
    for i in range(8):
        upcoming += classes_on(today + timedelta(days=i), classes, holidays)
    data = {
        "classes": [{"course": c["course"], "kind": c.get("kind", ""), "where": c.get("where", ""),
                     "start": c["start_dt"].isoformat(), "end": c["end_dt"].isoformat(),
                     "color": HEX[colors.get(c["course"], "secondary")]} for c in upcoming],
        "tasks": [{"id": t.id, "title": t.zh or t.title, "course": t.course, "due": t.due.isoformat(), "url": t.url,
                   "manual": t.manual, "color": HEX[colors.get(t.course, "secondary")]} for t in pending],
        "calendar": calendar_payload(today, cfg, classes, tasks, colors),
        "meme": meme_data,
        "state": state or {},
    }
    page = TEMPLATE.read_text(encoding="utf-8")
    return (page.replace("{{BODY}}", "\n".join(p))
                .replace("{{DATA}}", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="_build/today/index.html")
    ap.add_argument("--config", default=str(HERE / "schedule.toml"))
    ap.add_argument("--now", help="调试用：假装现在是这个时间（ISO 格式）")
    ap.add_argument("--prev", help="上一份（已解密的）页面，用来算和上次比的变化")
    ap.add_argument("--archive-dates", help="线上已有的历史快照日期（JSON：{\"dates\": [...]}）")
    ap.add_argument("--archive-base", default="archive/", help="历史快照的链接前缀")
    args = ap.parse_args(argv)

    now = datetime.fromisoformat(args.now).astimezone(TZ) if args.now else datetime.now(TZ)
    warnings: list[str] = []
    config = Path(args.config)
    if not config.exists():  # 真实课表不进公开仓库；云端没配 SCHEDULE_TOML 时退回示例
        config = HERE / "schedule.example.toml"
        warnings.append("没有找到课表（SCHEDULE_TOML），课表部分显示的是示例")
    with open(config, "rb") as f:
        cfg = tomllib.load(f)
    names = {int(k): v for k, v in cfg.get("courses", {}).items()}

    courses, tasks, announcements, ai_model = [], [], [], ""
    if not os.environ.get("CANVAS_TOKEN"):
        warnings.append("没读到 Canvas：没有设置 CANVAS_TOKEN。下面只有课表和固定作业")
    else:
        try:
            courses = canvas_get("courses", {"enrollment_state": "active", "include[]": "sections", "per_page": 100})
            tasks = canvas_tasks(now, names)
            announcements = canvas_announcements(now, names, courses)
            ai = enrich(now, tasks, announcements)
            if ai:
                apply_ai(ai, tasks, announcements)
                ai_model = ai["model"]
            tasks += announcement_tasks(announcements, tasks, now)
        except urllib.error.HTTPError as e:
            reason = "token 失效或过期（401），需要重新生成" if e.code == 401 else f"返回 HTTP {e.code}"
            warnings.append(f"没读到 Canvas：{reason}。下面只有课表和固定作业")
        except Exception as e:  # noqa: BLE001 - 网络问题不应让整个页面生成失败
            warnings.append(f"没读到 Canvas：连接出错（{type(e).__name__}）。下面只有课表和固定作业")
    tasks += recurring_tasks(now, cfg)

    timed, online = [], []
    sections = enrolled_sections(courses)
    if sections:
        try:
            timed, online = banner_classes(sections)
        except Exception as e:  # noqa: BLE001 - 选课系统偶尔维护，不影响其他部分
            warnings.append(f"没读到选课系统的课表（{type(e).__name__}），课表只显示手动填写的部分")
    cfg["term"] = derive_term(cfg.get("term", {}), timed, sections, now.date())
    notes = cfg.get("notes", [])
    classes = apply_notes(timed, notes) + cfg.get("classes", [])  # [[classes]] 是手动添加的额外日程
    online = apply_notes(online, notes)

    state = make_state(now, tasks, announcements, classes)
    prev_state = load_prev_state(args.prev)
    changes = diff(prev_state, state)
    kinds = [k for k in changes if k != "since"]
    print(f"  · 快照对比：{'没有上一份（第一次运行或解密失败）' if not prev_state else f'{len(kinds)} 类变化：' + '、'.join(kinds) if kinds else '和上一份一样'}")
    brief, brief_info = brief_agent.run_brief(agent_deps(now, cfg, tasks, classes, announcements, changes))
    print(f"  · 今日简报：{brief_info}")
    archive = []
    if args.archive_dates and Path(args.archive_dates).exists():
        archive = [d for d in json.loads(Path(args.archive_dates).read_text()).get("dates", []) if d != now.date().isoformat()]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(now, cfg, classes, online, tasks, announcements, warnings, ai_model,
                          brief=brief, changes=changes, state=state, archive=archive, archive_base=args.archive_base),
                   encoding="utf-8")
    count = lambda src: sum(t.source == src for t in tasks)  # noqa: E731
    print(f"✓ {out}  Canvas {count('Canvas')} 项 · 公告里读到 {count('公告')} 项 · "
          f"固定作业 {len(tasks) - count('Canvas') - count('公告')} 项 · 公告 {len(announcements)} 条 · "
          f"课 {len(timed)} 个时段 + 线上 {len(online)} 门（来自选课系统）"
          + (f" · 中文和截止日期由 {ai_model} 处理" if ai_model else " · 未用 AI（规则方式）")
          + "".join(f"\n  ⚠️ {w}" for w in warnings))


if __name__ == "__main__":
    main()
