"""快照：每次生成页面时把状态存进页面数据；下次运行和上一份比较，得出"和上次比有什么变化"。

变化全部由代码算出（确定、可测试），AI 只负责把它们讲成人话、给建议。
上一份快照从线上取回（加密的），在 CI 里用网站口令解密，不在任何地方留明文、也不需要提交或缓存。

命令行（给 GitHub Actions 用，只用标准库）：
  python assistant/snapshot.py fetch --base URL --prev-dir _prev --archive-dir _site/today/archive
  python assistant/snapshot.py add   --site-dir _site/today --date 2026-09-29 --keep 30
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

TRACKED = {"Canvas", "公告"}   # 固定作业（Top Hat / iMath）每周自动生成，不算"新出现"


# ---------- 状态和对比 ----------

def make_state(now, tasks, announcements, classes) -> dict:
    return {
        "generated": now.isoformat(),
        "tasks": {t.id: {"title": t.zh or t.title, "course": t.course, "due": t.due.isoformat(),
                         "done": t.done, "source": t.source} for t in tasks if t.source in TRACKED},
        "announcements": {str(a["id"]): {"title": a.get("zh_title") or a["title"], "course": a["course"]}
                          for a in announcements},
        "classes": sorted({f'{c["course"]}|{c.get("kind", "")}|{",".join(c.get("days", []))}|'
                           f'{c.get("start", "")}-{c.get("end", "")}|{c.get("where", "")}' for c in classes}),
    }


def diff(prev: dict | None, cur: dict) -> dict:
    """和上一份快照比：新作业、刚交掉的、改了截止时间的、被撤下的、新公告、课表变化。"""
    if not prev:
        return {}
    p, c = prev.get("tasks", {}), cur["tasks"]
    now = cur["generated"]
    out = {
        "since": prev.get("generated", ""),
        "new_tasks": [dict(id=k, **v) for k, v in c.items() if k not in p and not v["done"]],
        "completed": [dict(id=k, **v) for k, v in c.items() if k in p and v["done"] and not p[k]["done"]],
        "due_changed": [dict(id=k, old_due=p[k]["due"], **v) for k, v in c.items()
                        if k in p and v["due"] != p[k]["due"]],
        # 只报"还没到截止就不见了"的（老师撤下、改了），正常过期消失的不报
        "removed": [dict(id=k, **v) for k, v in p.items()
                    if k not in c and not v["done"] and v["due"] > now],
        "new_announcements": [dict(id=k, **v) for k, v in cur["announcements"].items()
                              if k not in prev.get("announcements", {})],
        "classes_added": [x for x in cur["classes"] if x not in set(prev.get("classes", []))],
        "classes_removed": [x for x in prev.get("classes", []) if x not in set(cur["classes"])],
    }
    return {k: v for k, v in out.items() if v}


# ---------- 自改进：核对上次的建议有没有被执行，把经验一代代传下去 ----------

MAX_LESSONS, LESSON_LEN, KEEP_BRIEF_DAYS, KEEP_FEEDBACK = 5, 80, 7, 14


def evaluate(prev: dict | None, cur: dict, today: str) -> dict | None:
    """核对"今天之前最近一份简报"建议先做的事，现在怎么样了。结果由代码判断，不靠 AI。
    done=已交  missed=过了截止还没交  open=还没到截止  unverifiable=Canvas 之外的任务，看不到完成情况"""
    briefs = (prev or {}).get("briefs", {})
    past = sorted(d for d in briefs if d < today)
    if not past:
        return None
    day = past[-1]
    items = []
    for tid in briefs[day].get("priorities", []):
        t = cur["tasks"].get(tid) or (prev or {}).get("tasks", {}).get(tid)
        if not t:
            outcome = "unverifiable"
        elif t["source"] != "Canvas":
            outcome = "unverifiable"
        elif cur["tasks"].get(tid, {}).get("done"):
            outcome = "done"
        elif t["due"] < cur["generated"]:
            outcome = "missed"
        else:
            outcome = "open"
        items.append({"task_id": tid, "title": (t or {}).get("title", tid), "due": (t or {}).get("due", ""),
                      "outcome": outcome})
    count = {k: sum(i["outcome"] == k for i in items) for k in ("done", "missed", "open", "unverifiable")}
    return {"brief_date": day, "checked_at": cur["generated"], "items": items, **count}


def clean_lessons(lessons) -> list[str]:
    """经验条目的护栏：只要字符串、去空、去重、每条不超过 80 字、最多 5 条。"""
    out = []
    for x in lessons or []:
        x = " ".join(str(x).split())[:LESSON_LEN] if isinstance(x, str) else ""
        if x and x not in out:
            out.append(x)
    return out[:MAX_LESSONS]


def carry_memory(prev: dict | None, cur: dict, today: str, brief=None) -> dict:
    """算出这一次要存进快照的记忆：最近 7 天的简报、最近 14 次核对、当前的经验。
    智能体这次失败（brief=None）时，经验和历史原样保留，不会丢。"""
    prev = prev or {}
    briefs = {d: b for d, b in prev.get("briefs", {}).items()
              if d >= (date.fromisoformat(today) - timedelta(days=KEEP_BRIEF_DAYS)).isoformat()}
    if brief is not None:
        briefs[today] = {"priorities": [p.task_id for p in brief.priorities], "generated": cur["generated"]}
    feedback = [f for f in prev.get("feedback", []) if isinstance(f, dict)]
    ev = evaluate(prev, cur, today)
    if ev:
        feedback = [f for f in feedback if f.get("brief_date") != ev["brief_date"]] + [ev]
    lessons = clean_lessons(brief.lessons) if brief is not None and brief.lessons else clean_lessons(prev.get("lessons"))
    return {"briefs": briefs, "feedback": feedback[-KEEP_FEEDBACK:], "lessons": lessons}


def load_prev_state(path: str | None) -> dict | None:
    """从上一份（已解密的）页面里取出嵌在数据里的状态；拿不到就返回 None（第一次运行、解密失败等）。"""
    if not path or not Path(path).exists():
        return None
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>',
                  Path(path).read_text(encoding="utf-8", errors="ignore"), re.S)
    try:
        return json.loads(m.group(1).replace("<\\/", "</")).get("state") if m else None
    except ValueError:
        return None


# ---------- 线上归档：取回、追加、清理 ----------

def _get(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError):
        return None


def fetch(base: str, prev_dir: Path, archive_dir: Path) -> None:
    """取回上一份（加密的）今日页面和所有归档快照，下次部署时原样带上。"""
    base = base.rstrip("/")
    prev_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    page = _get(f"{base}/today/index.html")
    if page:
        (prev_dir / "index.html").write_bytes(page)
    manifest = _get(f"{base}/today/archive/index.json")
    dates = json.loads(manifest).get("dates", []) if manifest else []
    kept = []
    for d in dates:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and (body := _get(f"{base}/today/archive/{d}.html")):
            (archive_dir / f"{d}.html").write_bytes(body)
            kept.append(d)
    (prev_dir / "archive.json").write_text(json.dumps({"dates": kept}), encoding="utf-8")
    print(f"取回上一份页面：{'有' if page else '没有'}，归档快照 {len(kept)} 份")


def add(site_dir: Path, day: str, keep: int) -> None:
    """把这次（加密后的）页面存成当天的快照，只保留最近 keep 天。"""
    archive = site_dir / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(site_dir / "index.html", archive / f"{day}.html")
    cutoff = (date.fromisoformat(day) - timedelta(days=keep)).isoformat()
    dates = sorted((f.stem for f in archive.glob("*.html") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem)), reverse=True)
    for d in dates:
        if d <= cutoff:
            (archive / f"{d}.html").unlink()
    dates = [d for d in dates if d > cutoff]
    (archive / "index.json").write_text(json.dumps({"dates": dates}), encoding="utf-8")
    print(f"归档快照 {len(dates)} 份（最近 {keep} 天）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--base", required=True)
    f.add_argument("--prev-dir", default="_prev")
    f.add_argument("--archive-dir", default="_site/today/archive")
    a = sub.add_parser("add")
    a.add_argument("--site-dir", default="_site/today")
    a.add_argument("--date", required=True)
    a.add_argument("--keep", type=int, default=30)
    args = ap.parse_args()
    if args.cmd == "fetch":
        fetch(args.base, Path(args.prev_dir), Path(args.archive_dir))
    else:
        add(Path(args.site_dir), args.date, args.keep)
