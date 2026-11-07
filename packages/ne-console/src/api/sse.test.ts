// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { afterEach, describe, expect, it, vi } from "vitest";
import { SseError, streamSse } from "./sse";
import type { SseFrame } from "./sse";

/** Build a ReadableStream that emits each provided string as one chunk, so we
 * can prove the parser reassembles frames split across network boundaries. */
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
}

function stubFetch(response: Response) {
  const fn = vi.fn(async (_url: string, _init?: RequestInit) => response);
  vi.stubGlobal("fetch", fn);
  return fn;
}

function sseResponse(stream: ReadableStream<Uint8Array>, init?: ResponseInit): Response {
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
    ...init,
  });
}

async function collect(signal?: AbortSignal): Promise<SseFrame[]> {
  const frames: SseFrame[] = [];
  await streamSse({
    url: "/api/v1/chat/sessions/s/messages",
    headers: { Authorization: "Bearer t" },
    body: "{}",
    signal,
    onFrame: (f) => frames.push(f),
  });
  return frames;
}

afterEach(() => vi.unstubAllGlobals());

describe("streamSse parser", () => {
  it("reassembles a frame split across network chunks", async () => {
    stubFetch(sseResponse(streamOf(["event: tok", 'en\ndata: {"a":1}', "\n\n"])));
    const frames = await collect();
    expect(frames).toEqual([{ event: "token", data: '{"a":1}' }]);
  });

  it("joins multi-line data with \\n", async () => {
    stubFetch(sseResponse(streamOf(["data: line1\ndata: line2\n\n"])));
    const frames = await collect();
    expect(frames[0]).toEqual({ event: "message", data: "line1\nline2" });
  });

  it("routes the event field to the frame name", async () => {
    stubFetch(sseResponse(streamOf(["event: done\ndata: {}\n\n"])));
    const frames = await collect();
    expect(frames[0]?.event).toBe("done");
  });

  it("handles CRLF line endings", async () => {
    stubFetch(sseResponse(streamOf(["event: token\r\ndata: hi\r\n\r\n"])));
    const frames = await collect();
    expect(frames[0]).toEqual({ event: "token", data: "hi" });
  });

  it("handles a CRLF split across network chunks (no forged blank line)", async () => {
    // The chunk boundary falls between the \r and \n of the first CRLF. A naive
    // per-chunk \r\n?->\n normalization turns the trailing lone \r into \n, then
    // the next chunk's leading \n forges a blank line that prematurely closes the
    // frame — splitting one frame into two and losing the data join.
    stubFetch(sseResponse(streamOf(["data: hi\r", "\ndata: bye\r\n\r\n"])));
    const frames = await collect();
    expect(frames).toEqual([{ event: "message", data: "hi\nbye" }]);
  });

  it("skips comment lines", async () => {
    stubFetch(sseResponse(streamOf([": keep-alive\ndata: real\n\n"])));
    const frames = await collect();
    expect(frames).toEqual([{ event: "message", data: "real" }]);
  });

  it("reports a comment-only frame as a heartbeat, never as data", async () => {
    // The server's `: keep-alive` tick (ne-api sse.py KEEPALIVE_FRAME shape).
    // It must reach onComment — the turn's stall clock feeds on it — and must
    // NOT reach onFrame.
    stubFetch(sseResponse(streamOf([": keep-alive\n\n", 'event: token\ndata: {"a":1}\n\n'])));
    const frames: SseFrame[] = [];
    let heartbeats = 0;
    await streamSse({
      url: "/api/v1/chat/sessions/s/messages",
      headers: {},
      body: "{}",
      onFrame: (f) => frames.push(f),
      onComment: () => (heartbeats += 1),
    });
    expect(heartbeats).toBe(1);
    expect(frames).toEqual([{ event: "token", data: '{"a":1}' }]);
  });

  it("does not report a data frame as a heartbeat (control)", async () => {
    stubFetch(sseResponse(streamOf(["event: token\ndata: hi\n\n"])));
    let heartbeats = 0;
    await streamSse({
      url: "/api/v1/chat/sessions/s/messages",
      headers: {},
      body: "{}",
      onFrame: () => undefined,
      onComment: () => (heartbeats += 1),
    });
    expect(heartbeats).toBe(0);
  });

  it("flushes a final frame not terminated by a blank line", async () => {
    stubFetch(sseResponse(streamOf(["data: tail"])));
    const frames = await collect();
    expect(frames).toEqual([{ event: "message", data: "tail" }]);
  });

  it("throws SseError on a non-200 response", async () => {
    stubFetch(new Response("nope", { status: 500 }));
    await expect(collect()).rejects.toBeInstanceOf(SseError);
  });

  it("carries only a string FastAPI detail on a non-200 response", async () => {
    stubFetch(
      new Response(
        JSON.stringify({
          detail: "frontier model selection is unavailable under AIR_GAP",
          internal: "must not surface",
        }),
        { status: 403, headers: { "content-type": "application/json" } },
      ),
    );
    await expect(collect()).rejects.toMatchObject({
      name: "SseError",
      status: 403,
      detail: "frontier model selection is unavailable under AIR_GAP",
      // The client-composed string is kept, on the property nothing renders;
      // `message` is the reader's sentence (transport-provenance guardian).
      developerMessage: "stream failed with HTTP 403",
      message: "frontier model selection is unavailable under AIR_GAP",
    });
  });

  it("does not carry a non-string detail or another response field", async () => {
    stubFetch(
      new Response(JSON.stringify({ detail: { secret: true }, internal: "must not surface" }), {
        status: 409,
        headers: { "content-type": "application/json" },
      }),
    );
    const error = await collect().then(
      () => null,
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(SseError);
    expect(error).toMatchObject({ status: 409, detail: undefined });
    expect(String(error)).not.toContain("must not surface");
    expect(String(error)).not.toContain("secret");
  });

  it("throws SseError on a non-text/event-stream content-type", async () => {
    stubFetch(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    await expect(collect()).rejects.toBeInstanceOf(SseError);
  });

  it("passes the AbortSignal through to fetch", async () => {
    const controller = new AbortController();
    const fetchFn = stubFetch(sseResponse(streamOf(["data: x\n\n"])));
    await collect(controller.signal);
    const init = fetchFn.mock.calls[0]?.[1];
    expect(init?.signal).toBe(controller.signal);
  });

  it("propagates an aborted fetch as a rejection", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new DOMException("aborted", "AbortError");
      }),
    );
    await expect(collect()).rejects.toBeInstanceOf(DOMException);
  });
});
