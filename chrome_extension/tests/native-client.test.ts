import { describe, expect, test, vi } from "vitest";

import { NativeClient, NativeClientError } from "../src/native-client.js";

type MessageListener = (message: unknown) => void;
type DisconnectListener = () => void;

class FakeNativePort {
  readonly posted: unknown[] = [];
  private readonly messageListeners: MessageListener[] = [];
  private readonly disconnectListeners: DisconnectListener[] = [];

  readonly onMessage = { addListener: (listener: MessageListener) => this.messageListeners.push(listener) };
  readonly onDisconnect = { addListener: (listener: DisconnectListener) => this.disconnectListeners.push(listener) };

  postMessage(message: unknown): void {
    this.posted.push(message);
  }

  emit(message: unknown): void {
    for (const listener of this.messageListeners) listener(message);
  }

  disconnect(): void {
    for (const listener of this.disconnectListeners) listener();
  }
}

const healthResult = {
  protocol_version: "1.0",
  kind: "health_result",
  status: "ready",
  host_version: "0.1.3"
};

const beginRequest = {
  protocol_version: "1.0",
  kind: "begin_job",
  job_id: "job_123",
  collection_surface: "extension_current",
  source_page_url: "https://www.xiaohongshu.com/explore/note_123",
  requested_count: 1,
  candidate_scan_limit: 1,
  selection_order: "exact_likes_desc"
} as const;

const jobStarted = {
  protocol_version: "1.0",
  kind: "job_started",
  job_id: "job_123",
  status: "started"
};

const finishRequest = {
  protocol_version: "1.0",
  kind: "finish_job",
  job_id: "job_123"
} as const;

const completedJob = {
  protocol_version: "1.0",
  kind: "job_result",
  job_id: "job_123",
  status: "complete",
  retained_count: 1,
  report_available: true,
  report_file: "index.html"
};

async function connectedClient(port = new FakeNativePort()): Promise<{ client: NativeClient; port: FakeNativePort; connectNative: ReturnType<typeof vi.fn> }> {
  const connectNative = vi.fn(() => port as unknown as chrome.runtime.Port);
  vi.stubGlobal("chrome", { runtime: { connectNative } });
  const client = new NativeClient();
  const pending = client.connect();
  port.emit(healthResult);
  await expect(pending).resolves.toEqual({ hostVersion: "0.1.3" });
  return { client, port, connectNative };
}

describe("NativeClient", () => {
  test("opens only the fixed host and performs version negotiation before requests", async () => {
    const { client, port, connectNative } = await connectedClient();

    expect(connectNative).toHaveBeenCalledExactlyOnceWith("com.xhs_workbench.native_host");
    expect(port.posted).toEqual([{ protocol_version: "1.0", kind: "health" }]);
    const request = client.request(beginRequest);
    port.emit(jobStarted);
    await expect(request).resolves.toEqual(jobStarted);
  });

  test("rejects a host version that was not negotiated by this extension", async () => {
    const port = new FakeNativePort();
    vi.stubGlobal("chrome", { runtime: { connectNative: () => port as unknown as chrome.runtime.Port } });
    const client = new NativeClient();
    const pending = client.connect();

    port.emit({ ...healthResult, host_version: "9.9.9" });

    await expect(pending).rejects.toMatchObject({ code: "native_host_version_mismatch" });
  });

  test("rejects the prior host version before it can send an unsupported report receipt", async () => {
    const port = new FakeNativePort();
    vi.stubGlobal("chrome", { runtime: { connectNative: () => port as unknown as chrome.runtime.Port } });
    const client = new NativeClient();
    const pending = client.connect();

    port.emit({ ...healthResult, host_version: "0.1.1" });

    await expect(pending).rejects.toMatchObject({ code: "native_host_version_mismatch" });
  });

  test("rejects a second native request while the exact first response is outstanding", async () => {
    const { client, port } = await connectedClient();
    const first = client.request(beginRequest);

    await expect(client.request(beginRequest)).rejects.toMatchObject({ code: "native_request_pending" });
    port.emit(jobStarted);
    await expect(first).resolves.toEqual(jobStarted);
  });

  test("fails closed for a mismatched allowed response kind without retaining the host payload", async () => {
    const { client, port } = await connectedClient();
    const pending = client.request(beginRequest);

    port.emit({
      protocol_version: "1.0",
      kind: "progress",
      job_id: "job_123",
      phase: "started",
      discovered: 1,
      inspected: 1,
      eligible: 1,
      selected: 1,
      saved: 0,
      html: "<token>secret</token>"
    });

    await expect(pending).rejects.toMatchObject({ code: "native_response_invalid" });
    await expect(client.request(beginRequest)).rejects.toMatchObject({ code: "native_response_invalid" });
  });

  test("invalidates the native session after a duplicate response", async () => {
    const { client, port } = await connectedClient();
    const pending = client.request(beginRequest);
    port.emit(jobStarted);
    await expect(pending).resolves.toEqual(jobStarted);

    port.emit(jobStarted);

    await expect(client.request(beginRequest)).rejects.toMatchObject({ code: "native_response_invalid" });
  });

  test("turns host loss into a finite interruption and does not reconnect automatically", async () => {
    const { client, port, connectNative } = await connectedClient();
    const pending = client.request(beginRequest);

    port.disconnect();

    await expect(pending).rejects.toMatchObject({ code: "native_host_interrupted" });
    await expect(client.request(beginRequest)).rejects.toMatchObject({ code: "native_host_interrupted" });
    expect(connectNative).toHaveBeenCalledTimes(1);
  });

  test("explicitly interrupts one pending request without retrying and permits a later explicit negotiation", async () => {
    const first = new FakeNativePort();
    const second = new FakeNativePort();
    const connectNative = vi.fn()
      .mockReturnValueOnce(first as unknown as chrome.runtime.Port)
      .mockReturnValueOnce(second as unknown as chrome.runtime.Port);
    vi.stubGlobal("chrome", { runtime: { connectNative } });
    const client = new NativeClient();
    const initial = client.connect();
    first.emit(healthResult);
    await initial;

    const pending = client.request(beginRequest);
    client.interrupt();
    await expect(pending).rejects.toMatchObject({ code: "native_host_interrupted" });
    expect(connectNative).toHaveBeenCalledTimes(1);

    const later = client.connect();
    second.emit(healthResult);
    await expect(later).resolves.toEqual({ hostVersion: "0.1.3" });
    expect(connectNative).toHaveBeenCalledTimes(2);
  });

  test("holds a normally completed host session until the terminal state is persisted", async () => {
    const first = new FakeNativePort();
    const second = new FakeNativePort();
    const connectNative = vi.fn()
      .mockReturnValueOnce(first as unknown as chrome.runtime.Port)
      .mockReturnValueOnce(second as unknown as chrome.runtime.Port);
    let terminalLastErrorReads = 0;
    const runtime = { connectNative };
    Object.defineProperty(runtime, "lastError", {
      get: () => {
        terminalLastErrorReads += 1;
        return { message: "Native host has exited." };
      }
    });
    vi.stubGlobal("chrome", { runtime });
    const client = new NativeClient();

    const firstConnect = client.connect();
    first.emit(healthResult);
    await expect(firstConnect).resolves.toEqual({ hostVersion: "0.1.3" });
    const finish = client.request(finishRequest);
    first.emit(completedJob);
    await expect(finish).resolves.toEqual(completedJob);

    const held = client.connect();
    expect(connectNative).toHaveBeenCalledTimes(1);
    await expect(held).resolves.toEqual({ hostVersion: "0.1.3" });

    client.completeTerminal();

    // Chrome reports the normal terminal host exit through lastError. Read it when
    // the completed session is deliberately closed after its persistence gate.
    expect(terminalLastErrorReads).toBe(1);
    const secondConnect = client.connect();
    second.emit(healthResult);
    await expect(secondConnect).resolves.toEqual({ hostVersion: "0.1.3" });
    expect(connectNative).toHaveBeenCalledTimes(2);
  });

  test("does not couple the service-worker native port to a popup port lifecycle", async () => {
    const { client, port } = await connectedClient();
    const popupPort = new FakeNativePort();
    const pending = client.request(beginRequest);

    popupPort.disconnect();
    port.emit(jobStarted);

    await expect(pending).resolves.toEqual(jobStarted);
  });

  test("rejects oversized native responses before parsing and records only a finite error code", async () => {
    const { client, port } = await connectedClient();
    const pending = client.request(beginRequest);

    port.emit({ ...jobStarted, body: "x".repeat(1024 * 1024) });

    await expect(pending).rejects.toBeInstanceOf(NativeClientError);
    await expect(client.request(beginRequest)).rejects.toMatchObject({ code: "native_response_too_large" });
  });

  test("converts a native connection exception to a finite public code without its raw message", async () => {
    vi.stubGlobal("chrome", {
      runtime: {
        connectNative: () => { throw new Error("token=secret https://ci.xhscdn.com/private.mp4"); }
      }
    });
    const client = new NativeClient();

    const pending = client.connect();

    await expect(pending).rejects.toMatchObject({ code: "native_host_unavailable" });
    await expect(pending.catch((error: unknown) => JSON.stringify(error))).resolves.not.toMatch(/secret|xhscdn|token/i);
  });
});
