/**
 * SPDX-License-Identifier: AGPL-3.0-only
 * Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
 */

export type TExtPropertyType = "dropdown" | "text" | "date" | "member" | "datetime";

export type TExtOption = { id: string; name: string; color: string; sort_order: number; is_archived: boolean };

export type TExtDefinition = {
  id: string;
  key: string;
  name: string;
  description: string;
  help_text: string;
  type: TExtPropertyType;
  is_multi: boolean;
  is_required: boolean;
  default_value: unknown;
  sort_order: number;
  is_archived: boolean;
  options: TExtOption[];
};

export type TExtBinding = {
  definition_id: string;
  key: string;
  sort_order: number;
  is_enabled: boolean;
  show_on_card: boolean;
};

/** A stored value: option/member id(s), ISO date/date-time, or text. */
export type TExtValue = string | string[] | null;

/** issueId -> definitionId -> value */
export type TExtValueMap = Record<string, Record<string, TExtValue>>;

export type TExtFlags = Record<string, boolean>;

/** Access matrix (workspace Admin only). Roles: 20 Admin, 15 Member, 5 Guest; null = not in project. */
export type TExtRole = 20 | 15 | 5;
export type TExtAccessProject = {
  id: string;
  identifier: string;
  name: string;
  is_private: boolean;
  is_archived: boolean;
};
export type TExtAccessPerson = { id: string; email: string; display_name: string; workspace_role: TExtRole };
export type TExtAccessGrant = { email: string; project_id: string; role: TExtRole };
export type TExtAccessMatrix = {
  projects: TExtAccessProject[];
  people: TExtAccessPerson[];
  /** memberId -> projectId -> role */
  cells: Record<string, Record<string, TExtRole>>;
  pending: TExtAccessGrant[];
};
export type TExtAccessCell = { member_id: string; project_id: string; role: TExtRole | null };

/** Recurring work items (plane_ext.recurrence). Weekdays: 0 = Monday .. 6 = Sunday. */
export type TExtSchedule =
  | { freq: "daily"; interval?: number; weekdays?: number[]; skip_weekends?: boolean }
  | { freq: "weekly"; interval?: number; weekdays: number[] }
  | {
      freq: "monthly";
      interval?: number;
      mode: "same_day" | "nth_weekday" | "first_day" | "last_day" | "business_day" | "days";
      day?: number;
      nth?: number;
      weekday?: number;
      days?: number[];
      /** each instance starts on the 1st of its due month */
      start?: "month_first";
    }
  | { freq: "yearly"; interval?: number; month: number; day: number }
  | { freq: "days_after"; days: number };

export type TExtRecurrenceTrigger = "completed" | "state" | "schedule";

export const EXT_COPY_FIELDS = [
  ["description", "Description"],
  ["assignees", "Assignees"],
  ["labels", "Labels"],
  ["modules", "Modules"],
  ["cycle", "Cycle (by date)"],
  ["priority", "Priority"],
  ["estimate", "Estimate"],
  ["properties", "Custom properties"],
  ["sub_items", "Sub-work-items"],
  ["sub_item_assignees", "Sub-work-item assignees"],
  ["remap_sub_item_dates", "Remap sub-work-item dates"],
  ["links", "Links"],
  ["subscribers", "Subscribers"],
  ["parent", "Parent"],
] as const;
export type TExtCopyField = (typeof EXT_COPY_FIELDS)[number][0];

export type TExtRecurrenceRule = {
  id: string;
  project_id: string;
  project_identifier?: string;
  issue_id: string | null;
  issue_name: string | null;
  issue_sequence_id: number | null;
  due: string | null;
  schedule: TExtSchedule;
  summary: string;
  anchor: string;
  trigger: TExtRecurrenceTrigger;
  trigger_state_id: string | null;
  create_new: boolean;
  new_state_id: string | null;
  copy_fields: TExtCopyField[];
  end_count: number | null;
  end_date: string | null;
  occurrences: number;
  resync_on_due_change: boolean;
  run_time: string;
  /** schedule trigger: create on the current item's due date, or on the next item's start date */
  run_on: "due" | "start";
  next_run_at: string | null;
  next_due: string | null;
  paused: boolean;
  last_spawned_at: string | null;
};

export type TExtRecurrenceInput = Partial<
  Pick<
    TExtRecurrenceRule,
    | "schedule"
    | "trigger"
    | "trigger_state_id"
    | "create_new"
    | "new_state_id"
    | "copy_fields"
    | "end_count"
    | "end_date"
    | "resync_on_due_change"
    | "run_time"
    | "run_on"
  >
>;

/** Column-preference id of the "Repeats" pseudo-column (server: views.REPEATS_COLUMN_ID). */
export const EXT_REPEATS_COLUMN_ID = "00000000-0000-4000-8000-000000000001";

// -- Attendance (flag ext.attendance) ------------------------------------------------------------
export type TExtAttendanceStatus =
  | "present"
  | "late"
  | "excused_late"
  | "absent"
  | "holiday"
  | "time_off"
  | "vacation"
  | "sick_leave"
  | "unplanned_leave";

export type TExtAttendanceDay = {
  date: string;
  status: TExtAttendanceStatus | null;
  clock_in: string | null;
  clock_out: string | null;
  hours: number | null;
  issue_id: string;
  project_id: string;
  identifier: string;
  name: string;
};

export type TExtAttendanceToday = {
  date: string;
  kind: "work" | "off" | "holiday";
  holiday: string;
  shift_start: string | null;
  late_after: string | null;
  day: TExtAttendanceDay | null;
  /** last night's shift, still open and recent enough to clock out of */
  open_yesterday: TExtAttendanceDay | null;
  now: string;
};

export type TExtAttendanceSummary = {
  counts: Record<TExtAttendanceStatus, number>;
  working_days: number;
  hours: number;
  missing_clock_out: number;
};

export type TExtAttendanceMe = {
  timezone: string;
  project_id: string;
  month: string;
  today: TExtAttendanceToday;
  days: TExtAttendanceDay[];
  summary: TExtAttendanceSummary;
};

export type TExtAttendancePerson = {
  member_id: string;
  name: string;
  full_name: string;
  project_id: string;
  project_identifier: string;
  today: TExtAttendanceToday;
  days: TExtAttendanceDay[];
  summary: TExtAttendanceSummary;
};

export type TExtAttendanceTeam = { timezone: string; month: string; people: TExtAttendancePerson[] };

export type TExtAttendanceSchedule = {
  id?: string;
  member_id: string | null;
  effective_from: string;
  shift_start: string;
  grace_minutes: number;
  workdays: number[];
  saturdays: number[];
};

export type TExtAttendanceSettings = {
  /** admin: everything; hr (an Admin of the HR project): shift schedule, holidays and the people they manage */
  access: "admin" | "hr";
  directory: {
    members: { id: string; display_name: string; email: string; workspace_role: number }[];
    projects: { id: string; name: string; identifier: string }[];
  };
  configured: boolean;
  timezone: string;
  recorder_id: string | null;
  time_off_project_id: string | null;
  mark_absent: boolean;
  mark_absent_from: string | null;
  schedules: TExtAttendanceSchedule[];
  holidays: { date: string; name: string }[];
  /** mark_absent off: clocking in is optional for that person (missed days stay blank) */
  people: {
    member_id: string;
    project_id: string;
    is_active: boolean;
    mark_absent: boolean;
    /** false: an HR manager's own row, or a Staff file they don't manage (shown, not changeable) */
    editable?: boolean;
    project_name?: string;
  }[];
};
