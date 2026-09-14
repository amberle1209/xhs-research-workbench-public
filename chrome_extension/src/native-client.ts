import {
  assertResponseForRequest,
  parseNativeRequest,
  parseNativeResponse,
  type NativeRequest,
  type NativeResponse
} from "./contracts.js";

const NATIVE_HOST_NAME = "com.xhs_workbench.native_host";
const NATIVE_HOST_VERSION = "0.1.3";
const MAX_NATIVE_RESPONSE_BYTES = (1024 * 1024) - 4096;

export type NativeClientFailureCode =
  | "native_host_unavailable"
  | "native_host_interrupted"
  | "native_host_version_mismatch"
  | "native_request_pending"
  | "native_request_invalid"
  | "native_request_too_large"
  | "native_response_invalid"
  | "native_response_too_large";

export class NativeClientError extends Error {
  constructor(readonly code: NativeClientFailureCode) {
    super(code);
    this.name = "NativeClientError";
  }
}

type PendingRequest = Readonly<{
  request: NativeRequest;
  resolve: (response: NativeResponse) => void;
  reject: (error: NativeClientError) => void;
}>;

function encodedSize(value: unknown): number | undefined {
  try {
    return new TextEncoder().encode(JSON.stringify(value)).byteLength;
  } catch {
    return undefined;
  }
}

/** A service-worker-owned, sequential native messaging client. */
export class NativeClient {
  private port: chrome.runtime.Port | undefined;
  private pending: PendingRequest | undefined;
  private terminal: NativeClientFailureCode | undefined;
  private hostVersion: string | undefined;

  async connect(): Promise<Readonly<{ hostVersion: string }>> {
    if (this.terminal !== undefined) throw new NativeClientError(this.terminal);
    if (this.hostVersion !== undefined) return { hostVersion: this.hostVersion };
    if (this.port === undefined) {
      try {
        const port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
        this.port = port;
        port.onMessage.addListener((message: unknown) => this.onMessage(port, message));
        port.onDisconnect.addListener(() => this.onDisconnect(port));
      } catch {
        this.fail("native_host_unavailable");
        throw new NativeClientError("native_host_unavailable");
      }
    }
    const response = await this.request({ protocol_version: "1.0", kind: "health" });
    if (response.kind !== "health_result") {
      this.fail("native_host_unavailable");
      throw new NativeClientError("native_host_unavailable");
    }
    if (response.host_version !== NATIVE_HOST_VERSION) {
      this.fail("native_host_version_mismatch");
      throw new NativeClientError("native_host_version_mismatch");
    }
    this.hostVersion = response.host_version;
    return { hostVersion: response.host_version };
  }

  request(value: unknown): Promise<NativeResponse> {
    if (this.terminal !== undefined) return Promise.reject(new NativeClientError(this.terminal));
    if (this.port === undefined) return Promise.reject(new NativeClientError("native_host_unavailable"));
    if (this.pending !== undefined) return Promise.reject(new NativeClientError("native_request_pending"));

    let request: NativeRequest;
    try {
      request = parseNativeRequest(value);
    } catch {
      return Promise.reject(new NativeClientError("native_request_invalid"));
    }
    const size = encodedSize(request);
    if (size === undefined || size > 1024 * 1024) {
      return Promise.reject(new NativeClientError("native_request_too_large"));
    }

    return new Promise<NativeResponse>((resolve, reject) => {
      this.pending = { request, resolve, reject };
      try {
        this.port?.postMessage(request);
      } catch {
        this.fail("native_host_unavailable");
      }
    });
  }

  /**
   * End this user-commanded session immediately.  Disconnecting deliberately
   * rejects exactly one pending request, but does not leave a terminal client
   * state: a later explicit command must negotiate a fresh host connection.
   */
  interrupt(): void {
    const pending = this.pending;
    const port = this.port;
    this.pending = undefined;
    this.port = undefined;
    this.hostVersion = undefined;
    this.terminal = undefined;
    try {
      port?.disconnect();
    } catch {
      // The pending request still receives the finite interruption code.
    }
    pending?.reject(new NativeClientError("native_host_interrupted"));
  }

  /** Close a completed job only after its terminal popup state is durable. */
  completeTerminal(): void {
    if (this.pending !== undefined) return;
    const port = this.port;
    this.port = undefined;
    this.hostVersion = undefined;
    this.terminal = undefined;
    try {
      port?.disconnect();
    } catch {
      // The completed result is already durable; closing is best-effort only.
    }
  }

  private onMessage(port: chrome.runtime.Port, value: unknown): void {
    if (this.port !== port) return;
    const pending = this.pending;
    if (pending === undefined) {
      this.fail("native_response_invalid");
      return;
    }
    const size = encodedSize(value);
    if (size === undefined || size > MAX_NATIVE_RESPONSE_BYTES) {
      this.fail("native_response_too_large");
      return;
    }
    try {
      const response = assertResponseForRequest(pending.request, parseNativeResponse(value));
      this.pending = undefined;
      pending.resolve(response);
    } catch {
      this.fail("native_response_invalid");
    }
  }

  private onDisconnect(port: chrome.runtime.Port): void {
    // Chrome exposes the normal native-host exit through lastError. It must be
    // observed even for a terminal port that was released before its delayed
    // disconnect callback, otherwise Chrome reports an unchecked runtime error.
    const disconnectedWithRuntimeError = chrome.runtime.lastError !== undefined;
    if (this.port !== port) return;
    this.fail(disconnectedWithRuntimeError ? "native_host_unavailable" : "native_host_interrupted");
  }

  private fail(code: NativeClientFailureCode): void {
    if (this.terminal !== undefined) return;
    this.terminal = code;
    const pending = this.pending;
    this.pending = undefined;
    const port = this.port;
    this.port = undefined;
    try {
      port?.disconnect();
    } catch {
      // The finite code is intentionally the only retained failure detail.
    }
    pending?.reject(new NativeClientError(code));
  }
}
