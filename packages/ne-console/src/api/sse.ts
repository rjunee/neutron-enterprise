// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Thin fetch-stream Server-Sent-Events parser. We own ~80 LOC rather than adopt
// a dependency because: native EventSource cannot send an Authorization header
// or POST a body (both mandatory for the governed, bearer-scoped chat API), and
// @microsoft/fetch-event-source is archived/unmaintained (last release 2021),
// failing the maintainer-viability intake check. Build-vs-adopt exception logged
// in the NE-061 build brief.
//
// This module is INSIDE src/api/ — the one sanctioned network boundary. It is
// the only place `fetch` is called for streaming (guardian-enforced).

import { transportUserMessage } from "./userMessage";

/**
 * Thrown on a stream that never became a stream.
 *
 * Same split as ApiError, for the same reason and enforced by the same
 * guardian: the client-composed "stream failed with HTTP {status}" lives on
 * `developerMessage`, and `message`/`userMessage` carry the server's `detail`
 * or §2.4's sentence for the status class. useAgentStream renders whatever this
 * carries when a turn dies before its first frame.
 */
export class SseError extends Error {
  readonly status: number;
  readonly detail?: string;
  /** The server's sentence, or §2.4's for this status class. ALWAYS populated.
   * Render this. */
  readonly userMessage: string;
  /** The client-composed transport string. Console and telemetry only. */
  readonly developerMessage: string;
  constructor(developerMessage: string, status: number, detail?: string) {
    super(transportUserMessage(status, detail));
    this.name = "SseError";
    this.status = status;
    this.detail = detail;
    this.developerMessage = developerMessage;
    this.userMessage = this.message;
  }
}

/** One decoded SSE frame: the `event:` name (default "message") and joined data. */
export interface SseFrame {
  event: string;
  data: string;
}

interface StreamSseArgs {
  url: string;
  headers: Record<string, string>;
  body: string;
  signal?: AbortSignal;
  onFrame: (frame: SseFrame) => void;
  /**
   * Invoked once per comment-only frame (the server's `: keep-alive` ticks).
   * A comment is not data and never reaches `onFrame`, but it IS proof the
   * server is alive — the caller's liveness clock feeds on it. Optional so
   * every existing call site is untouched.
   */
  onComment?: () => void;
}

/**
 * POST to `url` and consume a text/event-stream response, invoking `onFrame`
 * for every complete SSE frame. Fails closed: a non-200 status or a
 * non-`text/event-stream` content-type throws {@link SseError}. A non-200 JSON
 * response contributes only its string `detail`; no other response field is
 * carried. Handles frames split across network chunks, CRLF, multi-line `data:`
 * (joined with "\n"), comment lines (leading ":"), and AbortSignal.
 */
export async function streamSse({
  url,
  headers,
  body,
  signal,
  onFrame,
  onComment,
}: StreamSseArgs): Promise<void> {
  const response = await fetch(url, {
    method: "POST",
    headers: { ...headers, Accept: "text/event-stream" },
    body,
    signal,
  });

  if (response.status !== 200) {
    // FastAPI refusal/unavailable responses are JSON rather than SSE. Carry
    // only its documented, human-readable string detail; arbitrary response
    // keys and non-string values never cross into the rendered error path.
    let detail: string | undefined;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Empty/non-JSON body: the status remains authoritative on its own.
    }
    throw new SseError(`stream failed with HTTP ${response.status}`, response.status, detail);
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("text/event-stream")) {
    throw new SseError(`expected text/event-stream, got "${contentType}"`, response.status);
  }
  if (!response.body) {
    throw new SseError("streaming response had no body", response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // ABORT MUST REACH THE READ LOOP. `signal` is honoured by `fetch` for the
  // request, but once the response is in hand a pending `reader.read()` is only
  // interrupted if the body stream is errored — which not every runtime does on
  // abort. A stream that opens and then sends nothing (observed live on the demo
  // deployment: no error frame, no done frame, an open stream past 80 seconds)
  // would otherwise sit here forever, deaf to the caller's deadline. Cancelling
  // the reader ends the loop cleanly; the CALLER decides what an abort means,
  // which is why this resolves rather than throws.
  const cancelRead = () => {
    void reader.cancel().catch(() => {
      /* already closed/errored — nothing to do. */
    });
  };
  if (signal) {
    if (signal.aborted) cancelRead();
    else signal.addEventListener("abort", cancelRead, { once: true });
  }

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      // Append raw, then normalize CRLF/CR to LF — but HOLD a trailing lone CR:
      // it may be the first half of a CRLF split across this and the next network
      // chunk. Normalizing it now would forge a blank line and truncate the frame.
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n").replace(/\r(?!$)/g, "\n");

      // A frame is terminated by a blank line. Emit every complete frame,
      // keeping the trailing (possibly partial) segment in the buffer.
      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const rawFrame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const frame = parseFrame(rawFrame);
        if (frame) onFrame(frame);
        else if (onComment && isCommentOnly(rawFrame)) onComment();
      }
    }
    // Flush any final frame not terminated by a trailing blank line. No more
    // chunks arrive, so a held trailing CR can now be normalized too.
    buffer = (buffer + decoder.decode()).replace(/\r\n?/g, "\n");
    const tail = parseFrame(buffer);
    if (tail) onFrame(tail);
  } finally {
    signal?.removeEventListener("abort", cancelRead);
    reader.releaseLock();
  }
}

/** True when the raw frame carries at least one comment line and no data — the
 * shape of a server keep-alive tick. A frame with data lines is handled by
 * {@link parseFrame}; a fully blank segment is neither. */
function isCommentOnly(raw: string): boolean {
  const lines = raw.split("\n").filter((line) => line !== "");
  return lines.length > 0 && lines.every((line) => line.startsWith(":"));
}

/** Parse one raw frame (lines between blank lines) into an {@link SseFrame}. */
function parseFrame(raw: string): SseFrame | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // blank or comment
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    // Per spec, a single leading space after the colon is stripped.
    let rawValue = colon === -1 ? "" : line.slice(colon + 1);
    if (rawValue.startsWith(" ")) rawValue = rawValue.slice(1);
    if (field === "event") {
      event = rawValue;
    } else if (field === "data") {
      dataLines.push(rawValue);
    }
    // id/retry fields are ignored — we do not resume streams.
  }
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n") };
}
