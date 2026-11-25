# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""Attendance: Clock in / Clock out (flag ext.attendance).

Each day someone works (or is absent) is one work item in their own private Staff file project, in the
"Attendance" module, dated that day, with one label from the "Attendance" group (Present, Late, Excused
late, Absent, Holiday, Time off, Vacation, Sick leave, Unplanned leave) and the ext properties Clock in /
Clock out. The work item is the record: HR corrects a day by editing it, and a corrected Clock in
re-labels Present/Late. The items are created by the workspace's recorder (an Admin), so staff can read
and comment on their own days but cannot change them.

Rules (ExtAttendanceSchedule, from `effective_from` until the next row; a person's own row wins):
- Late = clocked in more than `grace_minutes` after `shift_start`, in the workspace timezone.
  Excused late = late on a day an accepted HR time-off request covers (e.g. "Late arrival").
- Working days = `workdays` (ISO weekday numbers) plus the `saturdays`-th Saturdays of the month,
  minus holidays (ExtAttendanceHoliday).
- After a working day ends, anyone with no item for it gets one: Holiday, the accepted time off's
  leave type, or Absent (noting any request still pending). Never before `mark_absent_from`, and never
  for someone whose clocking in is optional (ExtAttendancePerson.mark_absent off).
Clock times come from the server clock, never from the browser.
"""

import datetime as dt
import html
import json
import zoneinfo

from crum import impersonate
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from plane.db.models import (
    IntakeIssue,
    Issue,
    IssueComment,
    IssueLabel,
    Label,
    Module,
    ModuleIssue,
    ProjectMember,
    State,
    WorkspaceMember,
)

from . import services
from .models import (
    ExtAttendanceDay,
    ExtAttendanceHoliday,
    ExtAttendancePerson,
    ExtAttendanceSchedule,
    ExtAttendanceSettings,
    ExtProjectBinding,
    ExtPropertyDefinition,
    ExtPropertyValue,
)

FLAG = "ext.attendance"
GROUP = "Attendance"
MODULE = "Attendance"
CLOCK_IN, CLOCK_OUT = "clock_in", "clock_out"
STATUSES = {
    "present": ("Present", "#2E9E5B"),
    "late": ("Late", "#E8A33D"),
    "excused_late": ("Excused late", "#8CB369"),
    "absent": ("Absent", "#D33D44"),
    "holiday": ("Holiday", "#3F76FF"),
    "time_off": ("Time off", "#7C5CFC"),
    "vacation": ("Vacation", "#7C5CFC"),
    "sick_leave": ("Sick leave", "#9B59B6"),
    "unplanned_leave": ("Unplanned leave", "#B5651D"),
}
BY_NAME = {name: key for key, (name, _) in STATUSES.items()}
ARRIVAL = {"present", "late", "excused_late"}
LEAVE_LABELS = {"Vacation": "vacation", "Sick leave": "sick_leave"}  # HR "Leave type" -> attendance status
INTAKE_ACCEPTED, INTAKE_PENDING = 1, -2
LONGEST_SHIFT = dt.timedelta(hours=16)  # a clock-out after midnight still closes yesterday's shift
CATCH_UP_DAYS = 3  # the tick closes at most this many past days (missed ticks, downtime)
ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}


# -- rules --------------------------------------------------------------------------------------
def config(workspace_id):
    return ExtAttendanceSettings.objects.filter(workspace_id=workspace_id).first()


def zone(cfg):
    return zoneinfo.ZoneInfo(cfg.timezone if cfg else "Asia/Dhaka")


def schedule_for(workspace_id, member_id, day):
    rows = ExtAttendanceSchedule.objects.filter(workspace_id=workspace_id, effective_from__lte=day)
    own = rows.filter(member_id=member_id).order_by("-effective_from").first()
    return own or rows.filter(member__isnull=True).order_by("-effective_from").first()


def nth_weekday_of_month(day):
    return (day.day - 1) // 7 + 1


def day_kind(workspace_id, schedule, day):
    """('work' | 'off' | 'holiday', holiday name or '')."""
    holiday = ExtAttendanceHoliday.objects.filter(workspace_id=workspace_id, date=day).first()
    if holiday:
        return "holiday", holiday.name
    if schedule is None:
        return "work", ""
    weekday = day.isoweekday()
    if weekday == 6 and weekday not in schedule.workdays:
        return ("work" if nth_weekday_of_month(day) in schedule.saturdays else "off"), ""
    return ("work" if weekday in schedule.workdays else "off"), ""


def late_after(schedule, day, tz):
    start = dt.datetime.combine(day, schedule.shift_start, tzinfo=tz)
    return start + dt.timedelta(minutes=schedule.grace_minutes)


def arrival_status(schedule, clock_in, tz, excused):
    """present | late | excused_late for a clock-in time (aware datetime)."""
    if schedule is None:
        return "present"
    local = clock_in.astimezone(tz)
    if local <= late_after(schedule, local.date(), tz):
        return "present"
    return "excused_late" if excused else "late"


def time_off_on(cfg, member_id, day):
    """(accepted request or None, pending request or None) from the HR time-off Intake covering `day`."""
    if not cfg or not cfg.time_off_project_id:
        return None, None
    rows = (
        IntakeIssue.objects.filter(
            project_id=cfg.time_off_project_id,
            issue__created_by_id=member_id,
            issue__start_date__lte=day,
            issue__deleted_at__isnull=True,
            status__in=[INTAKE_ACCEPTED, INTAKE_PENDING],
        )
        .select_related("issue")
        .order_by("issue__start_date")
    )
    accepted = pending = None
    for r in rows:
        if (r.issue.target_date or r.issue.start_date) < day:
            continue
        if r.status == INTAKE_ACCEPTED and accepted is None:
            accepted = r.issue
        elif r.status == INTAKE_PENDING and pending is None:
            pending = r.issue
    return accepted, pending


def leave_status(request):
    names = set(
        IssueLabel.objects.filter(issue=request, label__parent__name="Leave type").values_list("label__name", flat=True)
    )
    for name, status in LEAVE_LABELS.items():
        if name in names:
            return status
    return "time_off"


# -- the Staff file pieces ----------------------------------------------------------------------
def ensure_project(person, actor):
    """Label group, labels, module and the two Clock properties in the person's Staff file (idempotent)."""
    p = person.project
    common = {"project_id": p.id, "workspace_id": p.workspace_id}
    with impersonate(actor):
        group = Label.objects.filter(project_id=p.id, name=GROUP, parent__isnull=True).first()
        if group is None:
            group = Label.objects.create(name=GROUP, color="#2E9E5B", sort_order=90000, **common)
        labels = {}
        for k, (key, (name, color)) in enumerate(STATUSES.items()):
            label = Label.objects.filter(project_id=p.id, name=name).first()
            if label is None:
                label = Label.objects.create(name=name, color=color, parent=group, sort_order=90001 + k, **common)
            labels[key] = label
        module = Module.objects.filter(project_id=p.id, name=MODULE, archived_at__isnull=True).first()
        if module is None:
            module = Module.objects.create(name=MODULE, sort_order=90000, **common)
        defs = {}
        for k, (key, name) in enumerate(((CLOCK_IN, "Clock in"), (CLOCK_OUT, "Clock out"))):
            d = ExtPropertyDefinition.objects.filter(workspace_id=p.workspace_id, key=key).first()
            if d is None:
                d = ExtPropertyDefinition.objects.create(
                    workspace_id=p.workspace_id, key=key, name=name, type="datetime", sort_order=90000 + k
                )
            ExtProjectBinding.objects.get_or_create(
                project_id=p.id, definition=d, deleted_at__isnull=True, defaults={"sort_order": 90000 + k}
            )
            defs[key] = d
    states = State.objects.filter(project_id=p.id).exclude(group="triage").order_by("sequence")
    started = states.filter(group="started").first() or states.filter(default=True).first() or states.first()
    done = states.filter(group="completed").first() or started
    return {"labels": labels, "module": module, "defs": defs, "started": started, "done": done}


def title(day, kind, schedule):
    text = f"Attendance: {day:%a, %b} {day.day}, {day.year}"
    if day.isoweekday() == 6 and kind == "work" and schedule and 6 not in schedule.workdays:
        text += f" · {ORDINAL[nth_weekday_of_month(day)]} Saturday"
    if kind == "off":
        text += " · day off"
    return text


def _link(request):
    ident = request.project.identifier
    return f"{ident}-{request.sequence_id} {html.escape(request.name)}"


def _activity(issue, actor, kind, data, previous=None):
    from plane.bgtasks.issue_activities_task import issue_activity

    issue_activity.delay(
        type=kind,
        requested_data=json.dumps(data, cls=DjangoJSONEncoder),
        actor_id=str(actor.id) if actor else None,
        issue_id=str(issue.id),
        project_id=str(issue.project_id),
        current_instance=json.dumps(previous, cls=DjangoJSONEncoder) if previous is not None else None,
        epoch=int(timezone.now().timestamp()),
        notification=False,
        origin=None,
    )


def _create_day(person, cfg, day, status, state, description_html, source):
    """The day's work item + its index row. Returns the ExtAttendanceDay (existing one on a race)."""
    from plane.app.serializers import IssueCreateSerializer

    actor = cfg.recorder
    parts = ensure_project(person, actor)
    schedule = schedule_for(person.workspace_id, person.member_id, day)
    kind, _ = day_kind(person.workspace_id, schedule, day)
    try:
        with transaction.atomic():
            with impersonate(actor):
                s = IssueCreateSerializer(
                    data={
                        "name": title(day, kind, schedule),
                        "state_id": str(parts[state].id),
                        "priority": "none",
                        "start_date": day.isoformat(),
                        "target_date": day.isoformat(),
                        "description_html": description_html or "<p></p>",
                        "label_ids": [str(parts["labels"][status].id)],
                    },
                    context={
                        "project_id": person.project_id,
                        "workspace_id": person.workspace_id,
                        "default_assignee_id": None,
                    },
                )
                s.is_valid(raise_exception=True)
                issue = s.save()
                Issue.all_objects.filter(id=issue.id).update(
                    external_source="plane_ext.attendance", external_id=f"{person.member_id}:{day.isoformat()}"
                )
                ModuleIssue.objects.create(
                    module=parts["module"], issue=issue, project_id=person.project_id, workspace_id=person.workspace_id
                )
                row = ExtAttendanceDay.objects.create(person=person, date=day, issue=issue, source=source)
    except IntegrityError:
        return ExtAttendanceDay.objects.get(person=person, date=day, deleted_at__isnull=True)
    _activity(issue, actor, "issue.activity.created", {"name": issue.name, "source": "plane_ext.attendance"})
    return row


def _live_day(person, day):
    row = ExtAttendanceDay.objects.filter(person=person, date=day).select_related("issue").first()
    if row is None:
        return None
    if Issue.objects.filter(id=row.issue_id).exists():
        return row
    row.delete()  # its work item was deleted: the day is free again
    return None


def _value(issue, key):
    v = (
        ExtPropertyValue.objects.filter(issue=issue, definition__key=key)
        .values_list("value_datetime", flat=True)
        .first()
    )
    return v


def _status_of(issue):
    names = IssueLabel.objects.filter(issue=issue, label__name__in=BY_NAME).values_list("label__name", flat=True)
    return next((BY_NAME[n] for n in names if n in BY_NAME), None)


def set_status(issue, status, actor):
    """Swap the day's Attendance label (other labels stay)."""
    project_id = issue.project_id
    wanted = Label.objects.filter(project_id=project_id, name=STATUSES[status][0]).first()
    if wanted is None:
        return
    IssueLabel.objects.filter(issue=issue, label__name__in=BY_NAME).exclude(label=wanted).delete()
    if not IssueLabel.objects.filter(issue=issue, label=wanted).exists():
        with impersonate(actor):
            IssueLabel.objects.create(issue=issue, label=wanted, project_id=project_id, workspace_id=issue.workspace_id)


def _comment(issue, user, note):
    note = (note or "").strip()[:2000]
    if not note:
        return
    with impersonate(user):
        IssueComment.objects.create(
            issue=issue,
            project_id=issue.project_id,
            workspace_id=issue.workspace_id,
            actor=user,
            comment_html=f"<p>{html.escape(note)}</p>",
            comment_stripped=note,
        )


# -- clocking -----------------------------------------------------------------------------------
def person_for(workspace_id, user):
    person = (
        ExtAttendancePerson.objects.filter(workspace_id=workspace_id, member=user, is_active=True)
        .select_related("project")
        .first()
    )
    if person is None or person.project.archived_at is not None:
        raise NotFound("Attendance is not set up for you. Ask HR to add you.")
    return person


def _ready(workspace_id):
    cfg = config(workspace_id)
    if cfg is None or cfg.recorder_id is None:
        raise NotFound("Attendance is not set up for this workspace.")
    return cfg


def clock_in(workspace_id, user, note=None, now=None):
    cfg = _ready(workspace_id)
    person = person_for(workspace_id, user)
    tz = zone(cfg)
    now = now or timezone.now()
    day = now.astimezone(tz).date()
    with transaction.atomic():
        row = _live_day(person, day)
        if row is not None and _value(row.issue, CLOCK_IN):
            at = _value(row.issue, CLOCK_IN).astimezone(tz)
            raise ValidationError({"detail": f"You already clocked in today at {at:%-I:%M %p}."})
        schedule = schedule_for(workspace_id, person.member_id, day)
        accepted, _ = time_off_on(cfg, person.member_id, day)
        status = arrival_status(schedule, now, tz, excused=accepted is not None)
        if row is None:
            note_html = (
                f"<p>Covered by time off {_link(accepted)}.</p>" if accepted and status == "excused_late" else ""
            )
            row = _create_day(person, cfg, day, status, "started", note_html, "clock")
        parts = ensure_project(person, cfg.recorder)
        services.write_value(row.issue, parts["defs"][CLOCK_IN], now.isoformat(), user)
        set_status(row.issue, status, cfg.recorder)
    _comment(row.issue, user, note)
    return day_json(row, tz)


def clock_out(workspace_id, user, note=None, now=None):
    cfg = _ready(workspace_id)
    person = person_for(workspace_id, user)
    tz = zone(cfg)
    now = now or timezone.now()
    today = now.astimezone(tz).date()
    with transaction.atomic():
        row = None
        for day in (today, today - dt.timedelta(days=1)):
            candidate = _live_day(person, day)
            started = candidate and _value(candidate.issue, CLOCK_IN)
            if not started:
                continue
            if _value(candidate.issue, CLOCK_OUT):
                if day == today:
                    at = _value(candidate.issue, CLOCK_OUT).astimezone(tz)
                    raise ValidationError({"detail": f"You already clocked out today at {at:%-I:%M %p}."})
                continue
            if day == today or now - started <= LONGEST_SHIFT:
                row = candidate
                break
        if row is None:
            raise ValidationError({"detail": "Clock in first."})
        parts = ensure_project(person, cfg.recorder)
        services.write_value(row.issue, parts["defs"][CLOCK_OUT], now.isoformat(), user)
        issue = Issue.objects.get(id=row.issue_id)
        if parts["done"] and issue.state_id != parts["done"].id:
            previous = str(issue.state_id)
            with impersonate(cfg.recorder):
                issue.state = parts["done"]
                issue.save(update_fields=["state", "updated_at", "updated_by"])
            _activity(
                issue,
                cfg.recorder,
                "issue.activity.updated",
                {"state_id": str(parts["done"].id)},
                {"state_id": previous},
            )
    _comment(row.issue, user, note)
    return day_json(row, tz)


def relabel_after_edit(issue_id):
    """A changed Clock in (HR correcting a day) re-derives Present / Late / Excused late."""
    row = ExtAttendanceDay.objects.filter(issue_id=issue_id).select_related("person").first()
    if row is None:
        return
    cfg = config(row.person.workspace_id)
    if cfg is None:
        return
    issue = Issue.objects.filter(id=issue_id).first()
    current = _status_of(issue) if issue else None
    clock = _value(issue, CLOCK_IN) if issue else None
    if issue is None or clock is None or (current is not None and current not in ARRIVAL):
        return
    schedule = schedule_for(row.person.workspace_id, row.person.member_id, row.date)
    if schedule is None or schedule.effective_from > row.date:
        return
    accepted, _ = time_off_on(cfg, row.person.member_id, row.date)
    status = arrival_status(schedule, clock, zone(cfg), excused=accepted is not None)
    if status != current:
        set_status(issue, status, cfg.recorder)


# -- end of day ---------------------------------------------------------------------------------
def close_day(person, cfg, day):
    """Give a past day with no item its Holiday / leave / Absent item. Returns the status or None."""
    if not cfg.mark_absent or not person.mark_absent or (cfg.mark_absent_from and day < cfg.mark_absent_from):
        return None
    if day < person.created_at.astimezone(zone(cfg)).date() or _live_day(person, day) is not None:
        return None
    schedule = schedule_for(person.workspace_id, person.member_id, day)
    kind, holiday = day_kind(person.workspace_id, schedule, day)
    if kind == "off":
        return None
    if kind == "holiday":
        status, text = "holiday", f"<p>{html.escape(holiday)}</p>"
    else:
        accepted, pending = time_off_on(cfg, person.member_id, day)
        if accepted:
            status, text = leave_status(accepted), f"<p>Time off {_link(accepted)}.</p>"
        else:
            status = "absent"
            text = "<p>No clock-in on a working day.</p>"
            if pending:
                text += f"<p>A time-off request covering this day is still waiting for HR: {_link(pending)}.</p>"
    _create_day(person, cfg, day, status, "done", text, "auto")
    return status


def run_due(now=None):
    """Tick: close the past days (up to CATCH_UP_DAYS) for every workspace with the flag on."""
    now = now or timezone.now()
    made = 0
    for cfg in ExtAttendanceSettings.objects.filter(recorder__isnull=False).select_related("recorder"):
        if not services.is_enabled(cfg.workspace_id, FLAG):
            continue
        today = now.astimezone(zone(cfg)).date()
        people = ExtAttendancePerson.objects.filter(
            workspace_id=cfg.workspace_id, is_active=True, project__archived_at__isnull=True
        ).select_related("project")
        for person in people:
            for back in range(CATCH_UP_DAYS, 0, -1):
                if close_day(person, cfg, today - dt.timedelta(days=back)):
                    made += 1
    return made


# -- read models --------------------------------------------------------------------------------
def _hours(clock_in, clock_out):
    if clock_in and clock_out and clock_out > clock_in:
        return round((clock_out - clock_in).total_seconds() / 3600, 2)
    return None


def day_json(row, tz):
    issue = Issue.objects.filter(id=row.issue_id).select_related("project").first()
    cin, cout = _value(issue, CLOCK_IN), _value(issue, CLOCK_OUT)
    return {
        "date": row.date.isoformat(),
        "status": _status_of(issue),
        "clock_in": cin.astimezone(tz).isoformat() if cin else None,
        "clock_out": cout.astimezone(tz).isoformat() if cout else None,
        "hours": _hours(cin, cout),
        "issue_id": str(issue.id),
        "project_id": str(issue.project_id),
        "identifier": f"{issue.project.identifier}-{issue.sequence_id}",
        "name": issue.name,
    }


def _month_bounds(month):
    first = month.replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    return first, nxt - dt.timedelta(days=1)


def _rows(person, first, last, tz):
    rows = ExtAttendanceDay.objects.filter(
        person=person, date__gte=first, date__lte=last, issue__deleted_at__isnull=True
    )
    return [day_json(r, tz) for r in rows.order_by("date")]


def summary(days, working_days):
    counts = {k: 0 for k in STATUSES}
    for d in days:
        if d["status"]:
            counts[d["status"]] += 1
    return {
        "counts": counts,
        "working_days": working_days,
        "hours": round(sum(d["hours"] or 0 for d in days), 2),
        "missing_clock_out": sum(1 for d in days if d["clock_in"] and not d["clock_out"]),
    }


def working_days(person, first, last):
    n, day = 0, first
    while day <= last:
        if day_kind(person.workspace_id, schedule_for(person.workspace_id, person.member_id, day), day)[0] == "work":
            n += 1
        day += dt.timedelta(days=1)
    return n


def today_json(person, cfg, now):
    tz = zone(cfg)
    day = now.astimezone(tz).date()
    schedule = schedule_for(person.workspace_id, person.member_id, day)
    kind, holiday = day_kind(person.workspace_id, schedule, day)
    row = _live_day(person, day)
    yesterday = _live_day(person, day - dt.timedelta(days=1))
    open_yesterday = None
    if yesterday:
        y = day_json(yesterday, tz)
        if y["clock_in"] and not y["clock_out"]:
            started = dt.datetime.fromisoformat(y["clock_in"])
            if now - started <= LONGEST_SHIFT:
                open_yesterday = y
    return {
        "date": day.isoformat(),
        "kind": kind,
        "holiday": holiday,
        "shift_start": schedule.shift_start.strftime("%H:%M") if schedule else None,
        "late_after": late_after(schedule, day, tz).strftime("%H:%M") if schedule else None,
        "day": day_json(row, tz) if row else None,
        "open_yesterday": open_yesterday,
        "now": now.astimezone(tz).isoformat(),
    }


def me_json(workspace_id, user, month=None, now=None):
    cfg = _ready(workspace_id)
    person = person_for(workspace_id, user)
    now = now or timezone.now()
    tz = zone(cfg)
    month = month or now.astimezone(tz).date()
    first, last = _month_bounds(month)
    today = now.astimezone(tz).date()
    days = _rows(person, first, last, tz)
    return {
        "timezone": cfg.timezone,
        "project_id": str(person.project_id),
        "month": first.strftime("%Y-%m"),
        "today": today_json(person, cfg, now),
        "days": days,
        "summary": summary(days, working_days(person, first, min(last, today))),
    }


def can_see(user, person):
    """HR view of a person: workspace Admins, and Admins of that person's Staff file."""
    if user.id == person.member_id:
        return False  # your own days come through `me`, never the team view
    role = services.workspace_role(user, person.workspace.slug)
    if role is not None and role >= 20:
        return True
    return ProjectMember.objects.filter(
        project_id=person.project_id, member=user, is_active=True, role__gte=20
    ).exists()


def team_json(workspace_id, viewer, month=None, now=None):
    cfg = _ready(workspace_id)
    now = now or timezone.now()
    tz = zone(cfg)
    month = month or now.astimezone(tz).date()
    first, last = _month_bounds(month)
    today = now.astimezone(tz).date()
    people = (
        ExtAttendancePerson.objects.filter(workspace_id=workspace_id, is_active=True)
        .select_related("member", "project", "workspace")
        .order_by("member__first_name", "member__email")
    )
    out = []
    for person in people:
        if not can_see(viewer, person):
            continue
        days = _rows(person, first, last, tz)
        out.append(
            {
                "member_id": str(person.member_id),
                "name": person.member.display_name or person.member.email,
                "full_name": f"{person.member.first_name} {person.member.last_name}".strip(),
                "project_id": str(person.project_id),
                "project_identifier": person.project.identifier,
                "today": today_json(person, cfg, now),
                "days": days,
                "summary": summary(days, working_days(person, first, min(last, today))),
            }
        )
    return {"timezone": cfg.timezone, "month": first.strftime("%Y-%m"), "people": out}


# -- settings (workspace Admin) -----------------------------------------------------------------
def settings_json(workspace_id):
    cfg = config(workspace_id)
    return {
        "configured": cfg is not None,
        "timezone": cfg.timezone if cfg else "Asia/Dhaka",
        "recorder_id": str(cfg.recorder_id) if cfg and cfg.recorder_id else None,
        "time_off_project_id": str(cfg.time_off_project_id) if cfg and cfg.time_off_project_id else None,
        "mark_absent": cfg.mark_absent if cfg else True,
        "mark_absent_from": cfg.mark_absent_from.isoformat() if cfg and cfg.mark_absent_from else None,
        "schedules": [
            {
                "id": str(s.id),
                "member_id": str(s.member_id) if s.member_id else None,
                "effective_from": s.effective_from.isoformat(),
                "shift_start": s.shift_start.strftime("%H:%M"),
                "grace_minutes": s.grace_minutes,
                "workdays": s.workdays,
                "saturdays": s.saturdays,
            }
            for s in ExtAttendanceSchedule.objects.filter(workspace_id=workspace_id).order_by("effective_from")
        ],
        "holidays": [
            {"date": h.date.isoformat(), "name": h.name}
            for h in ExtAttendanceHoliday.objects.filter(workspace_id=workspace_id).order_by("date")
        ],
        "people": [
            {
                "member_id": str(p.member_id),
                "project_id": str(p.project_id),
                "is_active": p.is_active,
                "mark_absent": p.mark_absent,
            }
            for p in ExtAttendancePerson.objects.filter(workspace_id=workspace_id)
        ],
    }


def _date(value, field):
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValidationError({field: "Use YYYY-MM-DD."})


def _time(value, field):
    try:
        return dt.time.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValidationError({field: "Use HH:MM."})


def _days(values, allowed, field):
    try:
        out = sorted({int(v) for v in values or []})
    except (TypeError, ValueError):
        raise ValidationError({field: "Numbers only."})
    if any(v not in allowed for v in out):
        raise ValidationError({field: f"Allowed: {min(allowed)} to {max(allowed)}."})
    return out


def apply_settings(workspace, actor, data):
    """Replace the attendance settings, schedules, holidays and people with `data` (one transaction)."""
    ws_id = workspace.id
    members = set(
        WorkspaceMember.objects.filter(workspace_id=ws_id, is_active=True).values_list("member_id", flat=True)
    )
    member_ids = {str(m) for m in members}
    tzname = data.get("timezone") or "Asia/Dhaka"
    try:
        zoneinfo.ZoneInfo(tzname)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValidationError({"timezone": "Unknown timezone."})
    recorder = data.get("recorder_id") or str(actor.id)
    admins = {
        str(m)
        for m in WorkspaceMember.objects.filter(workspace_id=ws_id, is_active=True, role__gte=20).values_list(
            "member_id", flat=True
        )
    }
    if recorder not in admins:
        raise ValidationError({"recorder_id": "Pick a workspace Admin."})
    from plane.db.models import Project

    projects = {
        str(p)
        for p in Project.objects.filter(workspace_id=ws_id, archived_at__isnull=True).values_list("id", flat=True)
    }
    time_off = data.get("time_off_project_id")
    if time_off and str(time_off) not in projects:
        raise ValidationError({"time_off_project_id": "Unknown project."})

    schedules = []
    for i, s in enumerate(data.get("schedules") or []):
        member = s.get("member_id")
        if member and str(member) not in member_ids:
            raise ValidationError({f"schedules[{i}].member_id": "Not a workspace member."})
        grace = s.get("grace_minutes", 15)
        if not isinstance(grace, int) or not 0 <= grace <= 240:
            raise ValidationError({f"schedules[{i}].grace_minutes": "0 to 240."})
        schedules.append(
            ExtAttendanceSchedule(
                workspace_id=ws_id,
                member_id=member or None,
                effective_from=_date(s.get("effective_from"), f"schedules[{i}].effective_from"),
                shift_start=_time(s.get("shift_start"), f"schedules[{i}].shift_start"),
                grace_minutes=grace,
                workdays=_days(s.get("workdays", [7, 1, 2, 3, 4]), range(1, 8), f"schedules[{i}].workdays"),
                saturdays=_days(s.get("saturdays", [2, 3]), range(1, 6), f"schedules[{i}].saturdays"),
            )
        )
    keys = [(s.member_id, s.effective_from) for s in schedules]
    if len(keys) != len(set(keys)):
        raise ValidationError({"schedules": "Two schedules start on the same day for the same people."})

    holidays = {}
    for i, h in enumerate(data.get("holidays") or []):
        day = _date(h.get("date"), f"holidays[{i}].date")
        name = str(h.get("name") or "").strip()[:120]
        if not name:
            raise ValidationError({f"holidays[{i}].name": "Give the holiday a name."})
        holidays[day] = name

    people = {}
    for i, p in enumerate(data.get("people") or []):
        member, project = str(p.get("member_id")), str(p.get("project_id"))
        if member not in member_ids:
            raise ValidationError({f"people[{i}].member_id": "Not a workspace member."})
        if project not in projects:
            raise ValidationError({f"people[{i}].project_id": "Unknown project."})
        if not ProjectMember.objects.filter(project_id=project, member_id=member, is_active=True).exists():
            raise ValidationError({f"people[{i}].project_id": "The person must be a member of their Staff file."})
        people[member] = (project, bool(p.get("is_active", True)), bool(p.get("mark_absent", True)))

    with transaction.atomic():
        ExtAttendanceSettings.objects.update_or_create(
            workspace_id=ws_id,
            defaults={
                "timezone": tzname,
                "recorder_id": recorder,
                "time_off_project_id": time_off or None,
                "mark_absent": bool(data.get("mark_absent", True)),
                "mark_absent_from": _date(data["mark_absent_from"], "mark_absent_from")
                if data.get("mark_absent_from")
                else None,
            },
        )
        ExtAttendanceSchedule.all_objects.filter(workspace_id=ws_id).delete()
        ExtAttendanceSchedule.objects.bulk_create(schedules)
        ExtAttendanceHoliday.all_objects.filter(workspace_id=ws_id).delete()
        ExtAttendanceHoliday.objects.bulk_create(
            [ExtAttendanceHoliday(workspace_id=ws_id, date=d, name=n) for d, n in sorted(holidays.items())]
        )
        for person in ExtAttendancePerson.objects.filter(workspace_id=ws_id):
            if str(person.member_id) not in people:
                person.is_active = False
                person.save(update_fields=["is_active", "updated_at"])
        for member, (project, active, absent) in people.items():
            person = ExtAttendancePerson.objects.filter(workspace_id=ws_id, member_id=member).first()
            if person is None:
                ExtAttendancePerson.objects.create(
                    workspace_id=ws_id, member_id=member, project_id=project, is_active=active, mark_absent=absent
                )
            elif (str(person.project_id), person.is_active, person.mark_absent) != (project, active, absent):
                person.project_id, person.is_active, person.mark_absent = project, active, absent
                person.save(update_fields=["project_id", "is_active", "mark_absent", "updated_at"])
    return settings_json(ws_id)
