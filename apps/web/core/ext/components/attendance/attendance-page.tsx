/**
 * SPDX-License-Identifier: AGPL-3.0-only
 * Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import { ExtService } from "../../services/ext.service";
import type {
  TExtAttendanceDay,
  TExtAttendanceMe,
  TExtAttendanceSchedule,
  TExtAttendanceSettings,
  TExtAttendanceStatus,
  TExtAttendanceSummary,
  TExtAttendanceTeam,
  TExtAttendanceToday,
} from "../../types";

const service = new ExtService();

const STATUS: Record<TExtAttendanceStatus, { label: string; tone: string }> = {
  present: { label: "Present", tone: "text-success-primary" },
  late: { label: "Late", tone: "text-warning-primary" },
  excused_late: { label: "Excused late", tone: "text-success-primary" },
  absent: { label: "Absent", tone: "text-danger-primary" },
  holiday: { label: "Holiday", tone: "text-accent-primary" },
  time_off: { label: "Time off", tone: "text-accent-primary" },
  vacation: { label: "Vacation", tone: "text-accent-primary" },
  sick_leave: { label: "Sick leave", tone: "text-accent-primary" },
  unplanned_leave: { label: "Unplanned leave", tone: "text-danger-primary" },
};
const WEEKDAYS = [
  { n: 7, label: "Sun" },
  { n: 1, label: "Mon" },
  { n: 2, label: "Tue" },
  { n: 3, label: "Wed" },
  { n: 4, label: "Thu" },
  { n: 5, label: "Fri" },
  { n: 6, label: "Sat" },
];
const ORDINALS = ["1st", "2nd", "3rd", "4th", "5th"];
const cell = "px-3 py-2 text-left";
const toggle = (list: number[], n: number) => (list.includes(n) ? list.filter((x) => x !== n) : [...list, n]);
const input = "rounded-md border border-subtle bg-surface-1 px-2 py-1 text-13";

const errorText = (e: unknown, fallback: string) => {
  const err = e as { detail?: string; error?: string } & Record<string, unknown>;
  if (err?.detail) return String(err.detail);
  if (err?.error) return String(err.error);
  const first = err && typeof err === "object" ? Object.entries(err)[0] : undefined;
  return first ? `${first[0]}: ${String(first[1])}` : fallback;
};

function useTime(timeZone: string) {
  return useMemo(() => {
    const time = new Intl.DateTimeFormat("en-US", { timeZone, hour: "numeric", minute: "2-digit" });
    const date = new Intl.DateTimeFormat("en-US", {
      timeZone: "UTC",
      weekday: "short",
      month: "short",
      day: "numeric",
    });
    const long = new Intl.DateTimeFormat("en-US", {
      timeZone: "UTC",
      weekday: "long",
      month: "long",
      day: "numeric",
      year: "numeric",
    });
    return {
      time: (iso: string | null) => (iso ? time.format(new Date(iso)) : "—"),
      day: (isoDate: string) => date.format(new Date(`${isoDate}T00:00:00Z`)),
      longDay: (isoDate: string) => long.format(new Date(`${isoDate}T00:00:00Z`)),
    };
  }, [timeZone]);
}

const shiftMonth = (month: string, by: number) => {
  const [y, m] = month.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + by, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
};
const monthLabel = (month: string) =>
  new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "long", year: "numeric" }).format(
    new Date(`${month}-01T00:00:00Z`)
  );

function StatusText({ status }: { status: TExtAttendanceStatus | null }) {
  if (!status) return <span className="text-tertiary">—</span>;
  return <span className={STATUS[status].tone}>{STATUS[status].label}</span>;
}

function MonthNav({ month, onChange }: { month: string; onChange: (m: string) => void }) {
  return (
    <div className="flex items-center gap-3 text-13">
      <button type="button" className="text-accent-primary" onClick={() => onChange(shiftMonth(month, -1))}>
        ‹ Previous
      </button>
      <span className="min-w-32 text-center font-medium text-primary">{monthLabel(month)}</span>
      <button type="button" className="text-accent-primary" onClick={() => onChange(shiftMonth(month, 1))}>
        Next ›
      </button>
    </div>
  );
}

function SummaryLine({ summary }: { summary: TExtAttendanceSummary }) {
  const c = summary.counts;
  const leave = c.time_off + c.vacation + c.sick_leave + c.unplanned_leave;
  const parts = [
    `${summary.working_days} working days so far`,
    `${c.present} present`,
    `${c.late} late`,
    c.excused_late ? `${c.excused_late} excused late` : "",
    `${c.absent} absent`,
    leave ? `${leave} on leave` : "",
    c.holiday ? `${c.holiday} holidays` : "",
    `${summary.hours} hours`,
    summary.missing_clock_out ? `${summary.missing_clock_out} without clock-out` : "",
  ].filter(Boolean);
  return <p className="text-13 text-secondary">{parts.join(" · ")}</p>;
}

function DaysTable({
  workspaceSlug,
  days,
  timeZone,
}: {
  workspaceSlug: string;
  days: TExtAttendanceDay[];
  timeZone: string;
}) {
  const fmt = useTime(timeZone);
  if (days.length === 0) return <p className="py-4 text-13 text-tertiary">No days recorded this month.</p>;
  return (
    <table className="min-w-full border-collapse text-13">
      <thead className="bg-surface-2 text-secondary">
        <tr>
          <th className={cell}>Day</th>
          <th className={cell}>Status</th>
          <th className={cell}>Clock in</th>
          <th className={cell}>Clock out</th>
          <th className={cell}>Hours</th>
          <th className={cell}>Work item</th>
        </tr>
      </thead>
      <tbody>
        {[...days]
          .sort((a, b) => b.date.localeCompare(a.date))
          .map((d) => (
            <tr key={d.date} className="border-t border-subtle">
              <td className={cell}>{fmt.day(d.date)}</td>
              <td className={cell}>
                <StatusText status={d.status} />
              </td>
              <td className={cell}>{fmt.time(d.clock_in)}</td>
              <td className={cell}>
                {d.clock_in && !d.clock_out ? (
                  <span className="text-warning-primary">missing</span>
                ) : (
                  fmt.time(d.clock_out)
                )}
              </td>
              <td className={cell}>{d.hours ?? "—"}</td>
              <td className={cell}>
                <Link
                  className="text-accent-primary hover:underline"
                  href={`/${workspaceSlug}/browse/${d.identifier}/`}
                >
                  {d.identifier}
                </Link>
              </td>
            </tr>
          ))}
      </tbody>
    </table>
  );
}

function TodayCard({
  today,
  timeZone,
  busy,
  onClock,
}: {
  today: TExtAttendanceToday;
  timeZone: string;
  busy: boolean;
  onClock: (action: "clock-in" | "clock-out", note: string) => void;
}) {
  const fmt = useTime(timeZone);
  const [note, setNote] = useState("");
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(t);
  }, []);
  const localTime = new Intl.DateTimeFormat("en-GB", { timeZone, hour: "2-digit", minute: "2-digit" }).format(now);
  const day = today.day;
  const lateNow = !day && today.kind === "work" && today.late_after !== null && localTime > today.late_after;
  const rule =
    today.kind === "holiday"
      ? `Holiday: ${today.holiday}`
      : today.kind === "off"
        ? "Day off"
        : today.shift_start
          ? `Shift starts ${today.shift_start} · late after ${today.late_after}`
          : "";

  let body;
  if (today.open_yesterday && !day?.clock_in) {
    body = (
      <>
        <p className="text-13 text-secondary">
          Your shift from {fmt.day(today.open_yesterday.date)} is still open (clocked in{" "}
          {fmt.time(today.open_yesterday.clock_in)}).
        </p>
        <div className="flex gap-2">
          <Button variant="primary" disabled={busy} onClick={() => onClock("clock-out", note)}>
            Clock out of last night&apos;s shift
          </Button>
          <Button variant="secondary" disabled={busy} onClick={() => onClock("clock-in", note)}>
            Clock in for today
          </Button>
        </div>
      </>
    );
  } else if (!day?.clock_in) {
    body = (
      <>
        {lateNow && (
          <p className="text-13 text-warning-primary">It&apos;s past {today.late_after}: this counts as late.</p>
        )}
        <Button variant="primary" size="lg" disabled={busy} onClick={() => onClock("clock-in", note)}>
          Clock in
        </Button>
      </>
    );
  } else if (!day.clock_out) {
    body = (
      <>
        <p className="text-14 text-primary">
          Clocked in at {fmt.time(day.clock_in)} · <StatusText status={day.status} />
        </p>
        <Button variant="primary" size="lg" disabled={busy} onClick={() => onClock("clock-out", note)}>
          Clock out
        </Button>
      </>
    );
  } else {
    body = (
      <p className="text-14 text-primary">
        Done for today: {fmt.time(day.clock_in)} – {fmt.time(day.clock_out)} ({day.hours ?? "—"} h) ·{" "}
        <StatusText status={day.status} />
      </p>
    );
  }
  const canNote = !day?.clock_out;
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-subtle bg-surface-1 p-6">
      <div>
        <h2 className="text-18 font-semibold text-primary">{fmt.longDay(today.date)}</h2>
        <p className="text-13 text-secondary">
          {localTime} ({timeZone}){rule ? ` · ${rule}` : ""}
        </p>
      </div>
      {body}
      {canNote && (
        <textarea
          className={`${input} max-w-xl`}
          rows={2}
          maxLength={2000}
          placeholder="Add a note (optional), e.g. why you are late or leaving early"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      )}
    </section>
  );
}

function MyAttendance({ workspaceSlug, initial }: { workspaceSlug: string; initial: TExtAttendanceMe }) {
  const [data, setData] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [month, setMonth] = useState(initial.month);

  useEffect(() => {
    if (month === data.month) return;
    service
      .getMyAttendance(workspaceSlug, month)
      .then(setData)
      .catch(() => undefined);
  }, [month, data.month, workspaceSlug]);

  const clock = async (action: "clock-in" | "clock-out", note: string) => {
    setBusy(true);
    try {
      const next = await service.clock(workspaceSlug, action, note.trim() || undefined);
      setData(next);
      setMonth(next.month);
      setToast({ type: TOAST_TYPE.SUCCESS, title: action === "clock-in" ? "Clocked in" : "Clocked out" });
    } catch (e) {
      setToast({ type: TOAST_TYPE.ERROR, title: "Not recorded", message: errorText(e, "Please try again.") });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <TodayCard key={data.today.date} today={data.today} timeZone={data.timezone} busy={busy} onClock={clock} />
      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h3 className="text-14 font-semibold text-primary">My days</h3>
          <MonthNav month={month} onChange={setMonth} />
        </div>
        <SummaryLine summary={data.summary} />
        <DaysTable workspaceSlug={workspaceSlug} days={data.days} timeZone={data.timezone} />
      </section>
    </div>
  );
}

function TeamAttendance({ workspaceSlug, initial }: { workspaceSlug: string; initial: TExtAttendanceTeam }) {
  const [data, setData] = useState(initial);
  const [month, setMonth] = useState(initial.month);
  const [open, setOpen] = useState<string | null>(null);
  const fmt = useTime(data.timezone);

  useEffect(() => {
    if (month === data.month) return;
    service
      .getTeamAttendance(workspaceSlug, month)
      .then(setData)
      .catch(() => undefined);
  }, [month, data.month, workspaceSlug]);

  const todayText = (t: TExtAttendanceToday) => {
    if (t.day?.clock_in)
      return `In ${fmt.time(t.day.clock_in)}${t.day.clock_out ? `, out ${fmt.time(t.day.clock_out)}` : ""}`;
    if (t.day) return STATUS[t.day.status ?? "absent"].label;
    if (t.kind === "holiday") return "Holiday";
    if (t.kind === "off") return "Day off";
    return "Not in yet";
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-14 font-semibold text-primary">Team</h3>
        <MonthNav month={month} onChange={setMonth} />
      </div>
      <table className="min-w-full border-collapse text-13">
        <thead className="bg-surface-2 text-secondary">
          <tr>
            <th className={cell}>Person</th>
            <th className={cell}>Today</th>
            <th className={cell}>Present</th>
            <th className={cell}>Late</th>
            <th className={cell}>Absent</th>
            <th className={cell}>Leave</th>
            <th className={cell}>Hours</th>
            <th className={cell}>No clock-out</th>
          </tr>
        </thead>
        <tbody>
          {data.people.map((p) => {
            const c = p.summary.counts;
            return [
              <tr key={p.member_id} className="border-t border-subtle">
                <td className={cell}>
                  <button
                    type="button"
                    className="text-accent-primary hover:underline"
                    onClick={() => setOpen(open === p.member_id ? null : p.member_id)}
                  >
                    {p.full_name || p.name}
                  </button>
                </td>
                <td className={cell}>{todayText(p.today)}</td>
                <td className={cell}>{c.present + c.excused_late}</td>
                <td className={cell}>{c.late}</td>
                <td className={cell}>{c.absent}</td>
                <td className={cell}>{c.time_off + c.vacation + c.sick_leave + c.unplanned_leave}</td>
                <td className={cell}>{p.summary.hours}</td>
                <td className={cell}>{p.summary.missing_clock_out || "—"}</td>
              </tr>,
              open === p.member_id && (
                <tr key={`${p.member_id}-days`}>
                  <td colSpan={8} className="bg-surface-2 p-3">
                    <DaysTable workspaceSlug={workspaceSlug} days={p.days} timeZone={data.timezone} />
                  </td>
                </tr>
              ),
            ];
          })}
        </tbody>
      </table>
      <p className="text-12 text-tertiary">
        To correct a day, open its work item and change Clock in, Clock out or the Attendance label. A changed Clock in
        re-checks Present or Late.
      </p>
    </div>
  );
}

function AttendanceSettings({ workspaceSlug, initial }: { workspaceSlug: string; initial: TExtAttendanceSettings }) {
  const [draft, setDraft] = useState(initial);
  const [saving, setSaving] = useState(false);

  const set = <K extends keyof TExtAttendanceSettings>(key: K, value: TExtAttendanceSettings[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));
  const setSchedule = (i: number, patch: Partial<TExtAttendanceSchedule>) =>
    set(
      "schedules",
      draft.schedules.map((s, j) => (j === i ? { ...s, ...patch } : s))
    );

  const people = initial.directory.members;
  const admins = people.filter((p) => p.workspace_role >= 20);
  const projects = initial.directory.projects;
  const isAdmin = initial.access === "admin";
  const name = (id: string | null) => people.find((p) => p.id === id)?.display_name ?? id ?? "";

  const save = async () => {
    setSaving(true);
    try {
      setDraft(await service.setAttendanceSettings(workspaceSlug, draft));
      setToast({ type: TOAST_TYPE.SUCCESS, title: "Attendance settings saved" });
    } catch (e) {
      setToast({ type: TOAST_TYPE.ERROR, title: "Not saved", message: errorText(e, "Check the highlighted values.") });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex max-w-5xl flex-col gap-8 text-13">
      <section className="flex flex-col gap-3">
        <h3 className="text-14 font-semibold text-primary">General</h3>
        {!isAdmin && <p className="text-secondary">Only workspace Admins can change these.</p>}
        <fieldset disabled={!isAdmin} className="flex flex-col gap-3 disabled:opacity-60">
          <label className="flex items-center gap-3">
            <span className="w-48 text-secondary">Timezone</span>
            <input className={input} value={draft.timezone} onChange={(e) => set("timezone", e.target.value)} />
          </label>
          <label className="flex items-center gap-3">
            <span className="w-48 text-secondary">Days are recorded by</span>
            <select
              className={input}
              value={draft.recorder_id ?? ""}
              onChange={(e) => set("recorder_id", e.target.value || null)}
            >
              <option value="">(me)</option>
              {admins.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name || p.email}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-3">
            <span className="w-48 text-secondary">Time-off requests come from</span>
            <select
              className={input}
              value={draft.time_off_project_id ?? ""}
              onChange={(e) => set("time_off_project_id", e.target.value || null)}
            >
              <option value="">(none)</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-3">
            <span className="w-48 text-secondary">Mark missed working days</span>
            <input type="checkbox" checked={draft.mark_absent} onChange={(e) => set("mark_absent", e.target.checked)} />
            <span className="text-secondary">from</span>
            <input
              type="date"
              className={input}
              value={draft.mark_absent_from ?? ""}
              onChange={(e) => set("mark_absent_from", e.target.value || null)}
            />
          </label>
        </fieldset>
      </section>

      <section className="flex flex-col gap-3">
        <h3 className="text-14 font-semibold text-primary">Shift schedule</h3>
        <p className="text-secondary">
          Each row applies from its start date until the next row. A row for one person overrides the team&apos;s.
        </p>
        <table className="min-w-full border-collapse">
          <thead className="bg-surface-2 text-secondary">
            <tr>
              <th className={cell}>From</th>
              <th className={cell}>For</th>
              <th className={cell}>Shift starts</th>
              <th className={cell}>Late after (min)</th>
              <th className={cell}>Working days</th>
              <th className={cell}>Working Saturdays</th>
              <th className={cell} />
            </tr>
          </thead>
          <tbody>
            {draft.schedules.map((s, i) => (
              <tr key={s.id ?? i} className="border-t border-subtle">
                <td className={cell}>
                  <input
                    type="date"
                    className={input}
                    value={s.effective_from}
                    onChange={(e) => setSchedule(i, { effective_from: e.target.value })}
                  />
                </td>
                <td className={cell}>
                  <select
                    className={input}
                    value={s.member_id ?? ""}
                    onChange={(e) => setSchedule(i, { member_id: e.target.value || null })}
                  >
                    <option value="">Everyone</option>
                    {draft.people.map((p) => (
                      <option key={p.member_id} value={p.member_id}>
                        {name(p.member_id)}
                      </option>
                    ))}
                  </select>
                </td>
                <td className={cell}>
                  <input
                    type="time"
                    className={input}
                    value={s.shift_start}
                    onChange={(e) => setSchedule(i, { shift_start: e.target.value })}
                  />
                </td>
                <td className={cell}>
                  <input
                    type="number"
                    min={0}
                    max={240}
                    className={`${input} w-20`}
                    value={s.grace_minutes}
                    onChange={(e) => setSchedule(i, { grace_minutes: Number(e.target.value) })}
                  />
                </td>
                <td className={cell}>
                  <div className="flex gap-2">
                    {WEEKDAYS.map((w) => (
                      <label key={w.n} className="flex items-center gap-1">
                        <input
                          type="checkbox"
                          checked={s.workdays.includes(w.n)}
                          onChange={() => setSchedule(i, { workdays: toggle(s.workdays, w.n) })}
                        />
                        {w.label}
                      </label>
                    ))}
                  </div>
                </td>
                <td className={cell}>
                  <div className="flex gap-2">
                    {ORDINALS.map((label, k) => (
                      <label key={label} className="flex items-center gap-1">
                        <input
                          type="checkbox"
                          disabled={s.workdays.includes(6)}
                          checked={s.workdays.includes(6) || s.saturdays.includes(k + 1)}
                          onChange={() => setSchedule(i, { saturdays: toggle(s.saturdays, k + 1) })}
                        />
                        {label}
                      </label>
                    ))}
                  </div>
                </td>
                <td className={cell}>
                  <button
                    type="button"
                    className="text-danger-primary"
                    onClick={() =>
                      set(
                        "schedules",
                        draft.schedules.filter((_, j) => j !== i)
                      )
                    }
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div>
          <Button
            variant="secondary"
            onClick={() =>
              set("schedules", [
                ...draft.schedules,
                {
                  member_id: null,
                  effective_from: new Date().toISOString().slice(0, 10),
                  shift_start: draft.schedules.at(-1)?.shift_start ?? "07:30",
                  grace_minutes: 15,
                  workdays: [7, 1, 2, 3, 4],
                  saturdays: [2, 3],
                },
              ])
            }
          >
            Add a schedule change
          </Button>
        </div>
      </section>

      <section className="flex flex-col gap-3">
        <h3 className="text-14 font-semibold text-primary">Holidays</h3>
        {draft.holidays.map((h, i) => (
          <div key={i} className="flex items-center gap-3">
            <input
              type="date"
              className={input}
              value={h.date}
              onChange={(e) =>
                set(
                  "holidays",
                  draft.holidays.map((x, j) => (j === i ? { ...x, date: e.target.value } : x))
                )
              }
            />
            <input
              className={`${input} w-72`}
              value={h.name}
              placeholder="Name"
              onChange={(e) =>
                set(
                  "holidays",
                  draft.holidays.map((x, j) => (j === i ? { ...x, name: e.target.value } : x))
                )
              }
            />
            <button
              type="button"
              className="text-danger-primary"
              onClick={() =>
                set(
                  "holidays",
                  draft.holidays.filter((_, j) => j !== i)
                )
              }
            >
              Remove
            </button>
          </div>
        ))}
        <div>
          <Button
            variant="secondary"
            onClick={() =>
              set("holidays", [...draft.holidays, { date: new Date().toISOString().slice(0, 10), name: "" }])
            }
          >
            Add a holiday
          </Button>
        </div>
      </section>

      <section className="flex flex-col gap-3">
        <h3 className="text-14 font-semibold text-primary">People who clock in</h3>
        <p className="text-secondary">
          Each person&apos;s days go into their own Staff file. They must be a member of it.
        </p>
        {draft.people.map((p, i) =>
          p.editable === false ? (
            <div key={i} className="flex items-center gap-3 text-secondary">
              <span className="w-64 truncate">{name(p.member_id)}</span>
              <span className="w-64 truncate">{p.project_name}</span>
              <span>
                {p.is_active ? "Active" : "Not active"} ·{" "}
                {p.mark_absent ? "Missed days marked" : "Clocking in optional"}
              </span>
              <span className="text-tertiary">(a workspace Admin can change this)</span>
            </div>
          ) : (
            <div key={i} className="flex items-center gap-3">
              <select
                className={input}
                value={p.member_id}
                onChange={(e) =>
                  set(
                    "people",
                    draft.people.map((x, j) => (j === i ? { ...x, member_id: e.target.value } : x))
                  )
                }
              >
                <option value="">Pick a person</option>
                {people.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.display_name || m.email}
                  </option>
                ))}
              </select>
              <select
                className={input}
                value={p.project_id}
                onChange={(e) =>
                  set(
                    "people",
                    draft.people.map((x, j) => (j === i ? { ...x, project_id: e.target.value } : x))
                  )
                }
              >
                <option value="">Pick their Staff file</option>
                {projects.map((pr) => (
                  <option key={pr.id} value={pr.id}>
                    {pr.name}
                  </option>
                ))}
              </select>
              <label className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={p.is_active}
                  onChange={(e) =>
                    set(
                      "people",
                      draft.people.map((x, j) => (j === i ? { ...x, is_active: e.target.checked } : x))
                    )
                  }
                />
                Active
              </label>
              <label className="flex items-center gap-1" title="Off: clocking in is optional; missed days stay blank">
                <input
                  type="checkbox"
                  checked={p.mark_absent}
                  onChange={(e) =>
                    set(
                      "people",
                      draft.people.map((x, j) => (j === i ? { ...x, mark_absent: e.target.checked } : x))
                    )
                  }
                />
                Mark missed days
              </label>
              <button
                type="button"
                className="text-danger-primary"
                onClick={() =>
                  set(
                    "people",
                    draft.people.filter((_, j) => j !== i)
                  )
                }
              >
                Remove
              </button>
            </div>
          )
        )}
        <div>
          <Button
            variant="secondary"
            onClick={() =>
              set("people", [...draft.people, { member_id: "", project_id: "", is_active: true, mark_absent: true }])
            }
          >
            Add a person
          </Button>
        </div>
      </section>

      <div className="flex gap-2">
        <Button variant="secondary" disabled={saving} onClick={() => setDraft(initial)}>
          Discard changes
        </Button>
        <Button variant="primary" disabled={saving} onClick={() => void save()}>
          Save
        </Button>
      </div>
    </div>
  );
}

type TTab = "me" | "team" | "settings";

/** Clock in / Clock out, your own month, HR's team view and the rules (workspace Admins; HR managers for the rota). */
export function AttendancePage({ workspaceSlug }: { workspaceSlug: string }) {
  const [me, setMe] = useState<TExtAttendanceMe | null>(null);
  const [team, setTeam] = useState<TExtAttendanceTeam | null>(null);
  const [settings, setSettings] = useState<TExtAttendanceSettings | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [tab, setTab] = useState<TTab>("me");

  const load = useCallback(async () => {
    const [m, t, s] = await Promise.allSettled([
      service.getMyAttendance(workspaceSlug),
      service.getTeamAttendance(workspaceSlug),
      service.getAttendanceSettings(workspaceSlug),
    ]);
    const mine = m.status === "fulfilled" ? m.value : null;
    const others = t.status === "fulfilled" && t.value.people.length > 0 ? t.value : null;
    setMe(mine);
    setTeam(others);
    setSettings(s.status === "fulfilled" ? s.value : null);
    setTab(mine ? "me" : others ? "team" : "settings");
    setLoaded(true);
  }, [workspaceSlug]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!loaded) return null;
  const tabs = [
    me && { key: "me" as const, label: "My attendance" },
    team && { key: "team" as const, label: "Team" },
    settings && { key: "settings" as const, label: "Settings" },
  ].filter(Boolean) as { key: TTab; label: string }[];
  if (tabs.length === 0)
    return <div className="p-8 text-13 text-secondary">Attendance is not set up for you here. Ask HR to add you.</div>;

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      {tabs.length > 1 && (
        <nav className="flex shrink-0 gap-4 border-b border-subtle px-6 text-13">
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className={`border-b-2 py-2.5 ${tab === t.key ? "border-accent-strong text-primary" : "border-transparent text-secondary"}`}
            >
              {t.label}
            </button>
          ))}
        </nav>
      )}
      <div className="min-h-0 flex-1 overflow-auto p-6">
        {tab === "me" && me && <MyAttendance workspaceSlug={workspaceSlug} initial={me} />}
        {tab === "team" && team && <TeamAttendance workspaceSlug={workspaceSlug} initial={team} />}
        {tab === "settings" && settings && <AttendanceSettings workspaceSlug={workspaceSlug} initial={settings} />}
      </div>
    </div>
  );
}
