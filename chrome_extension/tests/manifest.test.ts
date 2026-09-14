import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";

import { describe, expect, test } from "vitest";

import policy from "../policy.json" with { type: "json" };
import { extensionPolicy } from "../src/security.js";

const root = resolve(import.meta.dirname, "../..");
const bundleManifest = () =>
  JSON.parse(readFileSync(resolve(root, "src/xhs_workbench/extension_bundle/manifest.json"), "utf8")) as Record<
    string,
    unknown
  >;

describe("strict Manifest V3 policy", () => {
  test("builds the deterministic identity and exact least-privilege permission policy", () => {
    const manifest = bundleManifest();

    expect(manifest.manifest_version).toBe(3);
    expect(manifest.key).toMatch(/^[A-Za-z0-9+/]+={0,2}$/);
    expect(manifest.permissions).toEqual(policy.required_permissions);
    expect(manifest.optional_host_permissions).toEqual(policy.optional_host_permissions);
    expect(extensionPolicy).toEqual(policy);
    expect(manifest.background).toEqual({ service_worker: "service-worker.js", type: "module" });
    expect(manifest.action).toEqual({ default_popup: "popup.html", default_title: "小红书研究采集" });
  });

  test("does not expose persistent injection, broad hosts, remote sources, or forbidden capabilities", () => {
    const manifest = bundleManifest();
    const serialized = JSON.stringify(manifest);
    const forbiddenPermissions = ["tabs", "cookies", "webRequest", "debugger", "history", "declarativeNetRequest", "downloads", "<all_urls>"];

    expect(manifest.content_scripts).toBeUndefined();
    expect(manifest.externally_connectable).toBeUndefined();
    expect(manifest.host_permissions).toBeUndefined();
    expect(manifest.content_security_policy).toBeUndefined();
    for (const forbidden of forbiddenPermissions) expect(serialized).not.toContain(forbidden);
    expect(serialized).not.toMatch(/\bhttps?:\/\/(?!www\.xiaohongshu\.com\/\*|\*\.xhscdn\.com\/\*)/);
  });

  test("the build fails closed when the policy injects a forbidden production API", () => {
    const bundleBefore = readFileSync(resolve(root, "src/xhs_workbench/extension_bundle/service-worker.js"), "utf8");
    const injectedPolicy = { ...policy, forbidden_api_patterns: [...policy.forbidden_api_patterns, "chrome.runtime"] };
    const path = resolve(tmpdir(), `xhs-policy-${process.pid}.json`);
    writeFileSync(path, JSON.stringify(injectedPolicy), "utf8");
    try {
      const result = spawnSync("node", ["esbuild.mjs"], {
        cwd: resolve(root, "chrome_extension"),
        env: { ...process.env, XHS_EXTENSION_POLICY_PATH: path },
        encoding: "utf8"
      });
      expect(result.status).not.toBe(0);
      expect(readFileSync(resolve(root, "src/xhs_workbench/extension_bundle/service-worker.js"), "utf8")).toBe(bundleBefore);
    } finally {
      rmSync(path, { force: true });
    }
  });

  test("includes the local popup shell, styles, and bundled script named by the action", () => {
    expect(existsSync(resolve(root, "src/xhs_workbench/extension_bundle/popup.html"))).toBe(true);
    expect(existsSync(resolve(root, "src/xhs_workbench/extension_bundle/popup.css"))).toBe(true);
    expect(existsSync(resolve(root, "src/xhs_workbench/extension_bundle/popup.js"))).toBe(true);
  });
});
