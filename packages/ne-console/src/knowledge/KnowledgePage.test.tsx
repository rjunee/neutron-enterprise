// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useNavigationType,
} from "react-router";
import { describe, expect, it } from "vitest";
import { KnowledgePage } from "./KnowledgePage";

function LocationProbe() {
  const location = useLocation();
  const navigationType = useNavigationType();
  return (
    <output aria-label="location">{`${navigationType}:${location.pathname}${location.search}`}</output>
  );
}

function HistoryProbe() {
  const navigate = useNavigate();
  return (
    <>
      <LocationProbe />
      <button type="button" onClick={() => void navigate(-1)}>
        Back
      </button>
      <button type="button" onClick={() => void navigate(1)}>
        Forward
      </button>
    </>
  );
}

function renderLegacy(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/library" element={<KnowledgePage />} />
        <Route path="/knowledge" element={<KnowledgePage />} />
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function redirectedLocation(entry: string): Promise<string> {
  const rendered = renderLegacy(entry);
  const output = await screen.findByLabelText("location");
  const location = output.textContent ?? "";
  rendered.unmount();
  return location;
}

describe("retired Knowledge route compatibility", () => {
  it("sends bare Library and bare Knowledge to Documents with replace semantics", async () => {
    for (const legacy of ["/library", "/knowledge"]) {
      const { unmount } = renderLegacy(legacy);
      await waitFor(() =>
        expect(screen.getByLabelText("location")).toHaveTextContent("REPLACE:/knowledge/documents"),
      );
      unmount();
    }
  });

  it("does not leave the retired route in Back/Forward history", async () => {
    render(
      <MemoryRouter
        initialEntries={[
          "/before",
          "/library?tab=graph&anchor=one&anchor=two&citation=three&keep=yes",
        ]}
        initialIndex={1}
      >
        <Routes>
          <Route path="/library" element={<KnowledgePage />} />
          <Route path="*" element={<HistoryProbe />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("location")).toHaveTextContent(
        "REPLACE:/knowledge/graph?anchor=one&anchor=two&citation=three&keep=yes&mode=graph",
      ),
    );

    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("POP:/before");
    await userEvent.click(screen.getByRole("button", { name: "Forward" }));
    expect(screen.getByLabelText("location")).toHaveTextContent(
      "POP:/knowledge/graph?anchor=one&anchor=two&citation=three&keep=yes&mode=graph",
    );
  });

  it("preserves exact document selectors and unrelated query values while deleting only tab", async () => {
    const location = await redirectedLocation(
      "/library?tab=documents&document=opaque%2Fdoc&version=4&stage=graph&q=fee&type=pdf&source=m365&x=1&x=2",
    );
    const url = new URL(location.slice("REPLACE:".length), "https://console.invalid");
    expect(url.pathname).toBe("/knowledge/documents");
    expect(url.searchParams.get("tab")).toBeNull();
    expect(url.searchParams.get("document")).toBe("opaque/doc");
    expect(url.searchParams.get("version")).toBe("4");
    expect(url.searchParams.get("stage")).toBe("graph");
    expect(url.searchParams.getAll("x")).toEqual(["1", "2"]);
    expect(url.search).toBe(
      "?document=opaque%2Fdoc&version=4&stage=graph&q=fee&type=pdf&source=m365&x=1&x=2",
    );
  });

  it("preserves every ordered opaque graph anchor and citation without first-anchor inference", async () => {
    const location = await redirectedLocation(
      "/library?tab=graph&anchor=opaque%2Ftwo&citation=ref%2Fone&anchor=Opaque%2BOne&citation=ref%2Ftwo&depth=3&x=a&x=b",
    );
    const url = new URL(location.slice("REPLACE:".length), "https://console.invalid");
    expect(url.pathname).toBe("/knowledge/graph");
    expect(url.searchParams.getAll("anchor")).toEqual(["opaque/two", "Opaque+One"]);
    expect(url.searchParams.getAll("citation")).toEqual(["ref/one", "ref/two"]);
    expect(url.searchParams.get("entity")).toBeNull();
    expect(url.searchParams.get("mode")).toBe("graph");
    expect(url.searchParams.getAll("x")).toEqual(["a", "b"]);
    expect(url.search).toBe(
      "?anchor=opaque%2Ftwo&citation=ref%2Fone&anchor=Opaque%2BOne&citation=ref%2Ftwo&depth=3&x=a&x=b&mode=graph",
    );
  });

  it("prefers opaque graph context over an entity when no tab exists and preserves as_of exactly", async () => {
    const location = await redirectedLocation(
      "/knowledge?entity=urn%3Ane%3Aentity%3Aaccount%252Fbluepeak&anchor=%20%20&citation=&anchor=Opaque%2FOne&citation=%20&as_of=2026-08-20T12%3A00%3A00Z&anchor=Opaque%2FOne&keep=a&keep=b",
    );
    const url = new URL(location.slice("REPLACE:".length), "https://console.invalid");
    expect(url.pathname).toBe("/knowledge/graph");
    expect(url.searchParams.get("mode")).toBe("graph");
    expect(url.searchParams.get("entity")).toBe("urn:ne:entity:account%2Fbluepeak");
    expect(url.searchParams.getAll("anchor")).toEqual(["  ", "Opaque/One", "Opaque/One"]);
    expect(url.searchParams.getAll("citation")).toEqual(["", " "]);
    expect(url.searchParams.get("as_of")).toBe("2026-08-20T12:00:00Z");
    expect(url.searchParams.getAll("keep")).toEqual(["a", "b"]);
  });

  it("turns legacy Entities identity into canonical Record identity and unwraps an entity URN", async () => {
    const location = await redirectedLocation(
      "/library?tab=entities&entity=urn%3Ane%3Aentity%3Aaccount%252Fbluepeak&mode=graph&keep=yes",
    );
    const url = new URL(location.slice("REPLACE:".length), "https://console.invalid");
    expect(url.pathname).toBe("/knowledge/graph");
    expect(url.searchParams.get("entity")).toBe("account%2Fbluepeak");
    expect(url.searchParams.get("mode")).toBe("record");
    expect(url.searchParams.get("keep")).toBe("yes");
  });

  it("forces the explicit tab mode and treats an unknown tab like absent inference", async () => {
    expect(await redirectedLocation("/library?tab=graph&mode=record&anchor=one")).toBe(
      "REPLACE:/knowledge/graph?mode=graph&anchor=one",
    );
    expect(
      await redirectedLocation(
        "/library?tab=entities&mode=graph&entity=urn%3Ane%3Aentity%3Aaccount%252Fbluepeak",
      ),
    ).toBe("REPLACE:/knowledge/graph?mode=record&entity=account%252Fbluepeak");
    expect(await redirectedLocation("/library?tab=unknown&mode=record&anchor=one")).toBe(
      "REPLACE:/knowledge/graph?mode=graph&anchor=one",
    );
  });

  it("relocates Reference to Ontology and a citation-only Knowledge link to Graph", async () => {
    expect(await redirectedLocation("/library?tab=reference&section=rules")).toBe(
      "REPLACE:/knowledge/ontology?section=rules",
    );
    const location = await redirectedLocation("/knowledge?citation=first&citation=second");
    const url = new URL(location.slice("REPLACE:".length), "https://console.invalid");
    expect(url.pathname).toBe("/knowledge/graph");
    expect(url.searchParams.getAll("citation")).toEqual(["first", "second"]);
  });
});
