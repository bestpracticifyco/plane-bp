/**
 * SPDX-License-Identifier: AGPL-3.0-only
 * Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
 */

import { useMemo, useState } from "react";
import { observer } from "mobx-react";
import { useParams } from "next/navigation";
import { Button } from "@plane/propel/button";
import { DueDatePropertyIcon, StartDatePropertyIcon } from "@plane/propel/icons";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import type { TBulkIssueProperties, TBulkOperationsPayload, TIssue, TIssuePriorities } from "@plane/types";
import { cn, renderFormattedPayloadDate } from "@plane/utils";
import { CycleDropdown } from "@/components/dropdowns/cycle";
import { DateDropdown } from "@/components/dropdowns/date";
import { MemberDropdown } from "@/components/dropdowns/member/dropdown";
import { ModuleDropdown } from "@/components/dropdowns/module/dropdown";
import { PriorityDropdown } from "@/components/dropdowns/priority";
import { StateDropdown } from "@/components/dropdowns/state/dropdown";
import { IssuePropertyLabels } from "@/components/issues/issue-layouts/properties/labels";
import { useProject } from "@/hooks/store/use-project";
import { useIssues } from "@/hooks/store/use-issues";
import type { TSelectionHelper } from "@/hooks/use-multiple-select";
import { useIssueStoreType } from "@/hooks/use-issue-layout-store";

/** The bulk methods every CE issue store exposes (some stores implement them as no-ops). */
type TBulkStore = {
  bulkUpdateProperties?: (ws: string, projectId: string, data: TBulkOperationsPayload) => Promise<void>;
  archiveBulkIssues?: (ws: string, projectId: string, issueIds: string[]) => Promise<void>;
  removeBulkIssues?: (ws: string, projectId: string, issueIds: string[]) => Promise<void>;
};

type TRefused = { sequence_id: number; name: string; errors: string[] };
type TBulkError = { error?: string; refused?: TRefused[] } | undefined;

function describeError(err: unknown, prefix: string): string {
  const e = err as TBulkError;
  const refused = e?.refused ?? [];
  const names = refused
    .slice(0, 3)
    .map((r) => `#${r.sequence_id} ${r.name}: ${r.errors[0] ?? ""}`)
    .join("; ");
  const more = refused.length > 3 ? ` (+${refused.length - 3} more)` : "";
  return [e?.error ?? prefix, names && `${names}${more}`].filter(Boolean).join(" ");
}

type Props = {
  className?: string;
  selectionHelpers: TSelectionHelper;
  selectedIds: string[];
};

/**
 * Bottom bar shown while work items are selected (List, Spreadsheet, Gantt). Each control applies at once
 * to every selected item. Status, assignees, labels, module and cycle belong to one project, so they show
 * only when the selection is inside one project; priority, dates and archive work across projects.
 * Arrays (assignees, labels, modules) are added to what each item already has.
 */
export const ExtBulkBar = observer(function ExtBulkBar(props: Props) {
  const { className, selectionHelpers, selectedIds } = props;
  const params = useParams();
  const ws = params.workspaceSlug?.toString() ?? "";
  const storeType = useIssueStoreType();
  const { issues, issueMap } = useIssues(storeType) as unknown as {
    issues: TBulkStore;
    issueMap: Record<string, TIssue | undefined>;
  };
  const { getProjectById } = useProject();
  const [busy, setBusy] = useState(false);

  const byProject = useMemo(() => {
    const out: Record<string, string[]> = {};
    for (const id of selectedIds) {
      const pid = issueMap[id]?.project_id;
      if (pid) (out[pid] ??= []).push(id);
    }
    return out;
  }, [selectedIds, issueMap]);
  const projectIds = Object.keys(byProject);
  const single = projectIds.length === 1 ? projectIds[0] : null;
  const project = single ? getProjectById(single) : undefined;
  const count = selectedIds.length;

  const run = async (label: string, fn: (projectId: string, ids: string[]) => Promise<void> | undefined) => {
    if (busy || !count) return;
    setBusy(true);
    try {
      // one project at a time: stop at the first project that refuses (each call is all-or-nothing)
      // oxlint-disable-next-line no-await-in-loop
      for (const pid of projectIds) await fn(pid, byProject[pid]);
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: "Done",
        message: `${label}: ${count} work item${count > 1 ? "s" : ""}.`,
      });
      return true;
    } catch (err) {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: `${label} failed`,
        message: describeError(err, "Nothing was changed."),
      });
      return false;
    } finally {
      setBusy(false);
    }
  };

  const update = (label: string, properties: Partial<TBulkIssueProperties>) =>
    run(label, (pid, ids) => issues.bulkUpdateProperties?.(ws, pid, { issue_ids: ids, properties }));

  const archive = async () => {
    if (await run("Archived", (pid, ids) => issues.archiveBulkIssues?.(ws, pid, ids)))
      selectionHelpers.handleClearSelection();
  };

  const remove = async () => {
    if (!window.confirm(`Delete ${count} work item${count > 1 ? "s" : ""}? This can't be undone.`)) return;
    if (await run("Deleted", (pid, ids) => issues.removeBulkIssues?.(ws, pid, ids)))
      selectionHelpers.handleClearSelection();
  };

  const date = (d: Date | null) => (d ? (renderFormattedPayloadDate(d) ?? null) : null);

  return (
    <div className={cn("sticky bottom-0 left-0 z-[2] px-3.5 py-2", className)}>
      <div
        className={cn(
          "flex w-full flex-wrap items-center gap-2 rounded-md border-[0.5px] border-subtle-1 bg-layer-1 px-3 py-2 shadow-raised-200",
          busy && "pointer-events-none opacity-60"
        )}
      >
        <span className="text-body-xs-medium text-primary">{count} selected</span>
        <Button variant="ghost" size="sm" onClick={selectionHelpers.handleClearSelection}>
          Clear
        </Button>
        <span className="mx-1 h-4 w-px bg-layer-3" />
        {single && (
          <StateDropdown
            value={undefined}
            projectId={single}
            onChange={(stateId) => void update("Status changed", { state_id: stateId })}
            buttonVariant="border-with-text"
            placeholder="Status"
          />
        )}
        <PriorityDropdown
          value={undefined}
          onChange={(p: TIssuePriorities) => void update("Priority changed", { priority: p })}
          buttonVariant="border-with-text"
          placeholder="Priority"
        />
        {single && (
          <MemberDropdown
            projectId={single}
            value={[]}
            multiple
            onChange={(ids: string[]) => ids.length && void update("Assignee added", { assignee_ids: ids })}
            buttonVariant="border-with-text"
            placeholder="Add assignee"
          />
        )}
        {single && (
          <IssuePropertyLabels
            projectId={single}
            value={[]}
            onChange={(ids) => ids.length && void update("Label added", { label_ids: ids })}
            placeholderText="Add label"
          />
        )}
        {single && project?.module_view && (
          <ModuleDropdown
            projectId={single}
            value={[]}
            multiple
            onChange={(ids: string[]) => ids.length && void update("Module added", { module_ids: ids })}
            buttonVariant="border-with-text"
            placeholder="Add to module"
          />
        )}
        {single && project?.cycle_view && (
          <CycleDropdown
            projectId={single}
            value={null}
            onChange={(cycleId) => cycleId && void update("Cycle changed", { cycle_id: cycleId })}
            buttonVariant="border-with-text"
            placeholder="Cycle"
          />
        )}
        <DateDropdown
          value={null}
          onChange={(d) => void update("Start date set", { start_date: date(d) })}
          placeholder="Start date"
          icon={<StartDatePropertyIcon className="h-3 w-3 flex-shrink-0" />}
          buttonVariant="border-with-text"
        />
        <DateDropdown
          value={null}
          onChange={(d) => void update("Due date set", { target_date: date(d) })}
          placeholder="Due date"
          icon={<DueDatePropertyIcon className="h-3 w-3 shrink-0" />}
          buttonVariant="border-with-text"
        />
        <span className="ml-auto flex items-center gap-2">
          <Button variant="secondary" size="base" onClick={() => void archive()}>
            Archive
          </Button>
          <Button variant="error-outline" size="base" onClick={() => void remove()}>
            Delete
          </Button>
        </span>
      </div>
    </div>
  );
});
