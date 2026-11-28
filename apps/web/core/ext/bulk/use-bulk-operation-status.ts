/**
 * SPDX-License-Identifier: AGPL-3.0-only
 * Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
 */

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { extStore } from "../store/ext.store";

/**
 * Replaces upstream's `useBulkOperationStatus = () => false` (vite alias, patch 0007): bulk edit is on
 * when the workspace has the plane_ext flag `ext.bulk_edit`. Plain state, not MobX, because some
 * layouts that call this hook are not observers.
 */
export const useBulkOperationStatus = () => {
  const params = useParams();
  const ws = params.workspaceSlug?.toString() ?? "";
  const [enabled, setEnabled] = useState(() => (ws ? extStore.isEnabled(ws, "ext.bulk_edit") : false));
  useEffect(() => {
    if (!ws) return;
    let alive = true;
    void extStore
      .loadFlags(ws)
      .then(() => alive && setEnabled(extStore.isEnabled(ws, "ext.bulk_edit")))
      .catch(() => alive && setEnabled(false));
    return () => {
      alive = false;
    };
  }, [ws]);
  return enabled;
};
