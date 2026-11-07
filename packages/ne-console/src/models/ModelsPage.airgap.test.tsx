// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-AIRGAP-TOGGLE — the Network row's governed air-gap change (§10 / §2.5).
//
// C7 is the property under test: the console renders only what it read back.
// Every outcome here is proven by what the SERVER said next — the new posture
// after an applied engage, the `air_gap_pending` marker after a parked
// disengage, the 403 detail verbatim on a deny — and the bidirectional
// controls prove the absence cases: no writer ⇒ no control at all (absent,
// not disabled), and a pending change suppresses the submit affordance.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { PostureView } from "../api/types";
import { LandingPage } from "../landing/LandingPage";
import { PostureProvider } from "../posture/PostureContext";
import { StatusChip } from "../rail/StatusChip";
import { AuthStub } from "../test/authStub";
import {
  CONNECT_STATUS_EMPTY,
  POSTURE,
  POSTURE_NO_AIRGAP,
  POSTURE_PENDING_CHANGE,
} from "../test/mocks/fixtures";
import { ModelsPage } from "./ModelsPage";

/** The client the mocked hook hands the screen. Reassigned per test. */
const stub = vi.hoisted(() => ({ client: {} as Record<string, unknown> }));

vi.mock("../auth/useApiClient", () => ({
  useApiClient: () => stub.client,
}));

function setup() {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={["/models"]}>
        <ModelsPage />
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

/** The production composition that must stay coherent through the flip:
 * one shell-level PostureProvider, Models as the writer, StatusChip as the
 * always-mounted rail consumer, and Home as the next routed consumer. */
function setupShell() {
  const user = userEvent.setup();
  render(
    <AuthStub>
      <MemoryRouter initialEntries={["/models"]}>
        <PostureProvider>
          <StatusChip />
          <Link to="/">Home</Link>
          <Routes>
            <Route path="/models" element={<ModelsPage />} />
            <Route path="/" element={<LandingPage />} />
          </Routes>
        </PostureProvider>
      </MemoryRouter>
    </AuthStub>,
  );
  return user;
}

async function networkValue(): Promise<HTMLElement> {
  return await screen.findByText((_, el) => el?.className === "ne-models__networkvalue");
}

describe("NE-AIRGAP-TOGGLE — the Network row write flow", () => {
  beforeEach(() => {
    stub.client = { posture: () => Promise.resolve(POSTURE_NO_AIRGAP) };
  });

  it("renders NO write control when the client cannot serve the write (absent, not disabled)", async () => {
    setup();
    await screen.findByText("Off");
    expect(screen.queryByRole("button", { name: "Request change" })).toBeNull();
  });

  it("engage: submits the server's opposite of the SERVED posture and re-renders the read-back", async () => {
    // Server story: posture Off -> setAirGap(true) applied -> posture On.
    let posture: PostureView = POSTURE_NO_AIRGAP;
    const setAirGap = vi.fn((airGap: boolean) => {
      posture = { ...POSTURE, air_gap: airGap, air_gap_pending: null };
      return Promise.resolve({ status: "applied" as const, request_id: null });
    });
    stub.client = { posture: () => Promise.resolve(posture), setAirGap };
    const user = setup();

    await screen.findByText("Off");
    await user.click(screen.getByRole("button", { name: "Request change" }));
    // The submit names the consequence (§2.5 step 1) — engaging, since Off.
    await user.click(
      screen.getByRole("button", { name: "Engage air gap — frontier egress stops" }),
    );
    expect(setAirGap).toHaveBeenCalledExactlyOnceWith(true);
    // The new value is the RE-READ posture, not a local flip.
    await screen.findByText("On");
  });

  it("an applied engage refreshes the shared rail and routed Home tile from server truth", async () => {
    let posture: PostureView = POSTURE_NO_AIRGAP;
    const postureRead = vi.fn(() => Promise.resolve(posture));
    const setAirGap = vi.fn((airGap: boolean) => {
      posture = { ...POSTURE, air_gap: airGap, air_gap_pending: null };
      return Promise.resolve({ status: "applied" as const, request_id: null });
    });
    stub.client = {
      posture: postureRead,
      setAirGap,
      listStewardProposals: () => Promise.resolve({ proposals: [] }),
      connectStatus: () => Promise.resolve(CONNECT_STATUS_EMPTY),
    };
    const user = setupShell();

    await screen.findByText("Off");
    await waitFor(() => expect(postureRead).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("button", { name: "Operational" }));
    const status = screen.getByRole("group", { name: "System status" });
    const network = within(status).getByText("Network").closest("a");
    expect(network).toHaveTextContent("1 hosts allowed");

    await user.click(screen.getByRole("button", { name: "Request change" }));
    await user.click(
      screen.getByRole("button", { name: "Engage air gap — frontier egress stops" }),
    );
    await screen.findByText("On");
    await waitFor(() => expect(postureRead).toHaveBeenCalledTimes(4));
    // Clicking the Models control closes the rail popover through its real
    // click-away handler. Re-open it and read the newly rendered shared value.
    await user.click(screen.getByRole("button", { name: "Operational" }));
    const refreshedStatus = screen.getByRole("group", { name: "System status" });
    const refreshedNetwork = within(refreshedStatus).getByText("Network").closest("a");
    expect(refreshedNetwork).toHaveTextContent("Air gap on");

    // Close the popover, then route without remounting the shell provider. Home
    // must consume the same refreshed object and must not issue another posture read.
    await user.click(screen.getByRole("button", { name: "Operational" }));
    await user.click(screen.getByRole("link", { name: "Home" }));
    const controls = document.querySelector('.ne-tile[href="/posture"]');
    await waitFor(() => expect(controls).toHaveTextContent("Air gap on"));
    expect(postureRead).toHaveBeenCalledTimes(4);
  });

  it("disengage: a parked change renders the server's pending marker; the in-force value stays", async () => {
    // Server story: posture On -> setAirGap(false) parks -> posture still On
    // with air_gap_pending set (the §2.5 read-back).
    let posture: PostureView = POSTURE;
    const setAirGap = vi.fn((_airGap: boolean) => {
      posture = POSTURE_PENDING_CHANGE;
      return Promise.resolve({ status: "pending" as const, request_id: "req-airgap-1" });
    });
    const postureRead = vi.fn(() => Promise.resolve(posture));
    stub.client = { posture: postureRead, setAirGap };
    const user = setupShell();

    await screen.findByText("On");
    await waitFor(() => expect(postureRead).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("button", { name: "Request change" }));
    await user.click(screen.getByRole("button", { name: "Disengage air gap — requires approval" }));
    expect(setAirGap).toHaveBeenCalledExactlyOnceWith(false);
    // Pending renders ON the object, from the server's read-back...
    await screen.findByText(/Change requested by user:ops-olga/);
    expect(screen.getByRole("link", { name: "View request" })).toHaveAttribute(
      "href",
      "/approvals?request=req-airgap-1",
    );
    // ...the displayed value is still the in-force posture, and the submit
    // affordance is gone while the change awaits its approver.
    expect((await networkValue()).textContent).toBe("On");
    expect(screen.queryByRole("button", { name: "Request change" })).toBeNull();
    // Two initial readers plus the Models object read-back. A parked result
    // never refreshes the shared in-force posture as if it had applied.
    expect(postureRead).toHaveBeenCalledTimes(3);
  });

  it("denied: renders the server's 403 reason verbatim, and the posture does not move", async () => {
    const reason = "Denied by policy bundle 1.4.0: egress changes are frozen for this deployment";
    const setAirGap = vi.fn(() => Promise.reject(new ApiError("Forbidden", 403, reason)));
    stub.client = { posture: () => Promise.resolve(POSTURE_NO_AIRGAP), setAirGap };
    const user = setup();

    await screen.findByText("Off");
    await user.click(screen.getByRole("button", { name: "Request change" }));
    await user.click(
      screen.getByRole("button", { name: "Engage air gap — frontier egress stops" }),
    );
    await screen.findByText("Denied by policy.");
    expect(screen.getByText(reason)).toBeInTheDocument();
    expect((await networkValue()).textContent).toBe("Off");
  });

  it("denied with NO served reason: states the denial and invents none", async () => {
    // A 403 from a proxy carries no JSON body, so the gate stated nothing. The
    // card said "Denied by policy." over the console's own house sentence,
    // which reads as the gate's reason for denying — the gate gave none. Where
    // it states no reason, none is shown.
    const setAirGap = vi.fn(() => Promise.reject(new ApiError("Forbidden", 403)));
    stub.client = { posture: () => Promise.resolve(POSTURE_NO_AIRGAP), setAirGap };
    const user = setup();

    await screen.findByText("Off");
    await user.click(screen.getByRole("button", { name: "Request change" }));
    await user.click(
      screen.getByRole("button", { name: "Engage air gap \u2014 frontier egress stops" }),
    );
    await screen.findByText("Denied by policy.");
    expect(screen.queryByText("Your role doesn’t include this action.")).toBeNull();
    expect((await networkValue()).textContent).toBe("Off");
  });

  it("error: a failed submit renders the save error, never a flipped value", async () => {
    const setAirGap = vi.fn(() => Promise.reject(new ApiError("boom", 500, "engine unavailable")));
    const postureRead = vi.fn(() => Promise.resolve(POSTURE_NO_AIRGAP));
    stub.client = { posture: postureRead, setAirGap };
    const user = setup();

    await screen.findByText("Off");
    await user.click(screen.getByRole("button", { name: "Request change" }));
    await user.click(
      screen.getByRole("button", { name: "Engage air gap — frontier egress stops" }),
    );
    await screen.findByText(/Couldn.t save/);
    expect(screen.getByText("engine unavailable")).toBeInTheDocument();
    expect((await networkValue()).textContent).toBe("Off");
    expect(postureRead).toHaveBeenCalledTimes(1);
  });

  it("cancel: backs out of the confirm without calling the server", async () => {
    const setAirGap = vi.fn();
    stub.client = { posture: () => Promise.resolve(POSTURE_NO_AIRGAP), setAirGap };
    const user = setup();

    await screen.findByText("Off");
    await user.click(screen.getByRole("button", { name: "Request change" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(setAirGap).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Request change" })).toBeInTheDocument();
  });
});
