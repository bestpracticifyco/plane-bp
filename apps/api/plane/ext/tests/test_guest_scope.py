# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""ext.access_hardening: a Guest limited to their own items cannot reach anyone else's (GuestScopeMiddleware),
and project roles follow upstream's workspace-role rule on every path."""

import pytest
from django.test import Client
from rest_framework.exceptions import ValidationError

from plane.db.models import APIToken, Issue, Project, ProjectMember, State
from plane.ext import services

pytestmark = pytest.mark.django_db


@pytest.fixture
def on(ws):
    services.set_flags(ws.id, {"ext.access_hardening": True})


@pytest.fixture
def theirs(issue, people):
    Issue.objects.filter(id=issue.id).update(created_by=people["admin"])
    return issue


@pytest.fixture
def mine(project, states, people):
    i = Issue.objects.create(
        name="Laptop will not boot", project=project, workspace=project.workspace, state=states["backlog"]
    )
    Issue.objects.filter(id=i.id).update(created_by=people["guest"])
    return i


def _login(user):
    c = Client()
    c.force_login(user)
    return c


def _item(ws, project, issue, part):
    return f"/api/workspaces/{ws.slug}/projects/{project.id}/issues/{issue.id}/{part}"


@pytest.mark.parametrize("part", ["comments/", "history/", "sub-issues/", "issue-links/", "issue-subscribers/"])
def test_guest_cannot_open_someone_elses_item_by_id(ws, people, project, theirs, mine, on, part):
    guest = _login(people["guest"])
    assert guest.get(_item(ws, project, theirs, part)).status_code == 403
    assert guest.get(_item(ws, project, mine, part)).status_code != 403
    assert _login(people["member"]).get(_item(ws, project, theirs, part)).status_code != 403


def test_flag_off_or_guest_sees_all_leaves_upstream_alone(ws, people, project, theirs):
    url = _item(ws, project, theirs, "comments/")
    assert _login(people["guest"]).get(url).status_code != 403  # flag off
    services.set_flags(ws.id, {"ext.access_hardening": True})
    Project.objects.filter(id=project.id).update(guest_view_all_features=True)
    assert _login(people["guest"]).get(url).status_code != 403


def test_ext_values_of_someone_elses_item(ws, people, project, theirs, mine, on):
    base = f"/api/ext/v1/workspaces/{ws.slug}/projects/{project.id}/work-items"
    guest = _login(people["guest"])
    assert guest.get(f"{base}/{theirs.id}/values/").status_code == 403
    assert guest.get(f"{base}/{mine.id}/values/").status_code != 403


def test_guest_cannot_edit_states(ws, people, project, states, on):
    url = f"/api/workspaces/{ws.slug}/projects/{project.id}/states/{states['backlog'].id}/"
    r = _login(people["guest"]).patch(url, {"name": "Hacked"}, content_type="application/json")
    assert r.status_code == 403
    assert State.objects.get(id=states["backlog"].id).name == "Backlog"
    assert _login(people["guest"]).get(f"/api/workspaces/{ws.slug}/projects/{project.id}/states/").status_code == 200


def test_guest_cannot_browse_colleagues_profiles(ws, people, project, on):
    guest = _login(people["guest"])
    for kind in ("issues", "activity", "stats", "profile"):
        assert guest.get(f"/api/workspaces/{ws.slug}/user-{kind}/{people['member'].id}/").status_code == 403
    assert guest.get(f"/api/workspaces/{ws.slug}/user-issues/{people['guest'].id}/").status_code != 403
    member = _login(people["member"])
    assert member.get(f"/api/workspaces/{ws.slug}/user-issues/{people['guest'].id}/").status_code != 403


def test_public_api_is_closed_to_restricted_guests(ws, people, project, theirs, on):
    def get(user, path):
        token = APIToken.objects.create(user=user, workspace=ws, label="t")
        return Client().get(path, HTTP_X_API_KEY=token.token)

    listing = f"/api/v1/workspaces/{ws.slug}/projects/{project.id}/issues/"
    services.set_flags(ws.id, {"ext.access_hardening": False})
    assert get(people["guest"], listing).status_code == 200  # upstream: no Guest filter on the public API
    services.set_flags(ws.id, {"ext.access_hardening": True})
    assert get(people["guest"], listing).status_code == 403
    assert get(people["guest"], f"/api/v1/workspaces/{ws.slug}/work-items/{project.identifier}-1/").status_code == 403
    assert get(people["guest"], f"/api/v1/workspaces/{ws.slug}/work-items/search/?search=x").status_code == 403
    assert get(people["member"], listing).status_code != 403


def test_project_roles_follow_upstreams_workspace_rule(ws, people, project, on):
    member = ProjectMember.objects.get(project=project, member=people["member"])
    member.role = 20  # a workspace Member may be project Admin (to triage an Intake project)
    member.save()
    guest = ProjectMember.objects.get(project=project, member=people["guest"])
    for role in (15, 20):
        guest.role = role
        with pytest.raises(ValidationError):
            guest.save()
    admin = ProjectMember.objects.get(project=project, member=people["admin"])
    admin.role = 15  # a workspace Admin is always project Admin
    with pytest.raises(ValidationError):
        admin.save()


# -- Intake submissions as webhook events (ext.intake_events) ----------------------------------
def _submit(project, issue, people):
    from plane.db.models import Intake, IntakeIssue

    intake, _ = Intake.objects.get_or_create(name="IT Support Intake", project=project, workspace=project.workspace)
    return IntakeIssue.objects.create(intake=intake, issue=issue, project=project, workspace=project.workspace)


def test_intake_submission_sends_a_webhook_event(
    ws, people, project, mine, monkeypatch, django_capture_on_commit_callbacks
):
    from celery.app.task import Task

    sent = []  # patch the class, as test_properties' no_celery does: an instance patch outlives the test
    monkeypatch.setattr(
        Task, "delay", lambda self, *a, **kw: sent.append(kw) if self.name.endswith("model_activity") else None
    )
    with django_capture_on_commit_callbacks(execute=True):
        first = _submit(project, mine, people)
    first.delete(soft=False)
    assert sent == []  # flag off
    services.set_flags(ws.id, {"ext.intake_events": True})
    with django_capture_on_commit_callbacks(execute=True):
        ii = _submit(project, mine, people)
    assert len(sent) == 1
    assert sent[0]["model_name"] == "intake_issue" and sent[0]["model_id"] == str(ii.id)
    assert sent[0]["current_instance"] is None and sent[0]["slug"] == ws.slug
