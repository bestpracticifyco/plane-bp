/**
 * SPDX-License-Identifier: AGPL-3.0-only
 * Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
 */

import { Fragment, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Dialog, Transition } from "@headlessui/react";
import { observer } from "mobx-react";
import { Button } from "@plane/propel/button";
import { EModalPosition, EModalWidth } from "@plane/ui";
import { useProjectState } from "@/hooks/store/use-project-state";
import { extStore } from "../../store/ext.store";
import { EXT_COPY_FIELDS } from "../../types";
import type { TExtCopyField, TExtRecurrenceRule, TExtRecurrenceTrigger, TExtSchedule } from "../../types";

const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const ORDINAL = ["", "First", "Second", "Third", "Fourth", "Fifth"];
const ALL_DAYS = [0, 1, 2, 3, 4, 5, 6];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

type Preset =
  | "daily"
  | "weekly"
  | "biweekly"
  | "semimonthly"
  | "monthly"
  | "quarterly"
  | "yearly"
  | "days_after"
  | "custom";
type MonthMode = "same_day" | "nth_weekday" | "last_weekday" | "first_day" | "last_day" | "business_day";
type Unit = "daily" | "weekly" | "monthly" | "yearly";

type Form = {
  preset: Preset;
  interval: number;
  unit: Unit;
  dailyDays: number[];
  weekdays: number[];
  monthMode: MonthMode;
  businessDay: number;
  daysAfter: number;
  trigger: TExtRecurrenceTrigger;
  triggerStateId: string;
  runTime: string;
  runOn: "due" | "start";
  createNew: boolean;
  newStateId: string;
  copyFields: TExtCopyField[];
  ends: "never" | "count" | "date";
  endCount: number;
  endDate: string;
  resync: boolean;
};

/** Monday-based weekday (0..6) of an ISO date, in local time. */
const weekdayOf = (d: Date) => (d.getDay() + 6) % 7;

function dueFacts(iso: string | null) {
  const d = iso ? new Date(`${iso}T00:00:00`) : new Date();
  const lastDay = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
  return {
    date: d,
    day: d.getDate(),
    month: d.getMonth() + 1,
    weekday: weekdayOf(d),
    nth: Math.ceil(d.getDate() / 7),
    isLastWeekday: d.getDate() + 7 > lastDay,
  };
}

function initialForm(rule: TExtRecurrenceRule | null, due: ReturnType<typeof dueFacts>): Form {
  const base: Form = {
    preset: "monthly",
    interval: 1,
    unit: "monthly",
    dailyDays: ALL_DAYS,
    weekdays: [due.weekday],
    monthMode: "same_day",
    businessDay: 5,
    daysAfter: 7,
    trigger: "completed",
    triggerStateId: "",
    runTime: "06:00",
    runOn: "due",
    createNew: true,
    newStateId: "",
    copyFields: EXT_COPY_FIELDS.map(([k]) => k),
    ends: "never",
    endCount: 12,
    endDate: "",
    resync: false,
  };
  if (!rule) return base;
  const s = rule.schedule;
  const f: Form = {
    ...base,
    trigger: rule.trigger,
    triggerStateId: rule.trigger_state_id ?? "",
    runTime: rule.run_time,
    runOn: rule.run_on ?? "due",
    createNew: rule.create_new,
    newStateId: rule.new_state_id ?? "",
    copyFields: rule.copy_fields,
    ends: rule.end_count ? "count" : rule.end_date ? "date" : "never",
    endCount: rule.end_count ?? 12,
    endDate: rule.end_date ?? "",
    resync: rule.resync_on_due_change,
    interval: s.freq === "days_after" ? 1 : (s.interval ?? 1),
  };
  if (s.freq === "days_after") return { ...f, preset: "days_after", daysAfter: s.days };
  if (s.freq === "daily")
    return {
      ...f,
      preset: f.interval === 1 ? "daily" : "custom",
      unit: "daily",
      dailyDays: s.weekdays ?? (s.skip_weekends ? [0, 1, 2, 3, 4] : ALL_DAYS),
    };
  if (s.freq === "weekly")
    return {
      ...f,
      preset: f.interval === 1 ? "weekly" : f.interval === 2 ? "biweekly" : "custom",
      unit: "weekly",
      weekdays: s.weekdays,
    };
  if (s.freq === "yearly") return { ...f, preset: f.interval === 1 ? "yearly" : "custom", unit: "yearly" };
  if (s.mode === "days") return { ...f, preset: "semimonthly" };
  const monthMode: MonthMode =
    s.mode === "nth_weekday" ? (s.nth === -1 ? "last_weekday" : "nth_weekday") : (s.mode as MonthMode);
  return {
    ...f,
    preset: f.interval === 1 ? "monthly" : f.interval === 3 ? "quarterly" : "custom",
    unit: "monthly",
    monthMode,
    businessDay: s.mode === "business_day" ? (s.nth ?? 5) : 5,
  };
}

function toSchedule(f: Form, due: ReturnType<typeof dueFacts>): TExtSchedule {
  const monthly = (interval: number): TExtSchedule => {
    if (f.monthMode === "nth_weekday")
      return { freq: "monthly", interval, mode: "nth_weekday", nth: Math.min(due.nth, 5), weekday: due.weekday };
    if (f.monthMode === "last_weekday")
      return { freq: "monthly", interval, mode: "nth_weekday", nth: -1, weekday: due.weekday };
    if (f.monthMode === "business_day") return { freq: "monthly", interval, mode: "business_day", nth: f.businessDay };
    if (f.monthMode === "same_day") return { freq: "monthly", interval, mode: "same_day", day: due.day };
    return { freq: "monthly", interval, mode: f.monthMode };
  };
  switch (f.preset) {
    case "daily":
      return { freq: "daily", interval: 1, weekdays: f.dailyDays };
    case "weekly":
      return { freq: "weekly", interval: 1, weekdays: f.weekdays };
    case "biweekly":
      return { freq: "weekly", interval: 2, weekdays: f.weekdays };
    case "semimonthly":
      return { freq: "monthly", interval: 1, mode: "days", days: [1, 15] };
    case "monthly":
      return monthly(1);
    case "quarterly":
      return monthly(3);
    case "yearly":
      return { freq: "yearly", interval: 1, month: due.month, day: due.day };
    case "days_after":
      return { freq: "days_after", days: f.daysAfter };
    case "custom":
      if (f.unit === "daily") return { freq: "daily", interval: f.interval, weekdays: f.dailyDays };
      if (f.unit === "weekly") return { freq: "weekly", interval: f.interval, weekdays: f.weekdays };
      if (f.unit === "yearly") return { freq: "yearly", interval: f.interval, month: due.month, day: due.day };
      return monthly(f.interval);
  }
}

/** Toggle buttons for Monday..Sunday; at least one day stays selected. */
function DayPicker({ value, onChange }: { value: number[]; onChange: (days: number[]) => void }) {
  const toggle = (i: number) => {
    const next = value.includes(i) ? value.filter((d) => d !== i) : [...value, i];
    if (next.length) onChange(ALL_DAYS.filter((d) => next.includes(d)));
  };
  return (
    <div className="flex flex-wrap gap-1">
      {DAY_NAMES.map((n, i) => (
        <button
          key={n}
          type="button"
          aria-pressed={value.includes(i)}
          className={`h-7 rounded-sm border px-2 text-12 ${
            value.includes(i) ? "border-accent-strong bg-accent-primary text-on-color" : "border-subtle text-secondary"
          }`}
          onClick={() => toggle(i)}
        >
          {n.slice(0, 3)}
        </button>
      ))}
    </div>
  );
}

const field = "h-8 rounded-md border border-subtle bg-surface-1 px-2 text-13 text-primary";

/**
 * Plane's ModalCore, plus `data-prevent-outside-click` on the dialog root. The side peek closes on any
 * mousedown outside itself unless the target sits inside such an element, and this dialog is portalled
 * out of the peek. Without the marker, the first click in the dialog closed the peek, which unmounted
 * the dialog under the open <select> (Windows: everything vanished; macOS: the native option list was
 * left floating at the top-left of the screen).
 */
function ExtModal({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  return (
    <Transition.Root show as={Fragment}>
      <Dialog as="div" className="relative z-30" onClose={onClose} data-prevent-outside-click>
        <div className="fixed inset-0 bg-backdrop transition-opacity" />
        <div className="fixed inset-0 z-30 overflow-y-auto">
          <div className={EModalPosition.TOP}>
            <Dialog.Panel
              className={`relative w-full transform rounded-lg bg-surface-1 text-left shadow-raised-200 ${EModalWidth.XL}`}
            >
              {children}
            </Dialog.Panel>
          </div>
        </div>
      </Dialog>
    </Transition.Root>
  );
}

type Props = {
  workspaceSlug: string;
  projectId: string;
  issueId: string;
  dueDate: string | null;
  onClose: () => void;
};

/** ClickUp-style "Recurring" dialog for one work item. Mounted only while open, so the form
 *  is initialised from the saved rule each time it opens. */
export const RecurrenceEditor = observer(function RecurrenceEditor(props: Props) {
  const { workspaceSlug, projectId, issueId, dueDate, onClose } = props;
  const rule = extStore.recurrence[issueId] ?? null;
  const due = useMemo(() => dueFacts(rule?.due ?? dueDate), [rule?.due, dueDate]);
  const [form, setForm] = useState<Form>(() => initialForm(rule, due));
  const [showFields, setShowFields] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const { getProjectStates, fetchProjectStates } = useProjectState();
  const states = getProjectStates(projectId) ?? [];

  const statesLoaded = getProjectStates(projectId) !== undefined;
  useEffect(() => {
    if (!statesLoaded) void fetchProjectStates(workspaceSlug, projectId);
  }, [statesLoaded, fetchProjectStates, workspaceSlug, projectId]);

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((prev) => ({ ...prev, [k]: v }));
  const isMonthly =
    form.preset === "monthly" || form.preset === "quarterly" || (form.preset === "custom" && form.unit === "monthly");
  const isWeekly =
    form.preset === "weekly" || form.preset === "biweekly" || (form.preset === "custom" && form.unit === "weekly");
  const isDaily = form.preset === "daily" || (form.preset === "custom" && form.unit === "daily");

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await extStore.saveRecurrence(workspaceSlug, projectId, issueId, {
        schedule: toSchedule(form, due),
        trigger: form.preset === "days_after" && form.trigger === "schedule" ? "completed" : form.trigger,
        trigger_state_id: form.trigger === "state" ? form.triggerStateId || null : null,
        run_time: form.runTime,
        run_on: form.runOn,
        create_new: form.createNew,
        new_state_id: form.newStateId || null,
        copy_fields: form.copyFields,
        end_count: form.ends === "count" ? form.endCount : null,
        end_date: form.ends === "date" ? form.endDate || null : null,
        resync_on_due_change: form.resync,
      });
      onClose();
    } catch (e) {
      setError(
        Object.entries((e as Record<string, string[]>) ?? {})
          .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : String(v)}`)
          .join("\n") || "Could not save."
      );
    } finally {
      setSaving(false);
    }
  };

  const stop = async () => {
    await extStore.stopRecurrence(workspaceSlug, projectId, issueId);
    onClose();
  };

  const dayName = DAY_NAMES[due.weekday];
  return (
    <ExtModal onClose={onClose}>
      <div className="flex flex-col gap-4 p-5 text-13">
        <h3 className="text-16 font-medium text-primary">Recurring</h3>

        <div className="grid grid-cols-[9rem_1fr] items-center gap-3">
          <span className="text-secondary">Repeat</span>
          <select className={field} value={form.preset} onChange={(e) => set("preset", e.target.value as Preset)}>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="biweekly">Bi-weekly</option>
            <option value="semimonthly">Semi-monthly (1st &amp; 15th)</option>
            <option value="monthly">Monthly</option>
            <option value="quarterly">Quarterly</option>
            <option value="yearly">
              Yearly ({MONTHS[due.month - 1]} {due.day})
            </option>
            <option value="days_after">Days after completion…</option>
            <option value="custom">Custom…</option>
          </select>

          {form.preset === "custom" && (
            <>
              <span className="text-secondary">Every</span>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={1}
                  max={36}
                  className={`${field} w-20`}
                  value={form.interval}
                  onChange={(e) => set("interval", Math.max(1, Number(e.target.value)))}
                />
                <select className={field} value={form.unit} onChange={(e) => set("unit", e.target.value as Unit)}>
                  <option value="daily">days</option>
                  <option value="weekly">weeks</option>
                  <option value="monthly">months</option>
                  <option value="yearly">years</option>
                </select>
              </div>
            </>
          )}

          {isDaily && (
            <>
              <span className="text-secondary">On these days</span>
              <DayPicker value={form.dailyDays} onChange={(v) => set("dailyDays", v)} />
            </>
          )}

          {isWeekly && (
            <>
              <span className="text-secondary">On</span>
              <DayPicker value={form.weekdays} onChange={(v) => set("weekdays", v)} />
            </>
          )}

          {isMonthly && (
            <>
              <span className="text-secondary">On</span>
              <div className="flex items-center gap-2">
                <select
                  className={field}
                  value={form.monthMode}
                  onChange={(e) => set("monthMode", e.target.value as MonthMode)}
                >
                  <option value="same_day">Same day each month (day {due.day})</option>
                  {due.nth <= 5 && (
                    <option value="nth_weekday">
                      {ORDINAL[due.nth]} {dayName}
                    </option>
                  )}
                  {due.isLastWeekday && <option value="last_weekday">Last {dayName}</option>}
                  <option value="first_day">First day of the month</option>
                  <option value="last_day">Last day of the month</option>
                  <option value="business_day">Nth business day</option>
                </select>
                {form.monthMode === "business_day" && (
                  <input
                    type="number"
                    min={1}
                    max={23}
                    className={`${field} w-20`}
                    value={form.businessDay}
                    onChange={(e) => set("businessDay", Number(e.target.value))}
                  />
                )}
              </div>
            </>
          )}

          {form.preset === "days_after" && (
            <>
              <span className="text-secondary">Days after done</span>
              <input
                type="number"
                min={1}
                max={365}
                className={`${field} w-24`}
                value={form.daysAfter}
                onChange={(e) => set("daysAfter", Number(e.target.value))}
              />
            </>
          )}

          <span className="text-secondary">Create next</span>
          <div className="flex flex-wrap items-center gap-2">
            <select
              className={field}
              value={form.preset === "days_after" && form.trigger === "schedule" ? "completed" : form.trigger}
              onChange={(e) => set("trigger", e.target.value as TExtRecurrenceTrigger)}
            >
              <option value="completed">When completed</option>
              <option value="state">When it reaches a state</option>
              {form.preset !== "days_after" && <option value="schedule">On a schedule</option>}
            </select>
            {form.trigger === "state" && (
              <select
                className={field}
                value={form.triggerStateId}
                onChange={(e) => set("triggerStateId", e.target.value)}
              >
                <option value="">Choose a state</option>
                {states.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            )}
            {form.trigger === "schedule" && (
              <>
                <select
                  className={field}
                  value={form.runOn}
                  onChange={(e) => set("runOn", e.target.value as Form["runOn"])}
                >
                  <option value="due">on the due date</option>
                  <option value="start">on the next start date</option>
                </select>
                <span className="text-secondary">at</span>
                <input
                  type="time"
                  className={field}
                  value={form.runTime}
                  onChange={(e) => set("runTime", e.target.value)}
                />
              </>
            )}
          </div>

          <span className="text-secondary">New item</span>
          <div className="flex flex-col gap-2">
            <label className="flex items-center gap-2 text-secondary">
              <input type="checkbox" checked={form.createNew} onChange={(e) => set("createNew", e.target.checked)} />
              Create a new work item
              {form.createNew && (
                <button type="button" className="text-accent-primary" onClick={() => setShowFields((v) => !v)}>
                  Options
                </button>
              )}
            </label>
            {!form.createNew && (
              <span className="text-11 text-tertiary">The same item is reopened with new dates.</span>
            )}
            {form.createNew && showFields && (
              <div className="grid grid-cols-2 gap-1 rounded-md border border-subtle p-2">
                {EXT_COPY_FIELDS.map(([key, label]) => (
                  <label key={key} className="flex items-center gap-2 text-12 text-secondary">
                    <input
                      type="checkbox"
                      checked={form.copyFields.includes(key)}
                      onChange={(e) =>
                        set(
                          "copyFields",
                          e.target.checked ? [...form.copyFields, key] : form.copyFields.filter((x) => x !== key)
                        )
                      }
                    />
                    {label}
                  </label>
                ))}
              </div>
            )}
          </div>

          <span className="text-secondary">Status</span>
          <select className={field} value={form.newStateId} onChange={(e) => set("newStateId", e.target.value)}>
            <option value="">Project default state</option>
            {states.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>

          <span className="text-secondary">Ends</span>
          <div className="flex items-center gap-2">
            <select className={field} value={form.ends} onChange={(e) => set("ends", e.target.value as Form["ends"])}>
              <option value="never">Never (recur forever)</option>
              <option value="count">After</option>
              <option value="date">On</option>
            </select>
            {form.ends === "count" && (
              <>
                <input
                  type="number"
                  min={1}
                  className={`${field} w-20`}
                  value={form.endCount}
                  onChange={(e) => set("endCount", Number(e.target.value))}
                />
                <span className="text-secondary">times</span>
              </>
            )}
            {form.ends === "date" && (
              <input
                type="date"
                className={field}
                value={form.endDate}
                onChange={(e) => set("endDate", e.target.value)}
              />
            )}
          </div>

          <span />
          <label className="flex items-center gap-2 text-secondary">
            <input type="checkbox" checked={form.resync} onChange={(e) => set("resync", e.target.checked)} />
            Sync recurrence to due date (moving the due date moves the schedule)
          </label>
        </div>

        {error && <pre className="text-12 whitespace-pre-wrap text-danger-primary">{error}</pre>}

        <div className="flex items-center gap-2 border-t border-subtle pt-4">
          {rule && (
            <button type="button" className="text-danger-primary" onClick={() => void stop()}>
              Don&apos;t repeat
            </button>
          )}
          <div className="ml-auto flex gap-2">
            <Button variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => void save()} disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </Button>
          </div>
        </div>
      </div>
    </ExtModal>
  );
});
