"""每日助理的端到端测试：假数据 + 时间快进，检查状态机和页面不变量。"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest
from conftest import TZ, FakeCanvas, announcement, item, pt

URGENCY_ORDER = ["later", "soon", "overdue", "done", None]   # 一项作业的状态只能往后走


def check_invariants(page, now):
    """每一次生成页面都必须成立的规则。"""
    d = page.data
    # 1. "下一项截止"用的列表按时间排好，而且都还没截止太久
    dues = [t["due"] for t in d["tasks"]]
    assert dues == sorted(dues)
    # 2. 顶部卡片的数字和列表一致
    overdue = [k for k, v in page.states.items() if v == "overdue"]
    assert page.stats["逾期未交"] == len(overdue)
    assert page.stats["今天的课"] == len(page.today_classes)
    # 3. 逾期的一定已经过了截止时间；"两周内/更远"的一定还没过（固定作业允许过期 12 小时内）
    for tid, state in page.states.items():
        due = datetime.fromisoformat(re.search(rf'data-due="([^"]+)" data-id="{re.escape(tid)}"', page.html).group(1))
        if state == "overdue":
            assert due < now, tid
        if state in ("soon", "later"):
            assert due >= now - timedelta(hours=12), tid
    # 4. 已交的不会出现在"下一项截止"里
    done_ids = {k for k, v in page.states.items() if v == "done"}
    assert not done_ids & {t["id"] for t in d["tasks"]}


# ---------------------------------------------------------------- 作业状态机

def test_assignment_lifecycle_late_submission(build):
    """没按时交 → 逾期 → 晚交 → 已交 → 3 天后消失。状态只能往前走，不能跳回去。"""
    due = pt("2026-10-16 23:59")
    submit_at = due + timedelta(days=1)
    seen = []
    now = due - timedelta(days=16)
    while now <= due + timedelta(days=5):
        canvas = FakeCanvas(planner=[item(1, 1001, "Essay 1", due, submitted=now >= submit_at,
                                          missing=due < now < submit_at)])
        page = build(now, canvas)
        check_invariants(page, now)
        state = page.state("canvas-assignment-1")
        if not seen or seen[-1] != state:
            seen.append(state)
        now += timedelta(hours=6)
    assert seen == ["later", "soon", "overdue", "done", None], seen


def test_submitted_early_never_overdue(build):
    due = pt("2026-10-09 23:59")
    for now in [due - timedelta(days=3), due + timedelta(hours=1), due + timedelta(days=2)]:
        page = build(now, FakeCanvas(planner=[item(2, 1001, "Quiz", due, submitted=True)]))
        assert page.state("canvas-assignment-2") == "done"
    assert build(due + timedelta(days=4), FakeCanvas(planner=[item(2, 1001, "Quiz", due, submitted=True)])) \
        .state("canvas-assignment-2") is None


@pytest.mark.parametrize("flags", [{"excused": True}, {"marked": True}])
def test_excused_or_marked_complete_counts_as_done(build, flags):
    due = pt("2026-10-09 23:59")
    page = build(due + timedelta(hours=2), FakeCanvas(planner=[item(3, 1001, "Reading", due, **flags)]))
    assert page.state("canvas-assignment-3") == "done"
    assert page.stats["逾期未交"] == 0


def test_long_overdue_is_not_silently_dropped(build):
    """3 周前就该交、一直没交的作业，要一直留在"逾期"里。"""
    due = pt("2026-10-02 23:59")
    page = build(due + timedelta(days=21), FakeCanvas(planner=[item(4, 1001, "Lab 1", due, missing=True)]))
    assert page.state("canvas-assignment-4") == "overdue"


def test_non_submittable_item_never_overdue(build):
    """没有提交状态的项目（纸质作业、阅读页）过了截止就消失，不会永远挂在逾期里。"""
    due = pt("2026-10-02 10:00")
    canvas = FakeCanvas(planner=[item(5, 1001, "In-class worksheet", due, submittable=False)])
    assert build(due - timedelta(days=1), canvas).state("canvas-assignment-5") == "soon"
    assert build(due + timedelta(days=1), canvas).state("canvas-assignment-5") is None


def test_due_date_extended_moves_group(build):
    now = pt("2026-10-05 12:00")
    old = build(now, FakeCanvas(planner=[item(6, 1002, "Project", pt("2026-10-04 23:59"))]))
    assert old.state("canvas-assignment-6") == "overdue"
    new = build(now, FakeCanvas(planner=[item(6, 1002, "Project", pt("2026-10-30 23:59"))]))
    assert new.state("canvas-assignment-6") == "later"
    assert new.stats["逾期未交"] == 0


def test_new_assignment_appears_and_next_due_updates(build):
    now = pt("2026-10-05 12:00")
    canvas = FakeCanvas(planner=[item(7, 1001, "HW 1", pt("2026-10-09 23:59"))])
    assert build(now, canvas).data["tasks"][0]["title"] == "HW 1"
    canvas.planner.append(item(8, 1002, "Pop quiz", pt("2026-10-06 09:00")))
    page = build(now, canvas)
    assert page.state("canvas-assignment-8") == "soon"
    assert page.data["tasks"][0]["title"] == "Pop quiz"      # "最近的截止"换成了新的这项


# ---------------------------------------------------------------- 公告

def test_new_announcement_with_deadline_creates_task_once(build):
    now = pt("2026-10-05 09:00")
    msg = "<p>Please complete the survey by Friday, October 9th.</p><p>Also the reading is due 10/9.</p>"
    canvas = FakeCanvas(announcements=[announcement(50, 1001, "Survey", msg, now - timedelta(hours=2))])
    page = build(now, canvas)
    assert "Survey" in page.announcements
    ann_tasks = [k for k in page.states if k.startswith("ann-50-")]
    assert ann_tasks == ["ann-50-1009"]                     # 同一天提到两次，只加一项
    # 同一门课同一天已经有 Canvas 作业 → 不再重复加
    canvas.planner.append(item(9, 1001, "Survey", pt("2026-10-09 23:59")))
    page = build(now, canvas)
    assert not [k for k in page.states if k.startswith("ann-50-")]


def test_announcement_deadline_windows(build):
    now = pt("2026-10-05 09:00")
    msg = "Old thing was due 10/1. Final paper due December 11. Quiz by 10/12."
    page = build(now, FakeCanvas(announcements=[announcement(51, 1002, "Dates", msg, now)]))
    ids = {k for k in page.states if k.startswith("ann-51-")}
    assert ids == {"ann-51-1012"}          # 已经过去的、超过 30 天的都不加


def test_old_announcements_hidden_and_cancel_flag_only_same_day(build):
    lecture_day = pt("2026-10-06 08:00")    # 周二，DEMO 002A 大课 15:30
    canvas = FakeCanvas(announcements=[
        announcement(60, 1002, "CLASS CANCELLED TODAY", "Sorry, class is cancelled today.", lecture_day - timedelta(hours=1)),
        announcement(61, 1001, "Welcome", "Hello!", lecture_day - timedelta(days=12)),
    ])
    page = build(lecture_day, canvas)
    assert "Welcome" not in page.announcements                # 10 天前的公告不显示
    assert any("可能取消" in c for c in page.today_classes if "DEMO 002A" in c)
    thursday = build(pt("2026-10-08 08:00"), canvas)          # 过了当天就不再提示
    assert not any("可能取消" in c for c in thursday.today_classes)


def test_ai_enrichment_translates_and_replaces_regex_deadlines(build):
    now = pt("2026-10-05 09:00")
    canvas = FakeCanvas(planner=[item(10, 1001, "Week 2: Attendance Quiz", pt("2026-10-09 16:00"))],
                        announcements=[announcement(70, 1002, "Reminder", "Survey due next Friday.", now)])
    ai = {"model": "fake-model",
          "tasks": [{"id": "canvas-assignment-10", "zh": "第 2 周：出勤测验"}],
          "announcements": [{"id": "70", "zh_title": "提醒", "summary": "下周五前填问卷。",
                             "deadlines": [{"what": "填问卷", "due": "2026-10-09T23:59", "quote": "Survey due next Friday."}]}]}
    page = build(now, canvas, ai=ai)
    assert "第 2 周：出勤测验" in page.html and "Week 2: Attendance Quiz" in page.html   # 中文 + 原文
    assert "提醒" in page.announcements
    assert page.state("ann-70-1009") == "soon" and "填问卷" in page.html
    assert "fake-model" in page.html


@pytest.mark.parametrize("bad", [
    {"tasks": "oops", "announcements": None},
    {"tasks": [{"id": "canvas-assignment-10"}], "announcements": [{"id": "70", "deadlines": [{"due": "next week"}]}]},
    {"announcements": [{"id": "70", "deadlines": "none"}]},
])
def test_malformed_ai_output_does_not_break_page(build, bad):
    now = pt("2026-10-05 09:00")
    canvas = FakeCanvas(planner=[item(10, 1001, "Quiz", pt("2026-10-09 16:00"))],
                        announcements=[announcement(70, 1002, "Reminder", "text", now)])
    page = build(now, canvas, ai={"model": "x", **bad})
    assert page.state("canvas-assignment-10") == "soon"


def test_ai_unavailable_falls_back_to_rules(build):
    now = pt("2026-10-05 09:00")
    canvas = FakeCanvas(announcements=[announcement(71, 1001, "Due", "Homework due 10/7.", now)])
    page = build(now, canvas, ai=None)
    assert page.state("ann-71-1007") == "soon"


# ---------------------------------------------------------------- 周次、课表、学期

@pytest.mark.parametrize("now,week", [
    ("2026-09-25 12:00", "Week 0 / 10"), ("2026-09-28 08:00", "Week 1 / 10"),
    ("2026-10-12 08:00", "Week 3 / 10"), ("2026-12-04 08:00", "Week 10 / 10"),
])
def test_week_number_progression(build, now, week):
    assert build(pt(now)).week == week


def test_term_is_derived_from_banner(build):
    page = build(pt("2026-10-05 09:00"))
    assert "Fall 2026" in page.html


def test_classes_follow_banner_and_notes(build):
    # 周四：TEST 101 讨论课只在 10/1 之后出现（notes 里推迟了开始日期）
    assert not any("讨论课" in c for c in build(pt("2026-09-24 08:00")).today_classes)
    thu = build(pt("2026-10-01 08:00"))
    assert any("TEST 101 讨论课 021" in c for c in thu.today_classes)
    assert not any("022" in c for c in thu.today_classes)          # 没选的班不显示
    assert any("DEMO 002A 大课" in c for c in thu.today_classes)
    assert "线上课" in thu.html and "DEMO 002A Lab 021" in thu.html


def test_holiday_and_term_end_have_no_classes(build):
    assert build(pt("2026-11-11 08:00")).today_classes == []       # 放假（周三）
    assert build(pt("2026-12-07 08:00")).today_classes == []       # 学期结束后的周一
    assert "没有课" in build(pt("2026-12-04 20:00")).tomorrow        # 周六


def test_recurring_tasks_roll_forward_and_stop(build):
    week1 = build(pt("2026-10-01 12:00"))
    assert week1.state("HW-DEMO 002A-2026-10-02") == "soon"
    after = build(pt("2026-10-03 13:00"))                           # 截止过了 12 小时以上
    assert after.state("HW-DEMO 002A-2026-10-02") is None
    assert after.state("HW-DEMO 002A-2026-10-09") == "soon"
    end = build(pt("2026-12-05 12:00"))
    assert not [k for k in end.states if k.startswith("HW-")]     # 学期结束不再生成


def test_calendar_payload_matches_timetable(build):
    page = build(pt("2026-10-05 09:00"), FakeCanvas(planner=[
        item(11, 1001, "Pending", pt("2026-10-09 23:59")), item(12, 1001, "Done", pt("2026-10-08 23:59"), submitted=True)]))
    cal = page.data["calendar"]
    classes = [e for e in cal["events"] if not e.get("allDay")]
    # 学期 9/24–12/4：TEST 大课 MWF、讨论课 周四（10/1 起）、DEMO 大课 TR；放假日不上课
    per_day = {}
    for e in classes:
        per_day.setdefault(e["start"][:10], []).append(e["title"])
    assert "2026-11-11" not in per_day and "2026-11-26" not in per_day
    assert per_day["2026-10-01"] == ["DEMO 002A 大课", "TEST 101 讨论课 021"] or \
        sorted(per_day["2026-10-01"]) == sorted(["DEMO 002A 大课", "TEST 101 讨论课 021"])
    deadlines = [e["title"] for e in cal["events"] if e.get("allDay")]
    assert any("Pending" in t for t in deadlines) and not any("Done" in t for t in deadlines)
    assert cal["hiddenDays"] == [0, 6]


# ---------------------------------------------------------------- 时间相关的边界

def test_dst_change_keeps_wall_clock_due_time(build):
    due = pt("2026-11-06 23:59")                                    # 11/1 夏令时结束之后
    page = build(pt("2026-10-30 12:00"), FakeCanvas(planner=[item(13, 1001, "After DST", due)]))
    t = next(t for t in page.data["tasks"] if t["id"] == "canvas-assignment-13")
    assert t["due"] == "2026-11-06T23:59:00-08:00"


def test_meme_counts_whole_weeks_and_rolls_over(build):
    page = build(pt("2026-09-29 12:00"))
    assert '<span class="meme-num">38</span>' in page.html
    assert page.data["meme"]["year"] == 2026
    ny = build(pt("2027-01-01 00:30"), schedule=None)
    assert '<span class="meme-num">0</span>' in ny.html and ny.data["meme"]["year"] == 2027


# ---------------------------------------------------------------- 出错时的降级

def test_canvas_token_expired(build):
    page = build(pt("2026-10-05 09:00"), FakeCanvas(fail=401))
    assert any("token 失效" in w for w in page.warnings)
    assert page.states == {k: v for k, v in page.states.items() if k.startswith("HW-")}   # 只剩固定作业


def test_banner_down_still_builds(build):
    page = build(pt("2026-10-05 09:00"), banner_fail=True)
    assert any("选课系统" in w for w in page.warnings)


def test_no_token_and_no_schedule(build):
    page = build(pt("2026-10-05 09:00"), schedule=None, token=False)
    assert any("CANVAS_TOKEN" in w for w in page.warnings)
    assert any("课表" in w for w in page.warnings)


# ---------------------------------------------------------------- 前端脚本冒烟测试

CHROME = next((p for p in ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                           shutil.which("google-chrome") or "", shutil.which("chromium") or ""] if p and shutil.os.path.exists(p)), None)


@pytest.mark.skipif(CHROME is None, reason="没有 Chrome")
def test_frontend_script_runs(build, tmp_path):
    page = build(pt("2026-10-05 09:00"), FakeCanvas(planner=[item(14, 1001, "HW", pt("2027-06-01 23:59"))]))
    f = tmp_path / "p.html"
    f.write_text(page.html, encoding="utf-8")
    cmd = [CHROME, "--headless=new", "--disable-gpu", f"--user-data-dir={tmp_path / 'ud'}",
           "--virtual-time-budget=4000", "--dump-dom", f.as_uri()]
    try:
        dom = subprocess.run(cmd, capture_output=True, text=True, timeout=40).stdout
    except subprocess.TimeoutExpired as e:   # 页面上有每秒刷新的定时器，Chrome 输出 DOM 后可能不退出
        dom = (e.output or b"").decode() if isinstance(e.output, bytes) else (e.output or "")
    assert "</html>" in dom
    assert "加载中…" not in dom                      # "下一节课 / 最近截止"卡片被脚本填上了
    assert 'class="fc' in dom                          # FullCalendar 渲染出来了
