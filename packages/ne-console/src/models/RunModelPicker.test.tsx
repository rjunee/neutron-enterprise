// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { useCallback, useState } from "react";
import { AuthContext } from "react-oidc-context";
import { describe, expect, it, vi } from "vitest";
import type { ModelChoiceCatalogView, ModelChoiceSurface } from "../api/types";
import { makeAuthValue } from "../test/authStub";
import { server } from "../test/mocks/server";
import { RunModelPicker } from "./RunModelPicker";

function catalog(surface: ModelChoiceSurface, models: string[]): ModelChoiceCatalogView {
  return {
    surface,
    task: surface === "ask" ? "reason" : "agentic-workflow",
    effective_default: models[0] ?? null,
    choices: models.map((model) => ({
      model,
      provider: model.startsWith("frontier") ? "anthropic" : "vllm",
      lane: model.startsWith("frontier") ? "frontier-api" : "private-vllm",
      // This surface lists CONFIGURED models, so the server measured nothing
      // and sends no evidence — the picker must render that fine.
      evidence: null,
      observed_at: null,
    })),
  };
}

const AUTH = makeAuthValue();

function Harness({ surface, auth = AUTH }: { surface: ModelChoiceSurface; auth?: typeof AUTH }) {
  const [model, setModel] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [revision, setRevision] = useState(0);
  return (
    <AuthContext.Provider value={auth}>
      <span data-testid="selected-model">{model ?? "none"}</span>
      <span data-testid="catalog-ready">{ready ? "ready" : "not-ready"}</span>
      <button
        type="button"
        onClick={() => {
          setModel(null);
          setRevision((value) => value + 1);
        }}
      >
        Invalidate
      </button>
      <RunModelPicker
        surface={surface}
        value={model}
        onChange={setModel}
        onReadinessChange={setReady}
        invalidationKey={revision}
      />
    </AuthContext.Provider>
  );
}

describe("RunModelPicker", () => {
  it("reuses the exact Agent preflight catalog without reading generic model choices", async () => {
    const request = vi.fn();
    server.use(
      http.get("*/api/v1/models/choices/*", () => {
        request();
        return HttpResponse.json(catalog("ask", ["GENERIC-CHOICE-SENTINEL"]));
      }),
    );
    const onChange = vi.fn();
    const view = render(
      <RunModelPicker
        catalog={{
          choices: [
            {
              model: "agent-private",
              provider: "vllm",
              lane: "private-vllm",
              evidence: "serving-probe",
              observed_at: "2026-08-23T08:00:00Z",
            },
          ],
          effectiveDefault: "agent-private",
        }}
        value={null}
        onChange={onChange}
        label="Next Run model"
      />,
    );
    expect(screen.getByRole("combobox", { name: "Next Run model" })).toHaveValue("agent-private");
    expect(screen.queryByText(/GENERIC-CHOICE-SENTINEL/)).toBeNull();
    expect(request).not.toHaveBeenCalled();
    expect(onChange).not.toHaveBeenCalled();

    view.rerender(
      <RunModelPicker
        catalog={{ choices: [], effectiveDefault: null }}
        value="agent-private"
        onChange={onChange}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("No model is currently available");
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("ignores an Ada catalog that completes only after switching to Bob", async () => {
    let resolveAda!: (response: Response) => void;
    let resolveBob!: (response: Response) => void;
    server.use(
      http.get("*/api/v1/models/choices/ask", ({ request }) => {
        const authorization = request.headers.get("authorization");
        return new Promise<Response>((resolve) => {
          if (authorization === "Bearer ada-token") resolveAda = resolve;
          else resolveBob = resolve;
        });
      }),
    );
    const adaAuth = makeAuthValue({
      user: { access_token: "ada-token", profile: { sub: "ada" } } as typeof AUTH.user,
    });
    const bobAuth = makeAuthValue({
      user: { access_token: "bob-token", profile: { sub: "bob" } } as typeof AUTH.user,
    });
    const view = render(<Harness surface="ask" auth={adaAuth} />);
    await waitFor(() => expect(resolveAda).toBeTypeOf("function"));

    view.rerender(<Harness surface="ask" auth={bobAuth} />);
    expect(screen.queryByText(/ADA-MODEL-SENTINEL/)).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading model choices");
    await waitFor(() => expect(resolveBob).toBeTypeOf("function"));
    resolveBob(HttpResponse.json(catalog("ask", ["BOB-MODEL"])));
    expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue("BOB-MODEL");
    await act(async () => {
      resolveAda(HttpResponse.json(catalog("ask", ["ADA-MODEL-SENTINEL"])));
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(document.body.textContent).not.toContain("ADA-MODEL-SENTINEL");
    expect(screen.getByRole("combobox", { name: "Run model" })).toHaveValue("BOB-MODEL");
  });

  it("labels each option with the alias alone and offers exactly the server's list", async () => {
    server.use(
      http.get("*/api/v1/models/choices/ask", () =>
        HttpResponse.json(catalog("ask", ["frontier-sonnet", "private-qwen"])),
      ),
    );
    render(<Harness surface="ask" />);

    const picker = await screen.findByRole("combobox", { name: "Run model" });
    // The label is the alias and nothing else: provider and lane are internal
    // routing detail, and appending them to an option that carries no evidence
    // would dress configuration up as a description of what is serving.
    expect(
      screen.getAllByRole<HTMLOptionElement>("option").map((option) => [option.value, option.text]),
    ).toEqual([
      ["frontier-sonnet", "frontier-sonnet"],
      ["private-qwen", "private-qwen"],
    ]);
    expect(screen.queryByRole("option", { name: "configured-but-unserved" })).toBeNull();
    expect(screen.getByTestId("selected-model")).toHaveTextContent("frontier-sonnet");
    expect(screen.getByTestId("catalog-ready")).toHaveTextContent(/^ready$/);

    await userEvent.selectOptions(picker, "private-qwen");
    expect(screen.getByTestId("selected-model")).toHaveTextContent("private-qwen");
  });

  it("clears prior success while a replacement loads and leaves it cleared on failure", async () => {
    let resolveWorkflow!: (response: Response) => void;
    server.use(
      http.get("*/api/v1/models/choices/ask", () =>
        HttpResponse.json(catalog("ask", ["frontier-sonnet"])),
      ),
      http.get(
        "*/api/v1/models/choices/agentic-workflow",
        () => new Promise<Response>((resolve) => (resolveWorkflow = resolve)),
      ),
    );
    const view = render(<Harness surface="ask" />);
    expect(await screen.findByRole("combobox", { name: "Run model" })).toBeInTheDocument();
    expect(screen.getByTestId("selected-model")).toHaveTextContent("frontier-sonnet");

    view.rerender(<Harness surface="agentic-workflow" />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading model choices");
    expect(screen.getByTestId("selected-model")).toHaveTextContent("none");
    expect(screen.getByTestId("catalog-ready")).toHaveTextContent(/^not-ready$/);

    await waitFor(() => expect(resolveWorkflow).toBeTypeOf("function"));
    resolveWorkflow(HttpResponse.json({ detail: "evidence unavailable" }, { status: 503 }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t load model choices.");
    expect(screen.getByTestId("selected-model")).toHaveTextContent("none");
    expect(screen.getByTestId("catalog-ready")).toHaveTextContent(/^not-ready$/);
  });

  it("lets only the latest surface response win", async () => {
    let resolveAsk!: (response: Response) => void;
    server.use(
      http.get(
        "*/api/v1/models/choices/ask",
        () => new Promise<Response>((resolve) => (resolveAsk = resolve)),
      ),
      http.get("*/api/v1/models/choices/agentic-workflow", () =>
        HttpResponse.json(catalog("agentic-workflow", ["private-latest"])),
      ),
    );
    const view = render(<Harness surface="ask" />);
    await screen.findByRole("status");
    view.rerender(<Harness surface="agentic-workflow" />);

    expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue(
      "private-latest",
    );
    await act(async () => {
      resolveAsk(HttpResponse.json(catalog("ask", ["frontier-stale"])));
      // Let fetch consume the released MSW response and the guarded promise
      // settle before asserting. Without this, an immediate absence assertion
      // can pass before the deliberately slow response reaches the component.
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(screen.queryByRole("option", { name: /frontier-stale/ })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Run model" })).toHaveValue("private-latest");
    expect(screen.getByTestId("selected-model")).toHaveTextContent("private-latest");
  });

  it("fails closed when the server has no effective default", async () => {
    server.use(
      http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(catalog("ask", []))),
    );
    render(<Harness surface="ask" />);
    expect(
      await screen.findByText("No model is currently available for this run."),
    ).toHaveAttribute("role", "status");
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.getByTestId("selected-model")).toHaveTextContent("none");
    expect(screen.getByTestId("catalog-ready")).toHaveTextContent(/^not-ready$/);
  });

  it("selects the exact effective default even when it is not the first choice", async () => {
    const response = catalog("ask", ["frontier-first", "private-default"]);
    response.effective_default = "private-default";
    server.use(http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(response)));
    render(<Harness surface="ask" />);
    expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue(
      "private-default",
    );
    expect(screen.getByTestId("selected-model")).toHaveTextContent("private-default");
    expect(screen.getByTestId("catalog-ready")).toHaveTextContent(/^ready$/);
  });

  it("fails closed when effective_default is not in the server choices", async () => {
    const response = catalog("ask", ["private-present"]);
    response.effective_default = "private-missing";
    server.use(http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(response)));
    render(<Harness surface="ask" />);
    expect(await screen.findByText("No model is currently available for this run.")).toBeVisible();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.getByTestId("selected-model")).toHaveTextContent("none");
    expect(screen.getByTestId("catalog-ready")).toHaveTextContent(/^not-ready$/);
  });

  it("synchronously removes invalidated evidence and restores only a freshly read catalog", async () => {
    let reads = 0;
    let models = ["frontier-stale"];
    server.use(
      http.get("*/api/v1/models/choices/ask", () => {
        reads += 1;
        return HttpResponse.json(catalog("ask", models));
      }),
    );
    render(<Harness surface="ask" />);
    expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue(
      "frontier-stale",
    );

    await userEvent.click(screen.getByRole("button", { name: "Invalidate" }));
    expect(screen.queryByRole("combobox", { name: "Run model" })).not.toBeInTheDocument();
    expect(screen.getByTestId("selected-model")).toHaveTextContent("none");
    expect(screen.getByTestId("catalog-ready")).toHaveTextContent(/^not-ready$/);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Model choices changed. Refresh to continue.",
    );
    expect(reads).toBe(1);

    models = ["private-fresh"];
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue("private-fresh");
    expect(screen.getByTestId("selected-model")).toHaveTextContent("private-fresh");
    expect(reads).toBe(2);
  });

  it("reads the catalog once per mount however often the owner re-renders", async () => {
    // The Ask page re-read this endpoint whenever its owner re-rendered with
    // fresh callback identities, and every extra read cleared the selection and
    // dropped the control back to "Loading…" — visible churn under the user's
    // cursor, not merely a wasted request.
    let reads = 0;
    server.use(
      http.get("*/api/v1/models/choices/ask", () => {
        reads += 1;
        return HttpResponse.json(catalog("ask", ["private-qwen"]));
      }),
    );

    function ChurningOwner() {
      const [model, setModel] = useState<string | null>(null);
      const [tick, setTick] = useState(0);
      return (
        <AuthContext.Provider value={AUTH}>
          <span data-testid="tick">{tick}</span>
          <span data-testid="selected-model">{model ?? "none"}</span>
          <button type="button" onClick={() => setTick((value) => value + 1)}>
            Re-render
          </button>
          <RunModelPicker
            surface="ask"
            value={model}
            // Inline arrows: a NEW function object on every render, which is
            // what an owning surface is free to pass.
            onChange={(next) => setModel(next)}
            onReadinessChange={() => undefined}
          />
        </AuthContext.Provider>
      );
    }

    render(<ChurningOwner />);
    expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue("private-qwen");

    await userEvent.click(screen.getByRole("button", { name: "Re-render" }));
    await userEvent.click(screen.getByRole("button", { name: "Re-render" }));
    expect(screen.getByTestId("tick")).toHaveTextContent("2");

    // Still selected, never blanked, and read exactly once.
    expect(screen.getByRole("combobox", { name: "Run model" })).toHaveValue("private-qwen");
    expect(screen.getByTestId("selected-model")).toHaveTextContent("private-qwen");
    expect(reads).toBe(1);
  });

  it("re-announces its alias to a new ownership generation without re-reading", async () => {
    // ChatPage and WorkflowsPage scope the selection to an ownership generation
    // and drop BOTH the held alias and their setter identities when it advances
    // — which is what a settling `/me` does for the same client. The picker is
    // the only holder of the alias at that moment, so it must re-announce it or
    // the owner can never submit. It must do so from the catalog it already
    // read: that generation change does not make the list untrue.
    let reads = 0;
    server.use(
      http.get("*/api/v1/models/choices/ask", () => {
        reads += 1;
        return HttpResponse.json(catalog("ask", ["private-qwen"]));
      }),
    );

    function GenerationalOwner() {
      const [generation, setGeneration] = useState(0);
      const [held, setHeld] = useState({ generation: 0, model: null as string | null });
      const [ready, setReady] = useState({ generation: 0, value: false });
      const model = held.generation === generation ? held.model : null;
      const onChange = useCallback(
        (next: string | null) => setHeld({ generation, model: next }),
        [generation],
      );
      const onReadinessChange = useCallback(
        (value: boolean) => setReady({ generation, value }),
        [generation],
      );
      return (
        <AuthContext.Provider value={AUTH}>
          <span data-testid="selected-model">{model ?? "none"}</span>
          <span data-testid="catalog-ready">
            {ready.generation === generation && ready.value ? "ready" : "not-ready"}
          </span>
          <button type="button" onClick={() => setGeneration((value) => value + 1)}>
            New generation
          </button>
          <RunModelPicker
            surface="ask"
            value={model}
            onChange={onChange}
            onReadinessChange={onReadinessChange}
          />
        </AuthContext.Provider>
      );
    }

    render(<GenerationalOwner />);
    expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue("private-qwen");
    expect(screen.getByTestId("selected-model")).toHaveTextContent("private-qwen");
    expect(screen.getByTestId("catalog-ready")).toHaveTextContent(/^ready$/);
    expect(reads).toBe(1);

    await userEvent.click(screen.getByRole("button", { name: "New generation" }));

    await waitFor(() =>
      expect(screen.getByTestId("selected-model")).toHaveTextContent("private-qwen"),
    );
    expect(screen.getByTestId("catalog-ready")).toHaveTextContent(/^ready$/);
    expect(screen.getByRole("combobox", { name: "Run model" })).toHaveValue("private-qwen");
    expect(reads).toBe(1);
  });

  it("polls the server and clears a selection that is no longer available", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let response = catalog("ask", ["private-live"]);
      server.use(http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(response)));
      render(<Harness surface="ask" />);
      expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue(
        "private-live",
      );

      response = catalog("ask", []);
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(
        await screen.findByText("No model is currently available for this run."),
      ).toBeVisible();
      expect(screen.getByTestId("selected-model")).toHaveTextContent("none");
      expect(screen.getByTestId("catalog-ready")).toHaveTextContent(/^not-ready$/);
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears readiness when a periodic refresh fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let fail = false;
      server.use(
        http.get("*/api/v1/models/choices/ask", () =>
          fail
            ? HttpResponse.json({ detail: "serving evidence unavailable" }, { status: 503 })
            : HttpResponse.json(catalog("ask", ["private-live"])),
        ),
      );
      render(<Harness surface="ask" />);
      expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue(
        "private-live",
      );

      fail = true;
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t load model choices.");
      expect(screen.getByTestId("selected-model")).toHaveTextContent("none");
      expect(screen.getByTestId("catalog-ready")).toHaveTextContent(/^not-ready$/);
    } finally {
      vi.useRealTimers();
    }
  });

  it("preserves an explicit choice across a refresh while it remains available", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let response = catalog("ask", ["private-default", "private-explicit"]);
      server.use(http.get("*/api/v1/models/choices/ask", () => HttpResponse.json(response)));
      render(<Harness surface="ask" />);
      const picker = await screen.findByRole("combobox", { name: "Run model" });
      await userEvent
        .setup({ advanceTimers: vi.advanceTimersByTime })
        .selectOptions(picker, "private-explicit");

      response = { ...response, effective_default: null };
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(screen.getByRole("combobox", { name: "Run model" })).toHaveValue("private-explicit");
      expect(screen.getByTestId("selected-model")).toHaveTextContent("private-explicit");
    } finally {
      vi.useRealTimers();
    }
  });

  it("ignores a slow stale poll and an in-flight response after unmount", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let read = 0;
      let resolveSlow!: (response: Response) => void;
      let resolveUnmount!: (response: Response) => void;
      server.use(
        http.get("*/api/v1/models/choices/ask", () => {
          read += 1;
          if (read === 1) return HttpResponse.json(catalog("ask", ["private-initial"]));
          if (read === 2) {
            return new Promise<Response>((resolve) => {
              resolveSlow = resolve;
            });
          }
          if (read === 4) {
            return new Promise<Response>((resolve) => {
              resolveUnmount = resolve;
            });
          }
          return HttpResponse.json(catalog("ask", ["private-latest"]));
        }),
      );
      const view = render(<Harness surface="ask" />);
      expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue(
        "private-initial",
      );
      await act(async () => vi.advanceTimersByTimeAsync(60_000));
      expect(await screen.findByRole("combobox", { name: "Run model" })).toHaveValue(
        "private-latest",
      );
      await act(async () => resolveSlow(HttpResponse.json(catalog("ask", ["private-stale"]))));
      expect(screen.getByRole("combobox", { name: "Run model" })).toHaveValue("private-latest");

      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      await waitFor(() => expect(resolveUnmount).toBeTypeOf("function"));
      view.unmount();
      expect(() =>
        resolveUnmount(HttpResponse.json(catalog("ask", ["private-after-unmount"]))),
      ).not.toThrow();
    } finally {
      vi.useRealTimers();
    }
  });
});
