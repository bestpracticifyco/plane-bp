# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""Team layout (flag `ext.team_layout`): everyone starts from the same familiar views.

* A new project gets Views switched on and five shared views (by function, board, team, calendar, gantt),
  owned by the project lead (else the workspace owner), so only they can save changes and everyone
  else's on-the-fly changes are dropped on reload.
* The first time a member opens a project, their Work items page takes the layout of the project's
  "Open work by function" view (or the built-in one) and the team columns, unless they have already
  changed their layout or the project has no modules (then only the columns are added).
  That happens once per person and project; afterwards their own choices stick.

Seeding happens on first open (middleware + the ext columns endpoint), not at join time: upstream adds
members and their layout rows with bulk_create, which sends no signals.
"""

import logging

from django.db import IntegrityError, transaction

from . import services

log = logging.getLogger("plane.ext.layout")

FLAG = "ext.team_layout"
FUNCTION_VIEW = "Open work by function"
TEAM_COLUMN_KEYS = ("confidence",)  # ext properties shown as columns, when bound to the project
OPEN = {"state_group__in": "backlog,unstarted,started"}  # hides completed/cancelled; never goes stale


def _props():
    from plane.db.models.view import get_default_display_properties

    return {
        **get_default_display_properties(),
        "key": True,
        "state": True,
        "priority": True,
        "assignee": True,
        "start_date": True,
        "due_date": True,
        "cycle": True,
        "modules": True,
        "sub_issue_count": True,
        "labels": False,
        "estimate": False,
        "attachment_count": False,
        "link": False,
        "created_on": False,
        "updated_on": False,
    }


def _df(**kw):
    from plane.db.models.view import get_default_display_filters

    return {
        **get_default_display_filters(),
        "sub_group_by": None,
        "calendar": {"layout": "month", "show_weekends": True},
        **kw,
    }


def by_function():
    return _df(layout="list", group_by="module", order_by="target_date", sub_issue=False, show_empty_groups=False)


def standard_views():
    """(name, description, display_filters) for the five shared views."""
    return [
        (FUNCTION_VIEW, "Open items grouped by accounting function (module), soonest due first.", by_function()),
        (
            "Board",
            "Open items by status; drag a card to move it along.",
            _df(layout="kanban", group_by="state", order_by="target_date", sub_issue=False, show_empty_groups=True),
        ),
        (
            "Team",
            "Open items by assignee, including sub-items.",
            _df(layout="list", group_by="assignees", order_by="target_date", sub_issue=True, show_empty_groups=False),
        ),
        ("Calendar", "Open items on their due dates.", _df(layout="calendar", sub_issue=False)),
        (
            "Gantt",
            "Open items on a timeline from start to due date.",
            _df(layout="gantt_chart", order_by="start_date", sub_issue=False),
        ),
    ]


def ensure_project_views(project):
    """Switch on Views and add any of the five shared views the project lacks. Returns how many were added."""
    from plane.db.models import IssueView, Project

    if not services.is_enabled(project.workspace_id, FLAG):
        return 0
    if not project.issue_views_view:
        Project.objects.filter(id=project.id).update(issue_views_view=True)
    owner_id = project.project_lead_id or project.workspace.owner_id
    have = set(IssueView.objects.filter(project=project).values_list("name", flat=True))
    added = 0
    for name, description, display_filters in standard_views():
        if name in have:
            continue
        IssueView.objects.create(
            workspace_id=project.workspace_id,
            project=project,
            name=name,
            description=description,
            filters={},
            rich_filters=dict(OPEN),
            display_filters=display_filters,
            display_properties=_props(),
            access=1,
            owned_by_id=owner_id,
        )
        added += 1
    return added


def _pristine(prop):
    """Still Plane's untouched defaults (so seeding cannot overwrite a layout the person chose)."""
    df = prop.display_filters or {}
    return (
        df.get("layout", "list") == "list"
        and not df.get("group_by")
        and df.get("order_by", "-created_at") == "-created_at"
        and not prop.rich_filters
        and not any((prop.filters or {}).values())  # legacy filters default to a dict of None values
    )


def ensure_member_layout(project_id, user):
    """Give `user` the team layout in `project_id`, once. Returns True when it seeded something."""
    from plane.db.models import IssueView, Module, Project, ProjectMember, ProjectUserProperty

    from .models import ExtColumnPreference, ExtProjectBinding
    from .views import REPEATS_COLUMN_ID

    if user is None or not user.is_authenticated:
        return False
    project = Project.objects.filter(id=project_id, archived_at__isnull=True).only("id", "workspace_id").first()
    if project is None or not services.is_enabled(project.workspace_id, FLAG):
        return False
    if ExtColumnPreference.objects.filter(user=user, project=project).exists():
        return False  # seeded before, or the person already picked their own columns
    if not ProjectMember.objects.filter(project=project, member=user, is_active=True).exists():
        return False
    try:
        with transaction.atomic():
            prop = ProjectUserProperty.objects.select_for_update().filter(project=project, user=user).first()
            if prop is None:
                prop = ProjectUserProperty.objects.create(project=project, user=user, workspace_id=project.workspace_id)
            if ExtColumnPreference.objects.filter(user=user, project=project).exists():
                return False  # a concurrent request got here first
            works_by_module = Module.objects.filter(project=project, archived_at__isnull=True).exists()
            if works_by_module and _pristine(prop):
                view = IssueView.objects.filter(project=project, name=FUNCTION_VIEW, archived_at__isnull=True).first()
                prop.display_filters = dict(view.display_filters) if view else by_function()
                prop.display_properties = dict(view.display_properties) if view else _props()
                prop.rich_filters = dict(view.rich_filters) if view else dict(OPEN)
                prop.save(update_fields=["display_filters", "display_properties", "rich_filters", "updated_at"])
            columns = [
                str(i)
                for i in ExtProjectBinding.objects.filter(
                    project=project, is_enabled=True, definition__key__in=TEAM_COLUMN_KEYS
                ).values_list("definition_id", flat=True)
            ]
            if services.is_enabled(project.workspace_id, "ext.recurrence"):
                columns.append(REPEATS_COLUMN_ID)
            ExtColumnPreference.objects.create(user=user, project=project, definition_ids=columns)
    except IntegrityError:
        return False  # lost a race on the unique (user, project) row; the winner seeded it
    return True


def ensure_member_layout_safely(project_id, user):
    """For request paths: a seeding problem must never stop a page from loading."""
    try:
        return ensure_member_layout(project_id, user)
    except Exception:  # noqa: BLE001
        log.exception("team layout seeding failed for project %s", project_id)
        return False
