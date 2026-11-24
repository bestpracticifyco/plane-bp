# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""Keep news of other people's items away from a Guest limited to their own items (`ext.access_hardening`).

Upstream subscribes people to an item when they are @mentioned or added as subscribers, then notifies
them in the app and by email. Its background tasks write those rows with bulk_create, which skips model
signals, so the clean-up runs on the Celery task boundaries instead: after the notification and activity
tasks, and before the email batch is assembled. Nothing is sent for a row that is removed here.
"""

from django.db.models import Q

from plane.db.models import EmailNotificationLog, Issue, IssueSubscriber, Notification, ProjectMember

from .models import ExtFeatureFlag


def _scoped_guests(project_ids):
    """{project_id: {user_id}} of Guests limited to own items, in workspaces with the flag on."""
    hardened = ExtFeatureFlag.objects.filter(key="ext.access_hardening", enabled=True).values("workspace_id")
    rows = ProjectMember.objects.filter(
        project_id__in=project_ids,
        role=5,
        is_active=True,
        project__guest_view_all_features=False,
        project__archived_at__isnull=True,
        workspace_id__in=hardened,
    ).values_list("project_id", "member_id")
    out = {}
    for pid, uid in rows:
        out.setdefault(pid, set()).add(uid)
    return out


def purge_issue(issue_id):
    """Drop subscriptions, notifications and unsent emails about `issue_id` for scoped Guests who did not create it."""
    issue = Issue.all_objects.filter(id=issue_id).values("id", "project_id", "created_by_id").first()
    if issue is None:
        return 0
    guests = _scoped_guests([issue["project_id"]]).get(issue["project_id"], set()) - {issue["created_by_id"]}
    if not guests:
        return 0
    n = IssueSubscriber.all_objects.filter(issue_id=issue_id, subscriber_id__in=guests).delete()[0]
    n += Notification.all_objects.filter(entity_identifier=issue_id, receiver_id__in=guests).delete()[0]
    n += EmailNotificationLog.all_objects.filter(
        entity_identifier=issue_id, receiver_id__in=guests, processed_at__isnull=True
    ).delete()[0]
    return n


def purge_pending_emails():
    """Before the email batch: drop unsent emails about items a scoped Guest receiver did not create."""
    pending = list(
        EmailNotificationLog.all_objects.filter(processed_at__isnull=True, entity_identifier__isnull=False).values(
            "id", "entity_identifier", "receiver_id"
        )
    )
    if not pending:
        return 0
    issues = {
        i["id"]: i
        for i in Issue.all_objects.filter(id__in={p["entity_identifier"] for p in pending}).values(
            "id", "project_id", "created_by_id"
        )
    }
    guests = _scoped_guests({i["project_id"] for i in issues.values()})
    drop = [
        p["id"]
        for p in pending
        if (i := issues.get(p["entity_identifier"]))
        and p["receiver_id"] in guests.get(i["project_id"], set())
        and p["receiver_id"] != i["created_by_id"]
    ]
    return EmailNotificationLog.all_objects.filter(Q(id__in=drop)).delete()[0] if drop else 0


def connect():
    from celery.signals import task_postrun, task_prerun

    after = {"plane.bgtasks.notification_task.notifications", "plane.bgtasks.issue_activities_task.issue_activity"}
    before = {"plane.bgtasks.email_notification_task.stack_email_notification"}

    @task_postrun.connect(weak=False, dispatch_uid="plane_ext_guest_scope_after")
    def _after(sender=None, kwargs=None, **_):
        if getattr(sender, "name", None) in after and (kwargs or {}).get("issue_id"):
            purge_issue(kwargs["issue_id"])

    @task_prerun.connect(weak=False, dispatch_uid="plane_ext_guest_scope_before")
    def _before(sender=None, **_):
        if getattr(sender, "name", None) in before:
            purge_pending_emails()
