#!/usr/bin/env python3
"""生成 Wendy 的"今日助理"页面：今天要上的课、要交的作业和截止时间，每项带倒计时。

数据来源：
  1. Canvas API（环境变量 CANVAS_TOKEN）：作业、测验、讨论的截止时间和提交状态，最近的公告
  2. assistant/schedule.toml：课表，以及 Canvas 读不到的固定作业（Top Hat、iMath）

用法：
  python3 assistant/build_today.py --out _build/today/index.html
只用标准库。拿不到 Canvas 数据时照样生成页面，并在页面顶部显示原因。
"""
from __future__ import annotations

import argparse
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
        "start_date": (now - timedelta(days=14)).date().isoformat(),
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


def deadline_mentions(text: str, now: datetime) -> list[tuple[datetime, str]]:
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
            found.append((datetime.combine(d, at, TZ), sentence.strip()[:200]))
    return found


def canvas_announcements(now: datetime, names: dict[int, str]) -> list[dict]:
    courses = canvas_get("courses", {"enrollment_state": "active", "per_page": 100})
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
                    "preview": text[:220] + ("…" if len(text) > 220 else ""),
                    "mentions": deadline_mentions(text, now)})
    return sorted(out, key=lambda a: a["when"], reverse=True)


def announcement_tasks(anns: list[dict], canvas: list[Task], now: datetime) -> list[Task]:
    """公告里提到的未来 30 天的截止日期 → 待办；同一门课同一天已有 Canvas 作业的不重复添加。"""
    taken = {(t.course, t.due.date()) for t in canvas}
    out, seen = [], set()
    for a in anns:
        for due, sentence in a["mentions"]:
            key = (a["course"], due.date())
            if not now <= due <= now + timedelta(days=30) or key in taken or key in seen:
                continue
            seen.add(key)
            out.append(Task(id=f"ann-{a['id']}-{due:%m%d}", title=f"公告提到：{a['title']}", course=a["course"],
                            due=due, url=a["url"], source="公告", note=f"“{sentence}” —— 日期是从公告里自动读的，请点开核对"))
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


def classes_on(day: date, cfg: dict) -> list[dict]:
    if day.isoformat() in {str(h) for h in cfg["term"].get("holidays", [])}:
        return []
    out = []
    for c in cfg.get("classes", []):
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


# ---------- rendering ----------

def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt_due(dt: datetime) -> str:
    return f"{dt.month}/{dt.day} {WEEKDAYS_CN[dt.weekday()]} {dt:%H:%M}"


def day_heading(day: date, today: date) -> str:
    rel = {0: "今天", 1: "明天", 2: "后天"}.get((day - today).days, "")
    label = f"{day.month}月{day.day}日 {WEEKDAYS_CN[day.weekday()]}"
    return f"{rel} · {label}" if rel else label


def countdown_text(delta: timedelta) -> str:
    """服务器端的初始文字；打开页面后由 JS 实时刷新。"""
    s = int(delta.total_seconds())
    if s < 0:
        return "已过截止"
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"还剩 {d}天{h}小时" if d else f"还剩 {h}小时{m}分" if h else f"还剩 {m}分"


def task_html(t: Task, now: datetime) -> str:
    meta = [f'<span class="course">{esc(t.course)}</span>', esc(t.kind if not t.manual else t.source)]
    if t.points:
        meta.append(f"{t.points:g} 分")
    meta.append(f"截止 {fmt_due(t.due)}")
    title = f'<a href="{esc(t.url)}" target="_blank" rel="noopener">{esc(t.title)}</a>' if t.url else esc(t.title)
    note = f'<div class="note">{esc(t.note)}</div>' if t.note else ""
    check = (f'<label class="check"><input type="checkbox" data-key="{esc(t.id)}"> 我做完了</label>'
             if t.manual else "")
    badge = '<span class="tag done-tag">已交</span>' if t.done else ""
    return (f'<li class="task{" done" if t.done else ""}" data-due="{t.due.isoformat()}">'
            f'<div class="main"><div class="title">{title}{badge}</div>'
            f'<div class="meta">{" · ".join(meta)}</div>{note}{check}</div>'
            f'<div class="cd" data-due="{t.due.isoformat()}">{countdown_text(t.due - now)}</div></li>')


def class_html(c: dict) -> str:
    where = c.get("where", "")
    where_html = (f'<a href="{esc(where)}" target="_blank" rel="noopener">Zoom 链接</a>'
                  if where.startswith("http") else esc(where))
    span = f"{c['start_dt']:%H:%M}" + (f"–{c['end_dt']:%H:%M}" if c.get("end") else "")
    note = f" · {esc(c['note'])}" if c.get("note") else ""
    warn = (f'<a class="tag unread" href="{esc(c["warn_url"])}" target="_blank" rel="noopener">⚠️ 公告说可能取消</a>'
            if c.get("warn_url") else "")
    return (f'<li class="cls" data-start="{c["start_dt"].isoformat()}" data-end="{c["end_dt"].isoformat()}">'
            f'<div class="time">{span}</div><div class="main"><div class="title">'
            f'<span class="course">{esc(c["course"])}</span> {esc(c.get("kind", ""))}{warn}</div>'
            f'<div class="meta">{where_html}{note}</div></div><div class="cd cls-cd"></div></li>')


def render(now: datetime, cfg: dict, tasks: list[Task], announcements: list[dict], warnings: list[str]) -> str:
    today = now.date()
    week = week_number(today, cfg["term"])
    overdue = sorted((t for t in tasks if not t.done and not t.manual and t.due < now), key=lambda t: t.due)
    pending = sorted((t for t in tasks if not t.done and t.due >= now - timedelta(hours=12) and t not in overdue),
                     key=lambda t: t.due)
    done = sorted((t for t in tasks if t.done and t.due >= now - timedelta(days=3)), key=lambda t: t.due)
    horizon = today + timedelta(days=14)
    soon = [t for t in pending if t.due.date() <= horizon]
    later = [t for t in pending if t.due.date() > horizon]

    due_today = sum(1 for t in pending if t.due.date() == today)
    within3 = sum(1 for t in pending if t.due - now <= timedelta(days=3))
    chips = [f'<span class="chip red">今天截止 {due_today}</span>',
             f'<span class="chip orange">3 天内 {within3}</span>']
    if overdue:
        chips.insert(0, f'<span class="chip red solid">逾期未交 {len(overdue)}</span>')

    parts = [f'<header><h1>Wendy 的今日待办</h1>'
             f'<div class="sub">{today.month}月{today.day}日 {WEEKDAYS_CN[today.weekday()]} · '
             f'{esc(cfg["term"]["name"])} Week {week}</div><div class="chips">{"".join(chips)}</div></header>']
    parts += [f'<div class="alert">⚠️ {esc(w)}</div>' for w in warnings]

    if overdue:
        parts.append('<section><h2>🔴 逾期未交</h2><p class="hint">大多数作业晚交还能拿部分分，尽快补交，或者联系老师。</p>'
                     f'<ul class="list">{"".join(task_html(t, now) for t in overdue)}</ul></section>')

    # 当天发的公告里提到 cancel 的课，在课表上提醒一下
    cancelled = {a["course"]: a["url"] for a in announcements
                 if a["when"] and a["when"].date() == today and re.search(r"cancel", a["title"] + a["preview"], re.I)}
    for label, day in (("今天的课", today), ("明天的课", today + timedelta(days=1))):
        cls = classes_on(day, cfg)
        if day == today:
            cls = [{**c, "warn_url": cancelled.get(c["course"], "")} for c in cls]
        body = (f'<ul class="list">{"".join(class_html(c) for c in cls)}</ul>' if cls
                else '<p class="empty">没有课 🎉</p>')
        parts.append(f'<section><h2>{label}</h2>{body}</section>')

    groups: dict[date, list[Task]] = {}
    for t in soon:
        groups.setdefault(t.due.date(), []).append(t)
    parts.append('<section><h2>接下来两周要交的</h2>')
    if not groups:
        parts.append('<p class="empty">两周内没有截止的任务。</p>')
    for day, ts in groups.items():
        parts.append(f'<h3>{day_heading(day, today)}</h3><ul class="list">'
                     f'{"".join(task_html(t, now) for t in ts)}</ul>')
    parts.append('<p class="hint">Top Hat、iMath 的完成情况 Canvas 看不到，做完请自己勾选（只保存在这台设备上）。</p></section>')

    if later:
        parts.append(f'<details><summary>更远的 {len(later)} 项</summary><ul class="list">'
                     f'{"".join(task_html(t, now) for t in later)}</ul></details>')
    if done:
        parts.append(f'<details><summary>最近已交 {len(done)} 项 ✅</summary><ul class="list">'
                     f'{"".join(task_html(t, now) for t in done)}</ul></details>')

    recent = [a for a in announcements if a["when"] and a["when"] >= now - timedelta(days=10)]
    if recent:
        items = []
        for a in recent:
            unread = '<span class="tag unread">未读</span>' if a["unread"] else ""
            mentions = "".join(f'<div class="mention">📅 <b>{fmt_due(d)}</b>　{esc(s)}</div>' for d, s in a["mentions"])
            items.append(f'<li><a href="{esc(a["url"])}" target="_blank" rel="noopener">{esc(a["title"])}</a>{unread}'
                         f'<div class="meta"><span class="course">{esc(a["course"])}</span> · '
                         f'{a["when"].month}/{a["when"].day} {a["when"]:%H:%M}</div>'
                         f'<div class="preview">{esc(a["preview"])}</div>{mentions}</li>')
        parts.append('<section><h2>📢 最近的公告</h2><p class="hint">公告里带日期的句子会自动标出来，'
                     f'未来 30 天内的也加进了上面的待办。</p><ul class="ann">{"".join(items)}</ul></section>')

    unscheduled = [c for c in cfg.get("classes", []) if not c.get("days")]
    if unscheduled:
        names = "、".join(f'{c["course"]} {c.get("kind", "")}'.strip() for c in unscheduled)
        parts.append(f'<p class="hint foot-note">课表还缺：{esc(names)}。在 assistant/schedule.toml 里补上时间就会显示。</p>')

    parts.append(f'<footer>数据更新于 {now:%-m/%-d %H:%M}（太平洋时间），每天自动更新 4 次。倒计时是实时计算的。</footer>')
    return PAGE.replace("{{BODY}}", "\n".join(parts))


PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wendy 今日待办</title>
<style>
:root{--bg:#f6f7fb;--card:#fff;--text:#1b1d22;--muted:#6b7080;--line:#e6e8ef;--blue:#2d6cdf;
--red:#d93025;--red-bg:#fdecea;--orange:#c26401;--orange-bg:#fff3e0;--green:#1e8e3e;--green-bg:#e6f4ea}
@media (prefers-color-scheme:dark){:root{--bg:#121318;--card:#1c1e25;--text:#e8e9ee;--muted:#9aa0ad;
--line:#2c2f39;--blue:#7aa7ff;--red:#ff7b72;--red-bg:#3a1d1d;--orange:#ffb35c;--orange-bg:#3a2a14;
--green:#6fd08c;--green-bg:#17301f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,"PingFang SC","Noto Sans SC",sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:24px;margin:0}.sub{color:var(--muted);margin:2px 0 10px}
h2{font-size:17px;margin:0 0 10px}h3{font-size:14px;color:var(--muted);margin:14px 0 6px;font-weight:600}
section,details{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:14px 0}
summary{cursor:pointer;font-weight:600}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{font-size:13px;padding:3px 10px;border-radius:999px;border:1px solid var(--line)}
.chip.red{color:var(--red);background:var(--red-bg);border-color:transparent}
.chip.orange{color:var(--orange);background:var(--orange-bg);border-color:transparent}
.chip.solid{background:var(--red);color:#fff}
.alert{background:var(--red-bg);color:var(--red);border-radius:12px;padding:10px 14px;margin:12px 0}
.list,.ann{list-style:none;margin:0;padding:0}
.task,.cls{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line)}
.list>li:first-child{border-top:0}
.main{flex:1;min-width:0}.title{font-weight:600;overflow-wrap:anywhere}
.title a{color:inherit;text-decoration:none;border-bottom:1px dashed var(--muted)}
.meta,.note{font-size:13px;color:var(--muted)}.note{margin-top:2px}
.meta a,.ann a,footer a{color:var(--blue)}
.course{display:inline-block;font-size:12px;font-weight:600;color:var(--blue);background:color-mix(in srgb,var(--blue) 12%,transparent);padding:0 6px;border-radius:6px}
.time{font-variant-numeric:tabular-nums;font-weight:600;min-width:92px}
.cd{flex:none;font-size:13px;font-weight:600;padding:3px 8px;border-radius:8px;white-space:nowrap;background:var(--bg);color:var(--muted);font-variant-numeric:tabular-nums}
.cd.u-over,.cd.u-24{background:var(--red-bg);color:var(--red)}.cd.u-72{background:var(--orange-bg);color:var(--orange)}
.cd.u-week{color:var(--blue)}.cd.live{background:var(--green-bg);color:var(--green)}
.task.done .title,.task.checked .title{text-decoration:line-through;color:var(--muted)}
.task.done .cd,.task.checked .cd{visibility:hidden}
.tag{font-size:11px;margin-left:6px;padding:1px 6px;border-radius:6px;vertical-align:2px}
.done-tag{background:var(--green-bg);color:var(--green)}.unread{background:var(--orange-bg);color:var(--orange)}
.preview{font-size:13px;margin-top:4px;overflow-wrap:anywhere}
.mention{font-size:13px;margin-top:6px;padding:6px 8px;border-radius:8px;background:var(--orange-bg);overflow-wrap:anywhere}
.check{display:inline-flex;gap:6px;align-items:center;font-size:13px;margin-top:4px;color:var(--muted)}
.ann li{padding:8px 0;border-top:1px solid var(--line)}.ann li:first-child{border-top:0}
.empty{color:var(--muted);margin:0}.hint{font-size:13px;color:var(--muted);margin:8px 0 0}
.foot-note{margin:14px 4px}
footer{font-size:12px;color:var(--muted);text-align:center;margin-top:20px}
@media (max-width:480px){.task,.cls{flex-wrap:wrap}.cd{order:-1}.time{min-width:0;width:100%}}
</style></head>
<body><div class="wrap">
{{BODY}}
</div>
<script>
(function(){
  function fmt(ms){
    var s=Math.floor(Math.abs(ms)/1000),d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),x=s%60;
    return d?d+"天"+h+"小时":h?h+"小时"+m+"分":m+"分"+x+"秒";
  }
  function tick(){
    var now=Date.now();
    document.querySelectorAll(".task .cd").forEach(function(el){
      var ms=new Date(el.dataset.due)-now;
      el.textContent=ms<0?"已过截止 "+fmt(ms):"还剩 "+fmt(ms);
      el.className="cd "+(ms<0?"u-over":ms<864e5?"u-24":ms<2592e5?"u-72":ms<6048e5?"u-week":"");
    });
    document.querySelectorAll(".cls").forEach(function(li){
      var el=li.querySelector(".cls-cd"),a=new Date(li.dataset.start)-now,b=new Date(li.dataset.end)-now;
      if(a>0){el.textContent=fmt(a)+"后上课";el.className="cd cls-cd"+(a<36e5?" u-72":"");}
      else if(b>0){el.textContent="上课中";el.className="cd cls-cd live";}
      else{el.textContent="已结束";el.className="cd cls-cd";}
    });
  }
  function store(){try{return window.localStorage}catch(e){return null}}
  var ls=store();
  document.querySelectorAll(".check input").forEach(function(box){
    var key="wendy-done:"+box.dataset.key,li=box.closest(".task");
    try{box.checked=!!(ls&&ls.getItem(key));}catch(e){}
    li.classList.toggle("checked",box.checked);
    box.addEventListener("change",function(){
      li.classList.toggle("checked",box.checked);
      try{box.checked?ls.setItem(key,"1"):ls.removeItem(key);}catch(e){}
    });
  });
  tick();setInterval(tick,1000);
})();
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="_build/today/index.html")
    ap.add_argument("--config", default=str(HERE / "schedule.toml"))
    ap.add_argument("--now", help="调试用：假装现在是这个时间（ISO 格式）")
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now).astimezone(TZ) if args.now else datetime.now(TZ)
    warnings: list[str] = []
    config = Path(args.config)
    if not config.exists():  # 真实课表不进公开仓库；云端没配 SCHEDULE_TOML 时退回示例
        config = HERE / "schedule.example.toml"
        warnings.append("没有找到课表（SCHEDULE_TOML），课表部分显示的是示例")
    with open(config, "rb") as f:
        cfg = tomllib.load(f)
    names = {int(k): v for k, v in cfg.get("courses", {}).items()}

    tasks, announcements = [], []
    if not os.environ.get("CANVAS_TOKEN"):
        warnings.append("没读到 Canvas：没有设置 CANVAS_TOKEN。下面只有课表和固定作业")
    else:
        try:
            tasks = canvas_tasks(now, names)
            announcements = canvas_announcements(now, names)
            tasks += announcement_tasks(announcements, tasks, now)
        except urllib.error.HTTPError as e:
            reason = "token 失效或过期（401），需要重新生成" if e.code == 401 else f"返回 HTTP {e.code}"
            warnings.append(f"没读到 Canvas：{reason}。下面只有课表和固定作业")
        except Exception as e:  # noqa: BLE001 - 网络问题不应让整个页面生成失败
            warnings.append(f"没读到 Canvas：连接出错（{type(e).__name__}）。下面只有课表和固定作业")
    tasks += recurring_tasks(now, cfg)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(now, cfg, tasks, announcements, warnings), encoding="utf-8")
    count = lambda src: sum(t.source == src for t in tasks)  # noqa: E731
    print(f"✓ {out}  Canvas {count('Canvas')} 项 · 公告里读到 {count('公告')} 项 · "
          f"固定作业 {len(tasks) - count('Canvas') - count('公告')} 项 · 公告 {len(announcements)} 条"
          + "".join(f"\n  ⚠️ {w}" for w in warnings))


if __name__ == "__main__":
    main()
