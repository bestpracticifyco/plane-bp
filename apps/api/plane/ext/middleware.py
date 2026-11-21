# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""Close Plane CE's cross-project leaks for non-admins when `ext.access_hardening` is on.

These upstream endpoints aggregate over every project in the workspace without checking project
membership, so a Member of one private project would see counts and names from all the others:
analytics (legacy and default), saved analytic views, project stats, analytics export, and workspace
estimates. Workspace Admins keep them; everyone else gets 403. The membership-aware advanced-analytics
endpoints are left alone. Registered in plane.settings.bp, so no upstream file changes.
"""

import json
import re

from django.core.exceptions import ValidationError
from django.http import JsonResponse

from . import services

_BLOCKED = re.compile(
    r"^/api/workspaces/(?P<slug>[^/]+)/"
    r"(analytics|analytic-view|saved-analytic-view|export-analytics|default-analytics|project-stats|estimates)/"
)


class AccessHardeningMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        m = _BLOCKED.match(request.path)
        if m is not None:
            user = getattr(request, "user", None)
            slug = m.group("slug")
            if (
                user is not None
                and user.is_authenticated
                and services.is_enabled_for_slug(slug, "ext.access_hardening")
                and (services.workspace_role(user, slug) or 0) < 20
            ):
                return JsonResponse({"error": "Workspace analytics are limited to workspace admins."}, status=403)
        return self.get_response(request)


_CREATE = re.compile(
    r"^/api/(?:v1/)?workspaces/(?P<slug>[^/]+)/projects/(?P<project_id>[0-9a-f-]{36})/(?:issues|work-items)/$"
)
MODULE_REQUIRED = "Add this work item to a module (its Function), or create it in a backlog state such as Open."


class RequireModuleMiddleware:
    """`ext.require_module` on create (FR-EXT-012a).

    The pre_save guard in signals.py covers items moving out of Backlog/Triage, but an item can also be
    created straight into a work state. That cannot be checked at save time: the web create form saves
    the item first and attaches its modules in a second request. The create request itself carries the
    chosen `module_ids`, so it is checked here: a create into a non-backlog state, in a project with
    active modules, with no module chosen, gets 400 and the form shows the message.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        m = _CREATE.match(request.path) if request.method == "POST" else None
        if m is not None and services.is_enabled_for_slug(m.group("slug"), "ext.require_module"):
            error = self._check(m.group("slug"), m.group("project_id"), request.body)
            if error:
                return JsonResponse({"error": error, "state_id": [error]}, status=400)
        return self.get_response(request)

    @staticmethod
    def _check(slug, project_id, body):
        from plane.db.models import Module, State

        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("module_ids"):
            return None
        states = State.objects.filter(project_id=project_id, project__workspace__slug=slug)
        state_id = data.get("state_id") or data.get("state")
        try:
            state = states.filter(id=state_id) if state_id else states.filter(default=True)
            group = state.values_list("group", flat=True).first()
        except (ValueError, ValidationError):
            return None
        if group is None or group in ("backlog", "triage"):
            return None
        if not Module.objects.filter(project_id=project_id, archived_at__isnull=True).exists():
            return None
        return MODULE_REQUIRED


_USER_PROPERTIES = re.compile(
    r"^/api/workspaces/(?P<slug>[^/]+)/projects/(?P<project_id>[0-9a-f-]{36})/user-properties/$"
)


class TeamLayoutMiddleware:
    """`ext.team_layout`: the first time a member opens a project (the web app reads their saved layout
    from user-properties), give them the team layout before upstream answers. See plane/ext/layout.py."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        m = _USER_PROPERTIES.match(request.path) if request.method == "GET" else None
        if m is not None:
            user = getattr(request, "user", None)
            slug = m.group("slug")
            if user is not None and user.is_authenticated and services.is_enabled_for_slug(slug, "ext.team_layout"):
                from . import layout

                layout.ensure_member_layout_safely(m.group("project_id"), user)
        return self.get_response(request)
