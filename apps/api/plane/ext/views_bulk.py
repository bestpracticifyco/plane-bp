# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""Bulk edit for work items (flag ext.bulk_edit).

Community Edition ships the selection UI and the web store call (`bulkUpdateProperties` ->
POST /api/workspaces/<slug>/projects/<id>/bulk-operation-issues/) but not the endpoint. This is that
endpoint, mounted at the same path by plane.ext.urls_root, so the upstream store updates the
screen unchanged.

Payload: {"issue_ids": [...], "properties": {state_id, priority, assignee_ids, label_ids, module_ids,
cycle_id, start_date, target_date, estimate_point}}. Arrays ADD to what each item has (the store merges
the same way). Every item is saved one by one through the normal serializer, so the required-module /
required-property guards, recurrence triggers, activity and webhooks all behave as for a single edit.
All or nothing: if any item is refused, nothing changes and the response lists the refused items.
"""

import json

from crum import impersonate
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from plane.app.permissions import ROLE, allow_permission
from plane.app.serializers import IssueCreateSerializer
from plane.app.views.base import BaseAPIView
from plane.bgtasks.issue_activities_task import issue_activity
from plane.bgtasks.webhook_task import model_activity
from plane.db.models import Cycle, CycleIssue, Issue, Module, ModuleIssue
from plane.utils.host import base_host

from . import services

FLAG = "ext.bulk_edit"
MAX_ITEMS = 500
SCALARS = ("state_id", "priority", "start_date", "target_date", "estimate_point")
ARRAYS = ("assignee_ids", "label_ids")


def _messages(err):
    """Flatten a DRF / Django validation error into short strings."""
    detail = (
        getattr(err, "detail", None) or getattr(err, "message_dict", None) or getattr(err, "messages", None) or str(err)
    )
    out = []

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
        else:
            out.append(str(x))

    walk(detail)
    return out or [str(err)]


def _current(issue):
    return {
        "assignee_ids": [str(x) for x in issue.assignees.values_list("id", flat=True)],
        "label_ids": [str(x) for x in issue.labels.values_list("id", flat=True)],
    }


class BulkOperationIssuesEndpoint(BaseAPIView):
    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def post(self, request, slug, project_id):
        if not services.is_enabled_for_slug(slug, FLAG):
            raise PermissionDenied("Bulk edit is not turned on for this workspace.")
        issue_ids = [str(i) for i in (request.data.get("issue_ids") or [])]
        props = request.data.get("properties") or {}
        if not issue_ids or not isinstance(props, dict) or not props:
            return Response({"error": "issue_ids and properties are required"}, status=status.HTTP_400_BAD_REQUEST)
        if len(issue_ids) > MAX_ITEMS:
            return Response({"error": f"At most {MAX_ITEMS} work items at a time"}, status=status.HTTP_400_BAD_REQUEST)
        unknown = sorted(set(props) - set(SCALARS) - set(ARRAYS) - {"module_ids", "cycle_id"})
        if unknown:
            return Response(
                {"error": f"Unsupported properties: {', '.join(unknown)}"}, status=status.HTTP_400_BAD_REQUEST
            )

        issues = list(
            Issue.issue_objects.filter(workspace__slug=slug, project_id=project_id, pk__in=issue_ids).select_related(
                "state"
            )
        )
        if len(issues) != len(set(issue_ids)):
            return Response(
                {"error": "Some work items were not found in this project"}, status=status.HTTP_404_NOT_FOUND
            )

        module_ids = [str(m) for m in (props.get("module_ids") or [])]
        if module_ids and Module.objects.filter(
            project_id=project_id, pk__in=module_ids, archived_at__isnull=True
        ).count() != len(set(module_ids)):
            return Response({"error": "Unknown module"}, status=status.HTTP_400_BAD_REQUEST)
        cycle = None
        if props.get("cycle_id"):
            cycle = Cycle.objects.filter(project_id=project_id, pk=props["cycle_id"]).first()
            if cycle is None:
                return Response({"error": "Unknown cycle"}, status=status.HTTP_400_BAD_REQUEST)
            if cycle.end_date is not None and cycle.end_date < timezone.now():
                return Response({"error": "That cycle is completed; work items cannot be added"}, status=400)

        origin = base_host(request=request, is_app=True)
        epoch = int(timezone.now().timestamp())
        actor = str(request.user.id)
        refused, queued = [], []
        try:
            with transaction.atomic(), impersonate(request.user):  # guards read the acting user from crum
                for issue in issues:
                    errors = self._apply(issue, props, module_ids, cycle, project_id, actor, origin, epoch, queued)
                    if errors:
                        refused.append(
                            {
                                "id": str(issue.id),
                                "sequence_id": issue.sequence_id,
                                "name": issue.name,
                                "errors": errors,
                            }
                        )
                if refused:
                    raise _Refused()
        except _Refused:
            return Response(
                {
                    "error": f"{len(refused)} of {len(issues)} work items can't take this change; nothing was changed.",
                    "refused": refused,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        for task, kwargs in queued:  # after commit: the worker must find the saved rows
            task.delay(**kwargs)
        return Response({"updated": len(issues)}, status=status.HTTP_200_OK)

    def _apply(self, issue, props, module_ids, cycle, project_id, actor, origin, epoch, queued):
        """Apply the change to one item inside the batch transaction; return error strings (empty = ok)."""
        sid = transaction.savepoint()
        try:
            # Modules and cycle first: a state change into work may require a module (ext.require_module).
            have = set(str(m) for m in ModuleIssue.objects.filter(issue=issue).values_list("module_id", flat=True))
            for mid in module_ids:
                if mid in have:
                    continue
                ModuleIssue.objects.create(
                    issue=issue, module_id=mid, project_id=project_id, workspace_id=issue.workspace_id
                )
                queued.append(
                    (
                        issue_activity,
                        dict(
                            type="module.activity.created",
                            requested_data=json.dumps({"module_id": mid}),
                            actor_id=actor,
                            issue_id=str(issue.id),
                            project_id=str(project_id),
                            current_instance=None,
                            epoch=epoch,
                            notification=True,
                            origin=origin,
                        ),
                    )
                )
            if cycle is not None:
                ci = CycleIssue.objects.filter(issue=issue).first()
                if ci is None or ci.cycle_id != cycle.id:
                    created, updated = [], []
                    if ci is None:
                        created = [
                            CycleIssue.objects.create(
                                issue=issue, cycle=cycle, project_id=project_id, workspace_id=issue.workspace_id
                            )
                        ]
                    else:
                        updated = [
                            {"old_cycle_id": str(ci.cycle_id), "new_cycle_id": str(cycle.id), "issue_id": str(issue.id)}
                        ]
                        ci.cycle = cycle
                        ci.save(update_fields=["cycle", "updated_at"])
                    from django.core import serializers as dj

                    queued.append(
                        (
                            issue_activity,
                            dict(
                                type="cycle.activity.created",
                                requested_data=json.dumps({"cycles_list": [str(issue.id)]}),
                                actor_id=actor,
                                issue_id=None,
                                project_id=str(project_id),
                                current_instance=json.dumps(
                                    {
                                        "updated_cycle_issues": updated,
                                        "created_cycle_issues": dj.serialize("json", created),
                                    }
                                ),
                                epoch=epoch,
                                notification=True,
                                origin=origin,
                            ),
                        )
                    )

            data = {k: props[k] for k in SCALARS if k in props}
            if not data and not any(k in props for k in ARRAYS):
                transaction.savepoint_commit(sid)
                return []
            current = _current(issue)
            for k in ARRAYS:
                if k in props:
                    data[k] = sorted(set(current[k]) | {str(x) for x in props[k] or []})
            start = data.get("start_date", issue.start_date)
            due = data.get("target_date", issue.target_date)
            if start and due and str(start) > str(due):
                raise DRFValidationError({"start_date": ["Start date would be after the due date"]})
            before = json.dumps(
                {
                    "state_id": str(issue.state_id) if issue.state_id else None,
                    "priority": issue.priority,
                    "start_date": issue.start_date,
                    "target_date": issue.target_date,
                    "estimate_point": str(issue.estimate_point_id) if issue.estimate_point_id else None,
                    **current,
                },
                cls=DjangoJSONEncoder,
            )
            s = IssueCreateSerializer(issue, data=data, partial=True, context={"project_id": project_id})
            s.is_valid(raise_exception=True)
            s.save()
            queued.append(
                (
                    issue_activity,
                    dict(
                        type="issue.activity.updated",
                        requested_data=json.dumps(data, cls=DjangoJSONEncoder),
                        actor_id=actor,
                        issue_id=str(issue.id),
                        project_id=str(project_id),
                        current_instance=before,
                        epoch=epoch,
                        notification=True,
                        origin=origin,
                    ),
                )
            )
            queued.append(
                (
                    model_activity,
                    dict(
                        model_name="issue",
                        model_id=str(issue.id),
                        requested_data=data,
                        current_instance=before,
                        actor_id=actor,
                        slug=issue.workspace.slug,
                        origin=origin,
                    ),
                )
            )
            transaction.savepoint_commit(sid)
            return []
        except (DRFValidationError, DjangoValidationError) as e:
            transaction.savepoint_rollback(sid)
            return _messages(e)


class _Refused(Exception):
    pass
