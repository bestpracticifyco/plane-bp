# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""Root URLconf: /api/ext/v1/ in front of upstream's URLconf (re-exported unchanged).

Also serves the one upstream-path endpoint the CE web app calls but CE does not ship:
bulk-operation-issues (bulk edit, flag ext.bulk_edit)."""

from django.urls import include, path

from plane.urls import handler404, urlpatterns as core_urlpatterns  # noqa: F401

from .views_bulk import BulkOperationIssuesEndpoint

urlpatterns = [
    path("api/ext/v1/", include("plane.ext.urls")),
    path(
        "api/workspaces/<str:slug>/projects/<uuid:project_id>/bulk-operation-issues/",
        BulkOperationIssuesEndpoint.as_view(),
        name="ext-bulk-operation-issues",
    ),
    *core_urlpatterns,
]
