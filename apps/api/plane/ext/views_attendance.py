# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
"""Attendance API (flag ext.attendance). Everyone sees only their own days; HR (workspace Admins and
Admins of a person's Staff file) sees the team view; workspace Admins edit the settings, and Admins of the HR
project edit the shift schedule, holidays and the people they manage."""

import datetime as dt

from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from . import attendance
from .views import GUEST, ExtBaseView


def _month(request):
    value = request.query_params.get("month")
    if not value:
        return None
    try:
        return dt.date.fromisoformat(f"{value}-01")
    except ValueError:
        raise ValidationError({"month": "Use YYYY-MM."})


class AttendanceBase(ExtBaseView):
    flag = attendance.FLAG


class MyAttendanceEndpoint(AttendanceBase):
    def get(self, request, slug):
        ws = self.workspace(slug)
        self.require_workspace_role(slug, GUEST)
        return Response(attendance.me_json(ws.id, request.user, _month(request)))


class ClockEndpoint(AttendanceBase):
    """POST .../attendance/clock-in/ or .../clock-out/ with an optional {"note"}; the time is the server's."""

    def post(self, request, slug, action):
        ws = self.workspace(slug)
        self.require_workspace_role(slug, GUEST)
        note = request.data.get("note") if isinstance(request.data, dict) else None
        if action == "clock-in":
            attendance.clock_in(ws.id, request.user, note)
        elif action == "clock-out":
            attendance.clock_out(ws.id, request.user, note)
        else:
            raise ValidationError({"action": "clock-in or clock-out"})
        return Response(attendance.me_json(ws.id, request.user))


class TeamAttendanceEndpoint(AttendanceBase):
    def get(self, request, slug):
        ws = self.workspace(slug)
        self.require_workspace_role(slug, GUEST)
        return Response(attendance.team_json(ws.id, request.user, _month(request)))


class AttendanceSettingsEndpoint(AttendanceBase):
    def access(self, ws):
        access = attendance.settings_access(ws.id, self.request.user)
        if access is None:
            raise PermissionDenied("You don't have the required permissions.")
        return access

    def get(self, request, slug):
        ws = self.workspace(slug)
        return Response(attendance.settings_json(ws.id, request.user, self.access(ws)))

    def put(self, request, slug):
        ws = self.workspace(slug)
        return Response(attendance.apply_settings(ws, request.user, request.data, self.access(ws)))
