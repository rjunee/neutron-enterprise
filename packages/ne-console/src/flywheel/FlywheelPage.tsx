// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §11 — IMPROVEMENT. The steward surface: a proposal queue with real triage
// affordances, the skill library, and the weekly digest.
//
// A page is a title row and content (§1.4). What used to sit here — an animated
// breeding ring, a purpose sentence naming the loop, a paragraph naming the
// route each panel read from — is gone; none of it was the fact the steward came
// for, and the ring occupied a third of the screen to say what the Status column
// says in a word.
//
// Three tabs, deep-linkable through `?tab=`. The Queue's own `?proposal=` param
// carries the pane, so a link from Home, the palette or an approval's source
// reference lands on the right row with it open.

import { Link, useSearchParams } from "react-router";
import { ListScreen } from "../primitives/PageHeader";
import { DigestTab } from "./DigestTab";
import { QueueTab } from "./QueueTab";
import { SkillsTab } from "./SkillsTab";
import "./flywheel.css";

type Tab = "queue" | "skills" | "digest";

const TABS: ReadonlyArray<{ value: Tab; label: string }> = [
  { value: "queue", label: "Queue" },
  { value: "skills", label: "Skills" },
  { value: "digest", label: "Digest" },
];

function tabParam(value: string | null): Tab {
  if (value === "skills") return "skills";
  if (value === "digest") return "digest";
  return "queue";
}

export function FlywheelPage() {
  const [params] = useSearchParams();
  const tab = tabParam(params.get("tab"));

  return (
    <div className="ne-fly">
      <ListScreen
        title="Improvement"
        tabs={
          <nav className="ne-fly__tabs" aria-label="Improvement">
            {TABS.map((entry) => (
              <Link
                key={entry.value}
                to={`/improvement?tab=${entry.value}`}
                className="ne-fly__tab"
                aria-current={tab === entry.value ? "page" : undefined}
              >
                {entry.label}
              </Link>
            ))}
          </nav>
        }
      >
        {tab === "queue" ? <QueueTab /> : null}
        {tab === "skills" ? <SkillsTab /> : null}
        {tab === "digest" ? <DigestTab /> : null}
      </ListScreen>
    </div>
  );
}
