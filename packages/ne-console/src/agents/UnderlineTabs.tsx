// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// TABS THAT LOOK LIKE TABS.
//
// The owner, looking at the shipped workspace: "i dont know what those weird
// blue buttons 'chat, conversations, runs, capabilities' are at the top."
// Four solid filled buttons in the primary fill are four primary actions, and a
// screen with four primary actions has none. Nothing in the kit was an actual
// tab strip, so this is one: an underline strip sitting ON the mast's own rule,
// the selected tab carrying a 2px cherenkov underline and every other tab at
// rest in dim text with no fill at all.
//
// The cherenkov here is SELECTION, which is one of the three things the budget
// allows it to be spent on (the others being the screen's single primary action
// and live state). A tab at rest spends none.

export interface UnderlineTab<Id extends string> {
  readonly id: Id;
  readonly label: string;
  /** A served count beside the label ("4 tools · 1 skill"). Quiet, tabular, and
   * omitted entirely when the caller has no served count — never a zero. */
  readonly count?: string | null;
}

export function UnderlineTabs<Id extends string>({
  tabs,
  selected,
  onSelect,
  label,
}: {
  readonly tabs: readonly UnderlineTab<Id>[];
  readonly selected: Id;
  readonly onSelect: (id: Id) => void;
  /** The tablist's accessible name. */
  readonly label: string;
}) {
  return (
    <div className="ag-tabs" role="tablist" aria-label={label}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          className="ag-tab"
          aria-selected={tab.id === selected}
          onClick={() => onSelect(tab.id)}
        >
          {tab.label}
          {tab.count === undefined || tab.count === null ? null : (
            <span className="ag-tab__n">{tab.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}
