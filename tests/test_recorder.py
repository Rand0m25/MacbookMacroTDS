"""Recorder coalescer rules (plan S5)."""

from tds_macro.recorder import EventCoalescer
from tds_macro.geometry import Point


def test_double_click_merge_and_pre_move_dropped():
    c = EventCoalescer(dead_zone=0.01, double_click_ms=250)
    c.on_move(Point(0.5, 0.5), 100)             # free move before click -> dropped
    c.on_button(Point(0.5, 0.5), "left", True, 150)
    c.on_button(Point(0.5, 0.5), "left", False, 170)
    c.on_button(Point(0.5, 0.5), "left", True, 200)
    c.on_button(Point(0.5, 0.5), "left", False, 210)
    evs = c.finish()
    assert [e.type for e in evs] == ["click"]
    assert evs[0].clicks == 2


def test_two_far_clicks_not_merged():
    c = EventCoalescer(dead_zone=0.01, double_click_ms=250)
    c.on_button(Point(0.2, 0.2), "left", True, 0)
    c.on_button(Point(0.2, 0.2), "left", False, 10)
    c.on_button(Point(0.8, 0.8), "left", True, 50)
    c.on_button(Point(0.8, 0.8), "left", False, 60)
    evs = c.finish()
    assert [e.type for e in evs] == ["click", "click"]
    assert all(e.clicks == 1 for e in evs)


def test_drag_detected_by_deadzone():
    c = EventCoalescer(dead_zone=0.01)
    c.on_button(Point(0.2, 0.5), "left", True, 0)
    c.on_move(Point(0.6, 0.5), 50)
    c.on_button(Point(0.6, 0.5), "left", False, 100)
    evs = c.finish()
    assert evs[0].type == "drag"
    assert evs[0].frm.x == 0.2 and evs[0].to.x == 0.6
    assert evs[0].duration_ms == 100


def test_free_move_flushed_before_key():
    c = EventCoalescer(dead_zone=0.01)
    c.on_move(Point(0.3, 0.3), 10)
    c.on_key("e", True, 20)
    c.on_key("e", False, 25)
    assert [e.type for e in c.finish()] == ["mouse_move", "key_press", "key_release"]


def test_tiny_move_while_held_is_click_not_drag():
    c = EventCoalescer(dead_zone=0.02)
    c.on_button(Point(0.5, 0.5), "left", True, 0)
    c.on_move(Point(0.505, 0.5), 5)  # within dead zone
    c.on_button(Point(0.505, 0.5), "left", False, 10)
    evs = c.finish()
    assert [e.type for e in evs] == ["click"]


def test_events_have_monotonic_ids_and_sorted_time():
    c = EventCoalescer(dead_zone=0.01)
    c.on_key("a", True, 100)
    c.on_key("a", False, 110)
    c.on_button(Point(0.1, 0.1), "left", True, 50)
    c.on_button(Point(0.1, 0.1), "left", False, 60)
    evs = c.finish()
    assert [e.t_ms for e in evs] == sorted(e.t_ms for e in evs)
    assert len({e.id for e in evs}) == len(evs)
