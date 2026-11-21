# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""ext.team_layout: shared views on new projects; members start from the team layout, once."""

import pytest
from rest_framework.test import APIClient

from plane.db.models import IssueView, Module, Project, ProjectMember, ProjectUserProperty
from plane.ext import layout, services
from plane.ext.models import ExtColumnPreference, ExtProjectBinding, ExtPropertyDefinition
from plane.ext.views import REPEATS_COLUMN_ID

VIEW_NAMES = ["Open work by function", "Board", "Team", "Calendar", "Gantt"]


@pytest.fixture
def team_on(ws):
    services.set_flags(ws.id, {"ext.team_layout": True, "ext.recurrence": True})


@pytest.fixture(autouse=True)
def gl_module(project):
    return Module.objects.create(name="General Ledger", project=project, workspace=project.workspace)


@pytest.fixture
def confidence(ws, project):
    d = ExtPropertyDefinition.objects.create(workspace=ws, key="confidence", name="Confidence Level", type="dropdown")
    ExtProjectBinding.objects.create(project=project, definition=d)
    return d


def _new_project(ws, people, captured, **kw):
    with captured(execute=True):
        return Project.objects.create(name="Global Advertising", identifier="GLOBALAD", workspace=ws, **kw)


def test_new_project_gets_views_owned_by_lead(ws, people, team_on, django_capture_on_commit_callbacks):
    p = _new_project(ws, people, django_capture_on_commit_callbacks, project_lead=people["member"])
    p.refresh_from_db()
    views = IssueView.objects.filter(project=p).order_by("created_at")
    assert p.issue_views_view is True
    assert [v.name for v in views] == VIEW_NAMES
    assert {v.owned_by_id for v in views} == {people["member"].id} and {v.access for v in views} == {1}
    by_function = views[0]
    assert (
        by_function.display_filters["group_by"] == "module"
        and by_function.display_filters["show_empty_groups"] is False
    )
    assert by_function.rich_filters == layout.OPEN


def test_new_project_without_lead_views_owned_by_workspace_owner(
    ws, people, team_on, django_capture_on_commit_callbacks
):
    p = _new_project(ws, people, django_capture_on_commit_callbacks)
    assert set(IssueView.objects.filter(project=p).values_list("owned_by_id", flat=True)) == {ws.owner_id}


def test_flag_off_leaves_new_projects_alone(ws, people, django_capture_on_commit_callbacks):
    p = _new_project(ws, people, django_capture_on_commit_callbacks)
    p.refresh_from_db()
    assert p.issue_views_view is False and not IssueView.objects.filter(project=p).exists()


def test_views_are_not_duplicated(project, team_on):
    assert layout.ensure_project_views(project) == 5
    assert layout.ensure_project_views(project) == 0


def test_member_gets_team_layout_once(project, people, team_on, confidence):
    ProjectUserProperty.objects.filter(project=project).delete()  # as upstream's bulk add leaves it: no signals
    ProjectUserProperty.objects.bulk_create(
        [ProjectUserProperty(project=project, user=people["member"], workspace=project.workspace)]
    )
    assert layout.ensure_member_layout(project.id, people["member"]) is True
    prop = ProjectUserProperty.objects.get(project=project, user=people["member"])
    assert prop.display_filters["group_by"] == "module" and prop.display_filters["sub_issue"] is False
    assert prop.rich_filters == layout.OPEN
    cols = ExtColumnPreference.objects.get(project=project, user=people["member"]).definition_ids
    assert [str(c) for c in cols] == [str(confidence.id), REPEATS_COLUMN_ID]
    # the person regroups by state and clears the filter: a later open must not undo it
    prop.display_filters = {**prop.display_filters, "group_by": None}
    prop.rich_filters = {}
    prop.save()
    assert layout.ensure_member_layout(project.id, people["member"]) is False
    prop.refresh_from_db()
    assert prop.display_filters["group_by"] is None and prop.rich_filters == {}


def test_layout_follows_the_projects_function_view(project, people, team_on):
    layout.ensure_project_views(project)
    view = IssueView.objects.get(project=project, name=layout.FUNCTION_VIEW)
    view.display_filters = {**view.display_filters, "order_by": "-priority"}
    view.save()
    layout.ensure_member_layout(project.id, people["guest"])
    assert (
        ProjectUserProperty.objects.get(project=project, user=people["guest"]).display_filters["order_by"]
        == "-priority"
    )


def test_existing_layout_is_kept_but_columns_added(project, people, team_on, confidence):
    prop = ProjectUserProperty.objects.get(project=project, user=people["member"])
    prop.display_filters = {**prop.display_filters, "layout": "kanban", "group_by": "state"}
    prop.save()
    assert layout.ensure_member_layout(project.id, people["member"]) is True
    prop.refresh_from_db()
    assert prop.display_filters["group_by"] == "state"
    assert ExtColumnPreference.objects.filter(project=project, user=people["member"]).exists()


def test_not_for_non_members_or_flag_off(ws, project, people, team_on):
    assert layout.ensure_member_layout(project.id, people["outsider"]) is False
    services.set_flags(ws.id, {"ext.team_layout": False})
    assert layout.ensure_member_layout(project.id, people["member"]) is False
    assert not ExtColumnPreference.objects.exists()


def test_inactive_member_is_skipped(project, people, team_on):
    ProjectMember.objects.filter(project=project, member=people["member"]).update(is_active=False)
    assert layout.ensure_member_layout(project.id, people["member"]) is False


def test_first_open_seeds_through_user_properties(ws, project, people, team_on):
    c = APIClient()
    c.force_login(people["member"])
    r = c.get(f"/api/workspaces/{ws.slug}/projects/{project.id}/user-properties/")
    assert r.status_code == 200
    assert r.data["display_filters"]["group_by"] == "module"


def test_first_open_seeds_through_ext_columns(ws, project, people, team_on, confidence, client_for, base):
    r = client_for(people["member"]).get(f"{base}/projects/{project.id}/columns/")
    assert r.status_code == 200 and r.data["definition_ids"] == [str(confidence.id), REPEATS_COLUMN_ID]
    prop = ProjectUserProperty.objects.get(project=project, user=people["member"])
    assert prop.display_filters["group_by"] == "module"


def test_seeding_errors_never_break_the_page(ws, project, people, team_on, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(layout, "ensure_member_layout", boom)
    c = APIClient()
    c.force_login(people["member"])
    assert c.get(f"/api/workspaces/{ws.slug}/projects/{project.id}/user-properties/").status_code == 200


def test_project_without_modules_keeps_plain_layout_but_gets_columns(project, people, team_on, gl_module):
    gl_module.archived_at = gl_module.created_at
    gl_module.save()
    assert layout.ensure_member_layout(project.id, people["member"]) is True
    assert ProjectUserProperty.objects.get(project=project, user=people["member"]).display_filters["group_by"] is None
    assert ExtColumnPreference.objects.filter(project=project, user=people["member"]).exists()
