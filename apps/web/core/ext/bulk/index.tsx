/**
 * SPDX-License-Identifier: AGPL-3.0-only
 * Copyright (c) 2026 Best Practicify LLC. This file is part of a fork of Plane (https://github.com/makeplane/plane).
 */

import { observer } from "mobx-react";
import { useMultipleSelectStore } from "@/hooks/store/use-multiple-select-store";
import type { TSelectionHelper } from "@/hooks/use-multiple-select";
import { ExtBulkBar } from "./bulk-bar";

type Props = {
  className?: string;
  selectionHelpers: TSelectionHelper;
};

/**
 * Drop-in for upstream's `@/components/issues/bulk-operations` (vite alias, patch 0007): same export name
 * and props, but renders the plane_ext bulk bar instead of the "Upgrade" banner.
 */
export const IssueBulkOperationsRoot = observer(function IssueBulkOperationsRoot(props: Props) {
  const { className, selectionHelpers } = props;
  const { isSelectionActive, selectedEntityIds } = useMultipleSelectStore();

  if (!isSelectionActive || selectionHelpers.isSelectionDisabled) return null;

  return <ExtBulkBar className={className} selectionHelpers={selectionHelpers} selectedIds={selectedEntityIds} />;
});
