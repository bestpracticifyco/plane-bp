# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""Bulk edit (flag ext.bulk_edit): the CE web store's bulk-operation-issues endpoint, served by plane_ext."""

import datetime as dt

import pytest

from plane.db.models import (
    Cycle,
    CycleIssue,
    Issue,
    IssueAssignee,
    IssueLabel,
    Label,
    Module,
    ModuleIssue,
    Project,
    ProjectMember,
    State,
)
from plane.ext import services

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def calls(ws, monkeypatch):
    services.set_flags(ws.id, {"ext.bulk_edit": True})
    from celery.app.task import Task

    seen = []  # patch the class: an instance patch outlives the test (see test_guest_scope)

    def delay(self, *a, **kw):
        if self.name.endswith("issue_activity"):
            seen.append(("activity", kw))
        elif self.name.endswith("model_activity"):
            seen.append(("webhook", kw))

    monkeypatch.setattr(Task, "delay", delay)
    return seen


@pytest.fixture
def items(project, states):
    return [
        Issue.objects.create(name=f"Item {n}", project=project, workspace=project.workspace, state=states["backlog"])
        for n in range(3)
    ]


def _url(ws, project):  # the exact path the CE web app's issue service calls
    return f"/api/workspaces/{ws.slug}/projects/{project.id}/bulk-operation-issues/"


def _post(client, ws, project, ids, props):
    return client.post(_url(ws, project), {"issue_ids": [str(i.id) for i in ids], "properties": props}, format="json")


def test_member_changes_status_and_priority_on_many_items(client_for, people, ws, project, states, items, calls):
    r = _post(
        client_for(people["member"]), ws, project, items, {"state_id": str(states["started"].id), "priority": "high"}
    )
    assert r.status_code == 200, r.data
    assert r.data == {"updated": 3}
    for i in items:
        i.refresh_from_db()
        assert (i.state_id, i.priority) == (states["started"].id, "high")
    kinds = [k for k, kw in calls if kw.get("type", "webhook") in ("issue.activity.updated", "webhook")]
    assert kinds.count("activity") == 3 and kinds.count("webhook") == 3


def test_assignees_and_labels_are_added_not_replaced(client_for, people, ws, project, items):
    a, b = items[0], items[1]
    IssueAssignee.objects.create(issue=a, assignee=people["admin"], project=project, workspace=ws)
    old = Label.objects.create(name="Urgent", project=project, workspace=ws)
    new = Label.objects.create(name="Client", project=project, workspace=ws)
    IssueLabel.objects.create(issue=a, label=old, project=project, workspace=ws)
    r = _post(
        client_for(people["admin"]),
        ws,
        project,
        [a, b],
        {"assignee_ids": [str(people["member"].id)], "label_ids": [str(new.id)]},
    )
    assert r.status_code == 200, r.data
    assert set(a.assignees.values_list("id", flat=True)) == {people["admin"].id, people["member"].id}
    assert set(a.labels.values_list("id", flat=True)) == {old.id, new.id}
    assert set(b.assignees.values_list("id", flat=True)) == {people["member"].id}


def test_one_refused_item_changes_nothing(client_for, people, ws, project, states, items):
    services.set_flags(ws.id, {"ext.require_module": True})
    gl = Module.objects.create(name="General Ledger", project=project, workspace=ws)
    ModuleIssue.objects.create(issue=items[0], module=gl, project=project, workspace=ws)  # only item 0 may start
    r = _post(
        client_for(people["member"]), ws, project, items, {"state_id": str(states["started"].id), "priority": "urgent"}
    )
    assert r.status_code == 400
    assert {x["id"] for x in r.data["refused"]} == {str(items[1].id), str(items[2].id)}
    assert "module" in r.data["refused"][0]["errors"][0]
    for i in items:
        i.refresh_from_db()
        assert (i.state_id, i.priority) == (states["backlog"].id, "none")  # item 0 rolled back too


def test_module_and_status_together_pass_the_module_rule(client_for, people, ws, project, states, items, calls):
    services.set_flags(ws.id, {"ext.require_module": True})
    gl = Module.objects.create(name="General Ledger", project=project, workspace=ws)
    r = _post(
        client_for(people["member"]),
        ws,
        project,
        items,
        {"module_ids": [str(gl.id)], "state_id": str(states["started"].id)},
    )
    assert r.status_code == 200, r.data
    assert ModuleIssue.objects.filter(module=gl).count() == 3
    assert all(Issue.objects.get(pk=i.pk).state_id == states["started"].id for i in items)
    assert sum(1 for _, kw in calls if kw.get("type") == "module.activity.created") == 3


def test_cycle_add_and_move(client_for, people, ws, project, items):
    now = dt.datetime.now(dt.timezone.utc)
    old = Cycle.objects.create(
        name="2026-11",
        project=project,
        workspace=ws,
        owned_by=people["admin"],
        start_date=now,
        end_date=now + dt.timedelta(days=9),
    )
    new = Cycle.objects.create(
        name="2026-12",
        project=project,
        workspace=ws,
        owned_by=people["admin"],
        start_date=now,
        end_date=now + dt.timedelta(days=40),
    )
    CycleIssue.objects.create(issue=items[0], cycle=old, project=project, workspace=ws)
    r = _post(client_for(people["member"]), ws, project, items[:2], {"cycle_id": str(new.id)})
    assert r.status_code == 200, r.data
    assert set(CycleIssue.objects.filter(issue__in=items[:2]).values_list("cycle_id", flat=True)) == {new.id}
    done = Cycle.objects.create(
        name="2026-10",
        project=project,
        workspace=ws,
        owned_by=people["admin"],
        start_date=now - dt.timedelta(days=40),
        end_date=now - dt.timedelta(days=9),
    )
    assert _post(client_for(people["member"]), ws, project, items[:1], {"cycle_id": str(done.id)}).status_code == 400


def test_dates_cannot_cross(client_for, people, ws, project, items):
    items[0].start_date = dt.date(2026, 12, 10)
    items[0].save()
    r = _post(client_for(people["member"]), ws, project, items[:2], {"target_date": "2026-12-05"})
    assert r.status_code == 400
    assert [x["id"] for x in r.data["refused"]] == [str(items[0].id)]
    assert Issue.objects.get(pk=items[1].pk).target_date is None


@pytest.mark.parametrize("who", ["guest", "outsider"])
def test_guests_and_outsiders_cannot(client_for, people, ws, project, items, who):
    r = _post(client_for(people[who]), ws, project, items, {"priority": "high"})
    assert r.status_code == 403
    assert not Issue.objects.filter(priority="high").exists()


def test_flag_off_and_bad_requests(client_for, people, ws, project, states, items):
    c = client_for(people["admin"])
    other = Project.objects.create(name="Other", identifier="OTHER", workspace=ws)
    ProjectMember.objects.create(project=other, workspace=ws, member=people["admin"], role=20)
    foreign = Issue.objects.create(
        name="Elsewhere",
        project=other,
        workspace=ws,
        state=State.objects.create(name="Open", group="backlog", project=other, workspace=ws),
    )
    assert _post(c, ws, project, items + [foreign], {"priority": "high"}).status_code == 404
    assert _post(c, ws, project, items, {"name": "x"}).status_code == 400
    assert _post(c, ws, project, items, {}).status_code == 400
    services.set_flags(ws.id, {"ext.bulk_edit": False})
    assert _post(c, ws, project, items, {"priority": "high"}).status_code == 403
