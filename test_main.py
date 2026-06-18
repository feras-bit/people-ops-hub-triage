"""Offline tests for the triage logic — no Asana calls. Run: python3 test_main.py

Validates the pure logic + the error-isolation guard before any redeploy.
Network functions (_asana, move_to_section, update_task, notify_slack) are
monkeypatched, so this never touches Asana.
"""
import sys
import types
import os
import datetime

# Stub the GCP-only module and a dummy token so `import main` works offline.
_ff = types.ModuleType("functions_framework")
_ff.http = lambda f: f
sys.modules["functions_framework"] = _ff
os.environ.setdefault("ASANA_PAT", "test")
os.environ.setdefault("SEC_JOB_ORG", "JOBORG_SEC")  # so Job&Org routing is testable

import main as m  # noqa: E402


def _task(cat=None, pri=None, status=None, section=None, assignee=None,
          due=None, completed=False, name="t", gid="1", requester=None):
    cfs = []
    if cat is not None:
        cfs.append({"gid": m.F_CATEGORY, "enum_value": {"gid": cat}})
    if pri is not None:
        cfs.append({"gid": m.F_PRIORITY, "enum_value": {"gid": pri}})
    cfs.append({"gid": m.F_STATUS, "enum_value": ({"gid": status} if status else None)})
    cfs.append({"gid": m.F_REQUESTER, "text_value": requester})
    return {"gid": gid, "name": name, "completed": completed,
            "assignee": ({"gid": assignee} if assignee else None), "due_on": due,
            "memberships": [{"project": {"gid": m.PROJECT_GID},
                             "section": {"gid": section}}],
            "custom_fields": cfs}


def test_business_days():
    fri = datetime.date(2026, 6, 12)
    assert m.add_business_days(fri, 0) == fri
    assert m.add_business_days(fri, 2) == datetime.date(2026, 6, 16)   # +2bd -> Tue
    assert m.add_business_days(fri, 5) == datetime.date(2026, 6, 19)   # +5bd -> Fri
    assert m.add_business_days(datetime.date(2026, 6, 11), 1) == datetime.date(2026, 6, 12)


def test_enum_and_section():
    t = _task(cat=m.CAT_IT, pri=m.PRI_HIGH, section=m.SEC_NEW)
    assert m.enum_gid(t, m.F_CATEGORY) == m.CAT_IT
    assert m.enum_gid(t, m.F_STATUS) is None
    assert m.current_section(t) == m.SEC_NEW
    assert m.CATEGORY_TO_SECTION[m.CAT_IT] == m.SEC_IT
    assert m.CAT_OTHER not in m.CATEGORY_TO_SECTION


def test_routes_and_fills_blanks():
    moves, updates = [], []
    m.move_to_section = lambda g, s: moves.append((g, s))
    m.update_task = lambda g, f: updates.append((g, f))
    wed = datetime.date(2026, 6, 17)
    m.triage_task(_task(cat=m.CAT_IT, pri=m.PRI_HIGH, section=m.SEC_NEW), wed)
    assert moves == [("1", m.SEC_IT)], moves
    g, fields = updates[0]
    assert fields["assignee"] == m.OWNER_GID
    assert fields["custom_fields"][m.F_STATUS] == m.ST_NEW
    assert fields["due_on"] == "2026-06-19"


def test_other_is_left_alone():
    moves, updates = [], []
    m.move_to_section = lambda g, s: moves.append((g, s))
    m.update_task = lambda g, f: updates.append((g, f))
    m.triage_task(_task(cat=m.CAT_OTHER, pri=m.PRI_LOW, section=m.SEC_NEW),
                  datetime.date(2026, 6, 17))
    assert moves == [] and updates == []


def test_lifecycle_resolved_and_waiting():
    moves, updates = [], []
    m.move_to_section = lambda g, s: moves.append((g, s))
    m.update_task = lambda g, f: updates.append((g, f))
    m.triage_task(_task(cat=m.CAT_IT, status=m.ST_RESOLVED, section=m.SEC_IT,
                        completed=False), datetime.date(2026, 6, 17))
    assert ("1", m.SEC_RESOLVED) in moves
    assert any(f.get("completed") for _, f in updates)
    moves.clear()
    m.triage_task(_task(cat=m.CAT_IT, status=m.ST_WAITING, section=m.SEC_IT),
                  datetime.date(2026, 6, 17))
    assert moves == [("1", m.SEC_WAITING)]


def test_sweep_isolates_one_bad_ticket():
    m.fetch_open_tasks = lambda: [_task(name="good", gid="1", cat=m.CAT_IT,
                                         pri=m.PRI_HIGH, section=m.SEC_NEW),
                                  _task(name="bad", gid="2", cat=m.CAT_HR,
                                        pri=m.PRI_MED, section=m.SEC_NEW)]
    m.notify_slack = lambda *_: None
    m.move_to_section = lambda g, s: None
    def _flaky(t, today):
        if t["name"] == "bad":
            raise RuntimeError("boom")
        return "good: routed"
    original = m.triage_task
    m.triage_task = _flaky
    try:
        s = m.run_sweep()
    finally:
        m.triage_task = original   # restore so later tests use the real function
    assert s["scanned"] == 2 and s["changed"] == 1 and s["errors"] == 1, s


def test_adds_requester_as_follower():
    followers = []
    m.move_to_section = lambda g, s: None
    m.update_task = lambda g, f: None
    m.notify_slack = lambda *_: None
    m.add_followers = lambda g, fl: followers.append((g, fl))
    m.triage_task(_task(cat=m.CAT_IT, pri=m.PRI_MED, section=m.SEC_NEW,
                        requester="sam@lap.coffee"), datetime.date(2026, 6, 17))
    assert followers == [("1", ["sam@lap.coffee"])], followers


def test_follower_add_failure_is_swallowed():
    m.move_to_section = lambda g, s: None
    m.update_task = lambda g, f: None
    m.notify_slack = lambda *_: None

    def _boom(g, fl):
        raise RuntimeError("not a workspace member")
    m.add_followers = _boom
    # must not raise even though add_followers blows up
    m.triage_task(_task(cat=m.CAT_IT, pri=m.PRI_MED, section=m.SEC_NEW,
                        requester="ext@nope.com"), datetime.date(2026, 6, 17))


def test_urgent_ping_on_route():
    pings = []
    m.notify_slack = lambda msg: pings.append(msg)
    m.move_to_section = lambda g, s: None
    m.update_task = lambda g, f: None
    m.add_followers = lambda g, fl: None   # requester is set below → would be called
    m.triage_task(_task(cat=m.CAT_IT, pri=m.PRI_URGENT, section=m.SEC_NEW,
                        requester="anna@lap.coffee"),
                  datetime.date(2026, 6, 17))
    assert len(pings) == 1 and "URGENT" in pings[0], pings
    assert "anna@lap.coffee" in pings[0], pings   # requester shown in the ping
    # already-routed urgent ticket (not in New) must NOT re-ping
    pings.clear()
    m.triage_task(_task(cat=m.CAT_IT, pri=m.PRI_URGENT, section=m.SEC_IT,
                        assignee=m.OWNER_GID, due="2026-06-17", status=m.ST_NEW),
                  datetime.date(2026, 6, 17))
    assert pings == [], pings


def test_digest_lists_urgent_and_overdue():
    msgs = []
    m.notify_slack = lambda msg: msgs.append(msg)
    m.fetch_open_tasks = lambda: [
        _task(name="urgent one", pri=m.PRI_URGENT, section=m.SEC_IT, requester="bob@lap.coffee"),
        _task(name="late one", pri=m.PRI_MED, section=m.SEC_HR, due="2026-06-01"),
        _task(name="resolved", status=m.ST_RESOLVED, section=m.SEC_RESOLVED),
    ]
    r = m.digest_run()
    assert r["urgent_open"] == 1 and r["overdue"] == 1, r
    assert "urgent one" in msgs[0] and "late one" in msgs[0]
    assert "bob@lap.coffee" in msgs[0], msgs   # requester shown in digest


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"✅ {len(fns)} tests passed")
