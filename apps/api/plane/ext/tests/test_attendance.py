# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""Attendance: working days, late rule, clock in/out, day close, team view and privacy."""

import datetime as dt
import zoneinfo

import pytest
from freezegun import freeze_time

from plane.db.models import Intake, IntakeIssue, Issue, IssueLabel, Label, ModuleIssue, Project, ProjectMember, State
import plane.ext.urls_root  # noqa: F401  (load the URLconf before freeze_time: freezegun breaks its class definitions)
from plane.ext import attendance, services
from plane.ext.models import (
    ExtAttendanceDay,
    ExtAttendanceHoliday,
    ExtAttendancePerson,
    ExtAttendanceSchedule,
    ExtAttendanceSettings,
    ExtPropertyValue,
)

pytestmark = pytest.mark.django_db
D = dt.date
DHAKA = zoneinfo.ZoneInfo("Asia/Dhaka")


def at(y, m, d, hh, mm, ss=0):
    """An instant given as Dhaka wall-clock time."""
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=DHAKA)


def utc(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def staff_file(ws, people, who, ident, hr_admin=None):
    p = Project.objects.create(name=f"Staff file {ident}", identifier=ident, workspace=ws, network=0)
    ProjectMember.objects.create(project=p, workspace=ws, member=people["admin"], role=20)
    ProjectMember.objects.create(project=p, workspace=ws, member=people[who], role=5)
    if hr_admin:
        ProjectMember.objects.create(project=p, workspace=ws, member=people[hr_admin], role=20)
    for seq, (name, group) in enumerate((("To do", "unstarted"), ("In progress", "started"), ("Done", "completed"))):
        State.objects.create(
            name=name, group=group, project=p, workspace=ws, sequence=seq, default=group == "unstarted"
        )
    return p


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(attendance, "_activity", lambda *a, **k: None)  # Celery side effects


@pytest.fixture
def hr(ws, people):
    p = Project.objects.create(name="HR", identifier="HR", workspace=ws, network=0)
    for key, role in (("admin", 20), ("member", 20), ("guest", 5)):
        ProjectMember.objects.create(project=p, workspace=ws, member=people[key], role=role)
    State.objects.create(name="Triage", group="triage", project=p, workspace=ws)
    State.objects.create(name="Done", group="completed", project=p, workspace=ws)
    Intake.objects.create(name="HR Intake", project=p, workspace=ws, is_default=True)
    group = Label.objects.create(name="Leave type", project=p, workspace=ws)
    for name in ("Vacation", "Late arrival", "Standard time off"):
        Label.objects.create(name=name, parent=group, project=p, workspace=ws)
    return p


def time_off(hr, user, start, end, status, leave="Vacation"):
    issue = Issue.objects.create(
        name=f"{leave}: request",
        project=hr,
        workspace=hr.workspace,
        start_date=start,
        target_date=end,
        state=State.all_objects.get(project=hr, name="Done" if status == 1 else "Triage"),
    )
    Issue.all_objects.filter(id=issue.id).update(created_by=user)
    IssueLabel.objects.create(
        issue=issue, label=Label.objects.get(project=hr, name=leave), project=hr, workspace=hr.workspace
    )
    IntakeIssue.objects.create(
        intake=Intake.objects.get(project=hr), issue=issue, status=status, project=hr, workspace=hr.workspace
    )
    return issue


@pytest.fixture
def setup(ws, people, hr):
    """Guest clocks into SFGUEST (HR = 'member', an Admin of that file); 'member' clocks into SFMEMBER."""
    services.set_flags(ws.id, {"ext.attendance": True, "ext.access_hardening": True})
    sf_guest = staff_file(ws, people, "guest", "SFGUEST", hr_admin="member")
    sf_member = staff_file(ws, people, "member", "SFMEMBER")
    ExtAttendanceSettings.objects.create(
        workspace=ws, recorder=people["admin"], time_off_project=hr, mark_absent_from=D(2026, 11, 1)
    )
    ExtAttendanceSchedule.objects.create(workspace=ws, effective_from=D(2026, 1, 1), shift_start=dt.time(9, 0))
    ExtAttendanceSchedule.objects.create(workspace=ws, effective_from=D(2026, 11, 1), shift_start=dt.time(7, 30))
    ExtAttendanceHoliday.objects.create(workspace=ws, date=D(2026, 12, 16), name="Victory Day")
    g = ExtAttendancePerson.objects.create(workspace=ws, member=people["guest"], project=sf_guest)
    m = ExtAttendancePerson.objects.create(workspace=ws, member=people["member"], project=sf_member)
    ExtAttendancePerson.objects.filter(id__in=[g.id, m.id]).update(created_at=at(2026, 11, 1, 0, 0))
    return {
        "guest": ExtAttendancePerson.objects.get(id=g.id),
        "member": ExtAttendancePerson.objects.get(id=m.id),
        "sf_guest": sf_guest,
        "sf_member": sf_member,
    }


def status_of(issue):
    return attendance._status_of(issue)


# -- rules ---------------------------------------------------------------------------------------
def test_working_days_sunday_to_thursday_plus_2nd_and_3rd_saturday(ws, setup):
    s = attendance.schedule_for(ws.id, None, D(2026, 12, 1))
    kinds = {d: attendance.day_kind(ws.id, s, d)[0] for d in (D(2026, 12, n) for n in range(1, 32))}
    assert kinds[D(2026, 12, 4)] == "off"  # Friday
    assert kinds[D(2026, 12, 5)] == "off"  # 1st Saturday
    assert kinds[D(2026, 12, 12)] == "work"  # 2nd Saturday
    assert kinds[D(2026, 12, 19)] == "work"  # 3rd Saturday
    assert kinds[D(2026, 12, 26)] == "off"  # 4th Saturday
    assert kinds[D(2026, 12, 6)] == "work"  # Sunday
    assert attendance.day_kind(ws.id, s, D(2026, 12, 16)) == ("holiday", "Victory Day")
    assert attendance.title(D(2026, 12, 12), "work", s) == "Attendance: Sat, Dec 12, 2026 · 2nd Saturday"


def test_late_is_more_than_15_minutes_after_shift_start(ws, setup):
    s = attendance.schedule_for(ws.id, None, D(2026, 11, 25))
    assert s.shift_start == dt.time(7, 30)
    assert attendance.arrival_status(s, at(2026, 11, 25, 7, 45), DHAKA, False) == "present"
    assert attendance.arrival_status(s, at(2026, 11, 25, 7, 45, 1), DHAKA, False) == "late"
    assert attendance.arrival_status(s, at(2026, 11, 25, 8, 30), DHAKA, True) == "excused_late"


def test_schedule_follows_effective_dates_and_personal_rows_win(ws, people, setup):
    assert attendance.schedule_for(ws.id, people["guest"].id, D(2026, 10, 31)).shift_start == dt.time(9, 0)
    assert attendance.schedule_for(ws.id, people["guest"].id, D(2026, 11, 1)).shift_start == dt.time(7, 30)
    ExtAttendanceSchedule.objects.create(
        workspace=ws, member=people["guest"], effective_from=D(2026, 11, 10), shift_start=dt.time(8, 0)
    )
    assert attendance.schedule_for(ws.id, people["guest"].id, D(2026, 11, 12)).shift_start == dt.time(8, 0)
    assert attendance.schedule_for(ws.id, people["member"].id, D(2026, 11, 12)).shift_start == dt.time(7, 30)
    assert attendance.schedule_for(ws.id, people["guest"].id, D(2025, 12, 31)) is None


# -- clocking ------------------------------------------------------------------------------------
def test_clock_in_late_then_out_makes_one_locked_item(ws, people, setup, client_for, base):
    c = client_for(people["guest"])
    with freeze_time(utc(at(2026, 11, 25, 7, 50))):
        r = c.post(f"{base}/attendance/clock-in/", {"note": "Traffic on the bridge"}, format="json")
    assert r.status_code == 200, r.content
    day = r.json()["today"]["day"]
    assert day["status"] == "late" and day["clock_in"].startswith("2026-11-25T07:50")
    issue = Issue.objects.get(id=day["issue_id"])
    assert issue.project_id == setup["sf_guest"].id and issue.created_by_id == people["admin"].id
    assert issue.state.group == "started" and issue.start_date == issue.target_date == D(2026, 11, 25)
    assert issue.name == "Attendance: Wed, Nov 25, 2026"
    assert ModuleIssue.objects.filter(issue=issue, module__name="Attendance").exists()
    assert issue.issue_comments.filter(actor=people["guest"]).count() == 1

    with freeze_time(utc(at(2026, 11, 25, 7, 55))):
        again = c.post(f"{base}/attendance/clock-in/", {}, format="json")
    assert again.status_code == 400 and "already clocked in" in again.json()["detail"]

    # staff cannot rewrite their own clock times
    clock_in = ExtPropertyValue.objects.get(issue=issue, definition__key="clock_in").definition
    edit = c.patch(
        f"{base}/projects/{issue.project_id}/work-items/{issue.id}/values/{clock_in.id}/",
        {"value": "2026-11-25T01:00:00Z"},
        format="json",
    )
    assert edit.status_code == 403

    with freeze_time(utc(at(2026, 11, 25, 17, 20))):
        out = c.post(f"{base}/attendance/clock-out/", {}, format="json")
    assert out.status_code == 200
    day = out.json()["today"]["day"]
    assert day["hours"] == 9.5
    issue.refresh_from_db()
    assert issue.state.group == "completed"
    assert ExtAttendanceDay.objects.filter(person=setup["guest"]).count() == 1

    with freeze_time(utc(at(2026, 11, 25, 17, 30))):
        twice = c.post(f"{base}/attendance/clock-out/", {}, format="json")
    assert twice.status_code == 400


def test_clock_out_needs_a_clock_in_and_closes_last_nights_shift(ws, people, setup):
    with pytest.raises(Exception, match="Clock in first"):
        attendance.clock_out(ws.id, people["guest"], now=at(2026, 11, 25, 17, 0))
    attendance.clock_in(ws.id, people["guest"], now=at(2026, 11, 25, 22, 0))
    attendance.clock_out(ws.id, people["guest"], now=at(2026, 11, 26, 2, 0))
    row = ExtAttendanceDay.objects.get(person=setup["guest"])
    assert row.date == D(2026, 11, 25)
    assert attendance.day_json(row, DHAKA)["hours"] == 4.0


def test_late_arrival_covered_by_accepted_time_off_is_excused(ws, people, setup, hr):
    time_off(hr, people["guest"], D(2026, 11, 25), D(2026, 11, 25), 1, leave="Late arrival")
    day = attendance.clock_in(ws.id, people["guest"], now=at(2026, 11, 25, 9, 0))
    assert day["status"] == "excused_late"
    pending_only = attendance.clock_in(ws.id, people["member"], now=at(2026, 11, 25, 9, 0))
    assert pending_only["status"] == "late"


def test_corrected_clock_in_relabels_the_day(ws, people, setup, django_capture_on_commit_callbacks):
    day = attendance.clock_in(ws.id, people["guest"], now=at(2026, 11, 25, 8, 10))
    issue = Issue.objects.get(id=day["issue_id"])
    assert status_of(issue) == "late"
    definition = ExtPropertyValue.objects.get(issue=issue, definition__key="clock_in").definition
    with django_capture_on_commit_callbacks(execute=True):
        services.write_value(issue, definition, at(2026, 11, 25, 7, 32).isoformat(), people["admin"])
    assert status_of(issue) == "present"


def test_not_set_up_and_flag_off(ws, people, setup, client_for, base):
    r = client_for(people["admin"]).post(f"{base}/attendance/clock-in/", {}, format="json")
    assert r.status_code == 404
    services.set_flags(ws.id, {"ext.attendance": False})
    assert client_for(people["guest"]).get(f"{base}/attendance/me/").status_code == 404


# -- end of day ----------------------------------------------------------------------------------
def test_day_close_marks_absent_holiday_and_leave_once(ws, people, setup, hr):
    time_off(hr, people["member"], D(2026, 12, 14), D(2026, 12, 15), 1, leave="Vacation")
    time_off(hr, people["guest"], D(2026, 12, 15), D(2026, 12, 15), -2, leave="Standard time off")
    for day in (D(2026, 12, 14), D(2026, 12, 15), D(2026, 12, 16), D(2026, 12, 18)):  # Mon, Tue, Wed (holiday), Fri
        for who in ("guest", "member"):
            attendance.close_day(setup[who], attendance.config(ws.id), day)

    def labels(who):
        rows = ExtAttendanceDay.objects.filter(person=setup[who]).order_by("date")
        return {r.date.day: status_of(r.issue) for r in rows}

    assert labels("guest") == {14: "absent", 15: "absent", 16: "holiday"}
    assert labels("member") == {14: "vacation", 15: "vacation", 16: "holiday"}
    pending = ExtAttendanceDay.objects.get(person=setup["guest"], date=D(2026, 12, 15)).issue
    assert "still waiting for HR" in pending.description_html
    assert pending.state.group == "completed" and pending.created_by_id == people["admin"].id

    with freeze_time(utc(at(2026, 12, 17, 0, 10))):
        made = attendance.run_due()
    assert made == 0  # 14-16 already closed: idempotent
    with freeze_time(utc(at(2026, 12, 20, 0, 10))):
        assert attendance.run_due() == 4  # Thu 17 and Sat 19 (3rd Saturday) for both; Fri 18 is off


def test_day_close_respects_start_date_optional_people_and_flag(ws, people, setup):
    cfg = attendance.config(ws.id)
    assert attendance.close_day(setup["guest"], cfg, D(2026, 10, 29)) is None  # before mark_absent_from
    setup["member"].mark_absent = False  # clocking in is optional for them
    setup["member"].save()
    assert attendance.close_day(setup["member"], cfg, D(2026, 11, 25)) is None
    assert attendance.close_day(setup["guest"], cfg, D(2026, 11, 25)) == "absent"
    services.set_flags(ws.id, {"ext.attendance": False})
    with freeze_time(utc(at(2026, 11, 26, 0, 10))):
        assert attendance.run_due() == 0


# -- views and privacy ---------------------------------------------------------------------------
def test_me_shows_only_your_own_days(ws, people, setup, client_for, base):
    attendance.clock_in(ws.id, people["guest"], now=at(2026, 11, 24, 7, 20))
    attendance.clock_in(ws.id, people["member"], now=at(2026, 11, 24, 7, 25))
    with freeze_time(utc(at(2026, 11, 24, 12, 0))):
        mine = client_for(people["guest"]).get(f"{base}/attendance/me/").json()
    assert [d["identifier"].split("-")[0] for d in mine["days"]] == ["SFGUEST"]
    assert mine["summary"]["counts"]["present"] == 1
    assert mine["today"]["shift_start"] == "07:30" and mine["today"]["late_after"] == "07:45"
    assert (
        client_for(people["guest"])
        .get(f"/api/workspaces/{ws.slug}/projects/{setup['sf_member'].id}/issues/")
        .status_code
        == 403
    )


def test_team_view_is_hr_only(ws, people, setup, client_for, base):
    attendance.clock_in(ws.id, people["guest"], now=at(2026, 11, 24, 7, 20))
    with freeze_time(utc(at(2026, 11, 24, 12, 0))):
        as_guest = client_for(people["guest"]).get(f"{base}/attendance/team/").json()
        as_hr = client_for(people["member"]).get(f"{base}/attendance/team/").json()
        as_admin = client_for(people["admin"]).get(f"{base}/attendance/team/").json()
    assert as_guest["people"] == []
    assert [p["project_identifier"] for p in as_hr["people"]] == ["SFGUEST"]  # not their own file
    assert sorted(p["project_identifier"] for p in as_admin["people"]) == ["SFGUEST", "SFMEMBER"]
    assert as_admin["people"][0]["summary"]["working_days"] >= 1


def test_settings_are_admin_only_and_validated(ws, people, setup, client_for, base):
    url = f"{base}/attendance/settings/"
    assert client_for(people["guest"]).get(url).status_code == 403
    admin = client_for(people["admin"])
    current = admin.get(url).json()
    assert current["access"] == "admin" and all(p["editable"] for p in current["people"])
    assert [s["shift_start"] for s in current["schedules"]] == ["09:00", "07:30"]
    payload = {
        **current,
        "schedules": current["schedules"]
        + [{"effective_from": "2027-03-01", "shift_start": "08:00", "grace_minutes": 10}],
        "holidays": current["holidays"] + [{"date": "2027-02-21", "name": "Language Martyrs' Day"}],
    }
    r = admin.put(url, payload, format="json")
    assert r.status_code == 200, r.content
    assert attendance.schedule_for(ws.id, None, D(2027, 3, 2)).grace_minutes == 10
    assert ExtAttendanceHoliday.objects.filter(workspace=ws).count() == 2

    bad = admin.put(url, {**payload, "recorder_id": str(people["member"].id)}, format="json")
    assert bad.status_code == 400
    wrong_file = {
        **payload,
        "people": [{"member_id": str(people["guest"].id), "project_id": str(setup["sf_member"].id)}],
    }
    assert admin.put(url, wrong_file, format="json").status_code == 400
    optional = [{**p, "mark_absent": p["member_id"] != str(people["member"].id)} for p in payload["people"]]
    assert admin.put(url, {**payload, "people": optional}, format="json").status_code == 200
    assert ExtAttendancePerson.objects.get(workspace=ws, member=people["member"]).mark_absent is False
    dropped = admin.put(url, {**payload, "people": payload["people"][:1]}, format="json")
    assert dropped.status_code == 200
    assert ExtAttendancePerson.objects.filter(workspace=ws, is_active=True).count() == 1


def test_hr_manager_edits_rota_and_managed_people_only(ws, people, setup, client_for, base):
    """'member' is an Admin of HR (the time-off project) and of SFGUEST, but not a workspace Admin."""
    url = f"{base}/attendance/settings/"
    hr_user = client_for(people["member"])
    current = hr_user.get(url).json()
    assert current["access"] == "hr"
    assert sorted(p["identifier"] for p in current["directory"]["projects"]) == ["HR", "SFGUEST"]
    editable = {p["member_id"]: p["editable"] for p in current["people"]}
    assert editable == {str(people["guest"].id): True, str(people["member"].id): False}

    payload = {
        **current,
        "timezone": "UTC",
        "recorder_id": str(people["member"].id),
        "mark_absent": False,
        "holidays": current["holidays"] + [{"date": "2027-02-21", "name": "Language Martyrs' Day"}],
        "schedules": current["schedules"] + [{"effective_from": "2027-03-01", "shift_start": "08:00"}],
    }
    r = hr_user.put(url, payload, format="json")
    assert r.status_code == 200, r.content
    cfg = ExtAttendanceSettings.objects.get(workspace=ws)
    assert (cfg.timezone, cfg.recorder_id, cfg.mark_absent) == ("Asia/Dhaka", people["admin"].id, True)
    assert ExtAttendanceHoliday.objects.filter(workspace=ws).count() == 2
    assert attendance.schedule_for(ws.id, None, D(2027, 3, 2)).shift_start == dt.time(8, 0)

    def people_with(**changes):
        rows = [{**p, **changes.get(p["member_id"], {})} for p in current["people"]]
        return {**current, "people": [p for p in rows if not p.get("drop")]}

    own = people_with(**{str(people["member"].id): {"mark_absent": False}})
    assert hr_user.put(url, own, format="json").status_code == 400
    assert hr_user.put(url, people_with(**{str(people["member"].id): {"drop": True}}), format="json").status_code == 400
    assert ExtAttendancePerson.objects.get(workspace=ws, member=people["member"]).is_active
    managed = people_with(**{str(people["guest"].id): {"mark_absent": False}})
    assert hr_user.put(url, managed, format="json").status_code == 200
    assert ExtAttendancePerson.objects.get(workspace=ws, member=people["guest"]).mark_absent is False

    ProjectMember.objects.filter(project__identifier="HR", member=people["member"]).update(role=15)
    assert hr_user.get(url).status_code == 403
    assert client_for(people["outsider"]).get(url).status_code == 403
