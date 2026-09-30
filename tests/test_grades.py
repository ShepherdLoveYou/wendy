"""成绩与完成情况：总成绩可能不公开；得分率由已批改作业算出；缺交、晚交计数；读不到时不影响页面。"""
from __future__ import annotations

from conftest import FakeCanvas, pt

NOW = pt("2026-10-20 12:00")


def sub(score, points, missing=False, late=False, omit=False):
    return {"score": score, "missing": missing, "late": late,
            "assignment": {"points_possible": points, "omit_from_final_grade": omit}}


def test_grades_card_shows_rate_missing_late_and_hidden_totals(build):
    canvas = FakeCanvas(submissions={
        1001: [sub(9, 10), sub(18, 20, late=True), sub(None, 10, missing=True), sub(5, 5, omit=True)],
        1002: [sub(None, 10)],
    })
    page = build(NOW, canvas)
    card = page.html.split('id="grades"')[1].split("</section>")[0]
    assert "91.5%" in card                                   # TEST 101 公开了总成绩
    assert "90%（27/30，2 项）" in card                        # 不计 omit 的作业；未批改的不算
    assert "缺交 1" in card and "晚交 1" in card
    assert "老师未公开" in card and "还没有批改的作业" in card   # DEMO 002A
    assert "Orientation" not in card                         # 非正式课不显示


def test_grades_failure_is_not_fatal(build):
    page = build(NOW, FakeCanvas(grades_fail=True))
    assert 'id="grades"' not in page.html and 'id="todo"' in page.html


def test_nav_has_home_link(build):
    page = build(NOW)
    assert 'href="../"' in page.html and "大学规划" in page.html
