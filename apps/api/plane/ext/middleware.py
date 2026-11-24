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
from django.db import models
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


_UUID = r"[0-9a-f-]{36}"
_ITEM_PART = re.compile(
    rf"^/api/(?:assets/v2/|ext/v1/)?workspaces/(?P<slug>[^/]+)/projects/(?P<project_id>{_UUID})/"
    rf"(?:issues|work-items)/(?P<issue_id>{_UUID})/[^/]"
)
_STATE = re.compile(rf"^/api/workspaces/(?P<slug>[^/]+)/projects/(?P<project_id>{_UUID})/states/{_UUID}/")
_PROFILE = re.compile(rf"^/api/workspaces/(?P<slug>[^/]+)/user-(?:issues|activity|stats|profile)/(?P<user_id>{_UUID})/")
# Project areas that summarise or list every item of the project: progress counts, burndown, assignee and
# label breakdowns (cycles, modules, analytics charts), deleted-item ids, and pages.
_PROJECT_AREA = re.compile(
    rf"^/api/workspaces/(?P<slug>[^/]+)/projects/(?P<project_id>{_UUID})/"
    r"(?P<area>cycles|archived-cycles|modules|archived-modules|advance-analytics|advance-analytics-stats|"
    r"advance-analytics-charts|deleted-issues|pages|pages-summary|favorite-pages|user-favorite-cycles|"
    r"user-favorite-modules)/(?P<rest>.*)$"
)
_EMPTY_LISTS = {"cycles", "modules", "pages", "deleted-issues", "archived-cycles", "archived-modules"}
_WS_LISTS = re.compile(r"^/api/workspaces/(?P<slug>[^/]+)/(?:cycles|modules)/$")
_SEARCH = re.compile(r"^/api/workspaces/(?P<slug>[^/]+)/(?P<kind>search|entity-search)/$")
_ASSET = re.compile(
    rf"^/api/assets/v2/workspaces/(?P<slug>[^/]+)/(?:projects/{_UUID}/)?(?:download/)?(?P<asset_id>{_UUID})/$"
)
_PUBLIC_ASSET = re.compile(rf"^/api/v1/workspaces/(?P<slug>[^/]+)/assets/(?P<asset_id>{_UUID})/")
_PUBLIC_PROJECT = re.compile(rf"^/api/v1/workspaces/(?P<slug>[^/]+)/projects/(?P<project_id>{_UUID})/")
_PUBLIC_ITEMS = re.compile(
    r"^/api/v1/workspaces/(?P<slug>[^/]+)/(?:issues|work-items)/(?:search/|(?P<identifier>[^/]+)-[^/-]+/)"
)
GUEST_SCOPE = "Guests can only see the work items they submitted."
_DENY, _EMPTY, _NOT_FOUND = "deny", "empty", "not_found"


class GuestScopeMiddleware:
    """`ext.access_hardening`: a project Guest who may only see their own work items (upstream's
    `guest_view_all_features` off) cannot reach anyone else's through the side doors upstream leaves open.

    Upstream filters the item lists and the item page for such Guests, but not:
    - an item's comments, activity, sub-items, links, relations, attachments, subscribers and ext values
      (reached by item id, with a session or a personal API key);
    - search (global and @-mention search list everyone's item titles and pending Intake requests);
    - project summaries: cycle and module lists and progress, analytics charts, deleted-item ids, pages;
    - files by asset id; a colleague's profile page; the public API (`/api/v1/`); state edits.
    Those requests get 403 (or an empty list, where the web app loads them on every page), and search
    results are filtered. A Guest keeps full use of their own items (comment, attach, react, subscribe).
    Notifications and subscriptions are stopped at the source (signals.py). This is what makes a shared
    Intake project (IT Support, HR time off) private per requester.

    For everyone (flag on): an item reached under the wrong project's URL is 404 (upstream's activity and
    relation endpoints do not check the item's project), a private page another user owns is left out of
    search, and a project file is only served to members of that project.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/api/v1/"):
            verdict = self._public_api(request)
        else:
            verdict = self._app(request)
        if verdict == _NOT_FOUND:
            return JsonResponse({"error": "Not found."}, status=404)
        if verdict == _EMPTY:
            return JsonResponse([], safe=False)
        if verdict:
            return JsonResponse({"error": GUEST_SCOPE}, status=403)
        response = self.get_response(request)
        return self._filter(request, response)

    # -- who and where ---------------------------------------------------------------------------
    @staticmethod
    def _restricted(user, slug):
        return services.guest_scoped_projects(user, slug)

    def _user(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return user
        return self._api_key_user(request) if request.path.startswith("/api/ext/") else user

    # -- before the view ---------------------------------------------------------------------------
    def _app(self, request):
        user = self._user(request)
        m = _ITEM_PART.match(request.path)
        if m is not None:
            return self._item(user, m.group("slug"), m.group("project_id"), m.group("issue_id"))
        m = _PROJECT_AREA.match(request.path)
        if m is not None:
            if m.group("project_id") not in self._restricted(user, m.group("slug")):
                return False
            listing = request.method == "GET" and m.group("rest") == "" and m.group("area") in _EMPTY_LISTS
            return _EMPTY if listing else _DENY
        m = _ASSET.match(request.path)
        if m is not None:
            return self._asset(user, m.group("slug"), m.group("asset_id"))
        m = _STATE.match(request.path) if request.method not in ("GET", "HEAD", "OPTIONS") else None
        if m is not None:
            return m.group("project_id") in self._restricted(user, m.group("slug"))
        m = _PROFILE.match(request.path)
        if m is not None and user is not None and str(getattr(user, "id", "")) != m.group("user_id"):
            return bool(self._restricted(user, m.group("slug")))
        return False

    def _item(self, user, slug, project_id, issue_id):
        from plane.db.models import Issue

        if user is None or not user.is_authenticated or not services.is_enabled_for_slug(slug, "ext.access_hardening"):
            return False
        row = Issue.all_objects.filter(id=issue_id).values("project_id", "created_by_id").first()
        if row is not None and str(row["project_id"]) != project_id:
            return _NOT_FOUND  # an item of another project, reached through this project's URL
        if project_id not in self._restricted(user, slug):
            return False
        return row is None or row["created_by_id"] != user.id

    def _asset(self, user, slug, asset_id):
        """A file that belongs to a project: only for that project's members, and for a scoped Guest only
        when it belongs to their own item (or they uploaded it). Avatars, logos and workspace files pass."""
        from plane.db.models import FileAsset, ProjectMember

        if user is None or not user.is_authenticated or not services.is_enabled_for_slug(slug, "ext.access_hardening"):
            return False
        row = (
            FileAsset.all_objects.filter(id=asset_id, workspace__slug=slug)
            .values("project_id", "issue__created_by_id", "comment__issue__created_by_id", "created_by_id")
            .first()
        )
        if row is None or row["project_id"] is None:
            return False
        if (services.workspace_role(user, slug) or 0) >= 20:
            return False
        if not ProjectMember.objects.filter(project_id=row["project_id"], member=user, is_active=True).exists():
            return True
        if str(row["project_id"]) not in self._restricted(user, slug):
            return False
        owners = (row["issue__created_by_id"], row["comment__issue__created_by_id"], row["created_by_id"])
        return user.id not in owners  # a file on their own item, in a reply on it, or one they uploaded

    def _public_api(self, request):
        m = _PUBLIC_ASSET.match(request.path)
        if m is not None:
            return self._asset(self._api_key_user(request), m.group("slug"), m.group("asset_id"))
        m = _PUBLIC_PROJECT.match(request.path) or _PUBLIC_ITEMS.match(request.path)
        if m is None:
            return False
        user = self._api_key_user(request)
        restricted = self._restricted(user, m.group("slug"))
        if not restricted:
            return False
        if "project_id" in m.groupdict():
            return m.group("project_id") in restricted
        identifier = m.group("identifier")
        return identifier is None or identifier.upper() in {i.upper() for i in restricted.values()}

    @staticmethod
    def _api_key_user(request):
        """The API key's user, looked up read-only (upstream's authentication runs later, in the view)."""
        from django.db.models import Q
        from django.utils import timezone

        from plane.db.models import APIToken

        token = request.headers.get("X-Api-Key")
        if not token:
            return None
        row = (
            APIToken.objects.filter(
                Q(expired_at__gt=timezone.now()) | Q(expired_at__isnull=True),
                token=token,
                is_active=True,
                user__is_active=True,
            )
            .select_related("user")
            .first()
        )
        return row.user if row else None

    # -- after the view: filter lists that mix projects ----------------------------------------------
    def _filter(self, request, response):
        if request.method != "GET" or response.status_code != 200:
            return response
        m = _SEARCH.match(request.path) or _WS_LISTS.match(request.path)
        if m is None or not services.is_enabled_for_slug(m.group("slug"), "ext.access_hardening"):
            return response
        user = self._user(request)
        if user is None or not user.is_authenticated:
            return response
        try:
            data = json.loads(response.content)
        except ValueError:
            return response
        restricted = self._restricted(user, m.group("slug"))
        if "kind" in m.groupdict():
            data = _filter_search(data, user, restricted)
        elif restricted:
            data = _without_projects(data, restricted)
        response.content = json.dumps(data)
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response


def _without_projects(rows, restricted):
    if isinstance(rows, dict) and isinstance(rows.get("results"), list):
        return {**rows, "results": _without_projects(rows["results"], restricted)}
    if not isinstance(rows, list):
        return rows
    return [r for r in rows if not (isinstance(r, dict) and str(r.get("project_id")) in restricted)]


def _filter_search(data, user, restricted):
    """Global search ({"results": {entity: [...]}}) and @-mention search ({entity: [...]})."""
    from plane.db.models import Issue, Page

    if not isinstance(data, dict):
        return data
    groups = data["results"] if isinstance(data.get("results"), dict) else data
    rows = [r for key in ("issue", "intake") for r in groups.get(key) or [] if isinstance(r, dict)]
    scoped = [r["id"] for r in rows if str(r.get("project_id")) in restricted]
    if scoped:
        own = {str(i) for i in Issue.all_objects.filter(id__in=scoped, created_by=user).values_list("id", flat=True)}
        for key in ("issue", "intake"):
            if isinstance(groups.get(key), list):
                groups[key] = [
                    r
                    for r in groups[key]
                    if not (
                        isinstance(r, dict) and str(r.get("project_id")) in restricted and str(r.get("id")) not in own
                    )
                ]
    pages = [r for r in groups.get("page") or [] if isinstance(r, dict)]
    if pages:
        ok = {
            str(p)
            for p in Page.objects.filter(id__in=[r["id"] for r in pages])
            .exclude(models.Q(access=1) & ~models.Q(owned_by=user))
            .values_list("id", flat=True)
        }

        def visible(r):
            ids = r.get("project_ids") or ([r["projects__id"]] if r.get("projects__id") else [])
            in_open_project = not ids or any(str(p) not in restricted for p in ids)
            return str(r.get("id")) in ok and in_open_project

        groups["page"] = [r for r in groups["page"] if visible(r)]
    return data
