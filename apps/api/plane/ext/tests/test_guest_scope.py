# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""ext.access_hardening: a Guest limited to their own items cannot reach anyone else's (GuestScopeMiddleware),
and project roles follow upstream's workspace-role rule on every path."""

import datetime as dt

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


# -- bp.10: search, project summaries, files, cross-project ids, notifications -----------------------
@pytest.fixture
def pending(project, people):
    """Someone else's Intake request, still waiting (status -2), and the Guest's own."""
    from plane.db.models import Intake, IntakeIssue

    intake, _ = Intake.objects.get_or_create(name="Intake", project=project, workspace=project.workspace)
    out = {}
    for who, name in (("member", "Sick leave Nov 4"), ("guest", "Vacation Dec 1")):
        i = Issue.objects.create(name=name, project=project, workspace=project.workspace)
        Issue.objects.filter(id=i.id).update(created_by=people[who])
        IntakeIssue.objects.create(intake=intake, issue=i, project=project, workspace=project.workspace, status=-2)
        out[who] = i
    return out


def test_global_search_hides_others_items_and_requests(ws, people, project, theirs, mine, pending, on):
    url = f"/api/workspaces/{ws.slug}/search/?search=&entities=issue,intake"
    results = _login(people["guest"]).get(url).json()["results"]
    seen = {r["id"] for key in ("issue", "intake") for r in results[key]}
    assert str(theirs.id) not in seen and str(pending["member"].id) not in seen
    assert str(mine.id) in seen and str(pending["guest"].id) in seen
    member = _login(people["member"]).get(url).json()["results"]
    assert str(pending["guest"].id) in {r["id"] for r in member["intake"]}


def test_mention_search_hides_others_items(ws, people, project, theirs, mine, on):
    url = f"/api/workspaces/{ws.slug}/entity-search/?query=&query_type=issue&project_id={project.id}&count=50"
    ids = {r["id"] for r in _login(people["guest"]).get(url).json()["issue"]}
    assert str(theirs.id) not in ids and str(mine.id) in ids


def test_private_page_of_another_user_left_out_of_search(ws, people, project, on):
    from plane.db.models import Page, ProjectPage

    page = Page.objects.create(name="Salary review notes", workspace=ws, owned_by=people["admin"], access=1)
    ProjectPage.objects.create(page=page, project=project, workspace=ws)
    url = f"/api/workspaces/{ws.slug}/search/?search=Salary&entities=page"
    assert _login(people["member"]).get(url).json()["results"]["page"] == []
    assert len(_login(people["admin"]).get(url).json()["results"]["page"]) == 1


def test_project_summaries_closed_to_guests(ws, people, project, issue, on):
    from plane.db.models import Cycle, Module

    cycle = Cycle.objects.create(name="Nov", project=project, workspace=ws, owned_by=people["admin"])
    Module.objects.create(name="Time Off", project=project, workspace=ws)
    p = f"/api/workspaces/{ws.slug}/projects/{project.id}"
    guest, member = _login(people["guest"]), _login(people["member"])
    for listing in ("cycles/", "modules/", "pages/", "deleted-issues/"):
        r = guest.get(f"{p}/{listing}")
        assert r.status_code == 200 and r.json() == [], listing
    for path in (f"cycles/{cycle.id}/progress/", f"cycles/{cycle.id}/analytics/", "advance-analytics-charts/"):
        assert guest.get(f"{p}/{path}").status_code == 403, path
    assert len(member.get(f"{p}/modules/").json()) == 1
    ws_cycles = guest.get(f"/api/workspaces/{ws.slug}/cycles/")
    assert ws_cycles.status_code != 200 or all(c.get("project_id") != str(project.id) for c in ws_cycles.json())


def test_item_of_another_project_is_not_found(ws, people, project, issue, on):
    other = Project.objects.create(name="Other", identifier="OTHER", workspace=ws)
    ProjectMember.objects.create(project=other, workspace=ws, member=people["member"], role=15)
    url = f"/api/workspaces/{ws.slug}/projects/{other.id}/issues/{issue.id}/history/"
    ProjectMember.objects.filter(project=project, member=people["member"]).update(is_active=False)
    assert _login(people["member"]).get(url).status_code == 404


def test_project_files_only_for_their_owners(ws, people, project, theirs, mine, on):
    from plane.db.models import FileAsset

    def asset(issue, by):
        return FileAsset.objects.create(
            attributes={"name": "x.png"}, asset=f"{ws.id}/x.png", workspace=ws, project=project, issue=issue,
            entity_type="ISSUE_ATTACHMENT", created_by=by, is_uploaded=True,
        )

    other_file, own_file = asset(theirs, people["admin"]), asset(mine, people["guest"])
    dl = f"/api/assets/v2/workspaces/{ws.slug}/download/{{}}/"
    guest = _login(people["guest"])
    assert guest.get(dl.format(other_file.id)).status_code == 403
    assert guest.get(dl.format(own_file.id)).status_code != 403
    assert _login(people["member"]).get(dl.format(other_file.id)).status_code != 403
    ProjectMember.objects.filter(project=project, member=people["member"]).update(is_active=False)
    assert _login(people["member"]).get(dl.format(other_file.id)).status_code == 403  # not in the project


def test_ext_values_and_api_key_follow_guest_scope(ws, people, project, theirs, mine, on):
    from plane.ext.models import ExtProjectBinding, ExtPropertyDefinition, ExtPropertyValue

    d = ExtPropertyDefinition.objects.create(workspace=ws, name="Hours", key="hours", type="text")
    ExtProjectBinding.objects.create(project=project, definition=d)
    for i in (theirs, mine):
        ExtPropertyValue.objects.create(definition=d, issue=i, value_text="8")
    base = f"/api/ext/v1/workspaces/{ws.slug}/projects/{project.id}"
    with_session = _login(people["guest"]).get(f"{base}/values/")
    assert with_session.status_code == 200
    assert str(theirs.id) not in with_session.json() and str(mine.id) in with_session.json()
    token = APIToken.objects.create(user=people["guest"], workspace=ws, label="t")
    r = Client().get(f"{base}/work-items/{theirs.id}/values/", HTTP_X_API_KEY=token.token)
    assert r.status_code == 403


def test_no_subscriptions_notifications_or_emails_about_others_items(ws, people, project, theirs, mine, on):
    from plane.db.models import EmailNotificationLog, IssueSubscriber, Notification
    from plane.ext import guest_scope

    w = dict(project=project, workspace=ws)
    IssueSubscriber.objects.create(issue=theirs, subscriber=people["guest"], **w)  # an @mention, one by one
    assert not IssueSubscriber.objects.filter(issue=theirs, subscriber=people["guest"]).exists()
    # upstream's tasks use bulk_create (no signals): the task boundary clean-up removes them
    IssueSubscriber.objects.bulk_create([IssueSubscriber(issue=theirs, subscriber=people["guest"], **w)])
    IssueSubscriber.objects.bulk_create([IssueSubscriber(issue=mine, subscriber=people["guest"], **w)])
    Notification.objects.bulk_create(
        [
            Notification(workspace=ws, project=project, entity_identifier=i.id, entity_name="issue", title="t",
                         sender="x", receiver=people["guest"])
            for i in (theirs, mine)
        ]
    )
    EmailNotificationLog.objects.bulk_create(
        [
            EmailNotificationLog(receiver=people["guest"], triggered_by=people["admin"], entity_identifier=i.id,
                                 entity_name="issue", entity="issue")
            for i in (theirs, mine)
        ]
    )
    assert guest_scope.purge_pending_emails() == 1
    assert guest_scope.purge_issue(theirs.id) == 2
    assert guest_scope.purge_issue(mine.id) == 0
    subs = IssueSubscriber.objects.filter(subscriber=people["guest"]).values_list("issue_id", flat=True)
    notes = Notification.objects.filter(receiver=people["guest"]).values_list("entity_identifier", flat=True)
    assert list(subs) == [mine.id] and list(notes) == [mine.id]
    assert EmailNotificationLog.objects.filter(receiver=people["guest"]).count() == 1


def test_recurrence_overviews_follow_guest_scope(ws, people, project, theirs, mine, on):
    from plane.ext.models import ExtRecurrenceRule

    services.set_flags(ws.id, {"ext.recurrence": True})
    for i in (theirs, mine):
        ExtRecurrenceRule.objects.create(
            workspace=ws, project=project, current_issue=i, schedule={"freq": "monthly", "mode": "same_day", "day": 1},
            anchor=dt.date(2026, 12, 1), created_by=people["admin"],
        )
    base = f"/api/ext/v1/workspaces/{ws.slug}"
    guest = _login(people["guest"])
    assert set(guest.get(f"{base}/projects/{project.id}/recurrence/").json()) == {str(mine.id)}
    assert {r["issue_id"] for r in guest.get(f"{base}/recurrence/").json()} <= {str(mine.id)}
