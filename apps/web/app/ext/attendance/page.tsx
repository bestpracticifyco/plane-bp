/**
 * SPDX-License-Identifier: AGPL-3.0-only
 * Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
 */

import { useParams } from "next/navigation";
import { AttendancePage } from "@/ext/components/attendance/attendance-page";

export default function Attendance() {
  const { workspaceSlug } = useParams();
  if (!workspaceSlug) return null;
  return <AttendancePage workspaceSlug={workspaceSlug.toString()} />;
}
