// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

/**
 * Previous/Next over opaque server cursors, shared by every paged Agents list.
 *
 * `Next` is enabled exactly when the server handed back a cursor, so the control
 * states what the server said rather than guessing from row counts. No page
 * number, no total, no "of N" — the wire carries none of those.
 */
export function CursorPager({
  label,
  nextCursor,
  onFirstPage,
  onBack,
  onForward,
}: {
  label: string;
  nextCursor: string | null;
  onFirstPage: boolean;
  onBack: () => void;
  onForward: () => void;
}) {
  if (onFirstPage && nextCursor === null) return null;
  return (
    <nav aria-label={label}>
      <button
        type="button"
        className="ne-btn ne-btn--quiet"
        disabled={onFirstPage}
        onClick={onBack}
      >
        Previous page
      </button>
      <button
        type="button"
        className="ne-btn ne-btn--quiet"
        disabled={nextCursor === null}
        onClick={onForward}
      >
        Next page
      </button>
    </nav>
  );
}
