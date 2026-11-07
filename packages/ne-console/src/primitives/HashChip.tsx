// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §2.7 — "Hashes truncate to 8 chars + copy affordance; full value in `title`
// and clipboard."
//
// Eight characters is what a person reads; sixty-four is what a person pastes.
// The shipped console rendered the full 64 in a 280px rail column, where it
// overflowed and painted across page content on four routes. Truncation is not
// a cosmetic decision — the full value is never lost, it is one click away in
// the clipboard and always present in `title`.
//
// THIN CLIENT (C7): this displays a hash. It never compares one, and there is
// deliberately no code path here that could.

import { useCallback, useState } from "react";
import { truncateHash } from "./format";

export function HashChip({
  hash,
  label = "Copy configuration hash",
}: {
  hash: string | null | undefined;
  /** The copy button's accessible name — say WHICH hash. */
  label?: string;
}) {
  const [copied, setCopied] = useState(false);

  const copy = useCallback(() => {
    if (!hash) return;
    void navigator.clipboard?.writeText(hash).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      },
      () => {
        // Clipboard unavailable (insecure context, denied permission). The full
        // value is still in `title`, so nothing is lost — and the button does
        // not claim a copy that did not happen.
      },
    );
  }, [hash]);

  if (!hash) return <span className="ne-absent">—</span>;

  return (
    <span className="ne-hashchip">
      <span className="ne-mono" title={hash}>
        {truncateHash(hash)}
      </span>
      <button type="button" className="ne-hashchip__copy" aria-label={label} onClick={copy}>
        <span aria-hidden="true">{copied ? "✓" : "⧉"}</span>
      </button>
    </span>
  );
}
