import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { cp, mkdtemp, open, readdir, readFile, rename, rm, unlink, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

import { build } from "esbuild";

const root = resolve(import.meta.dirname, "..");
const source = resolve(import.meta.dirname, "src");
const output = resolve(root, "src/xhs_workbench/extension_bundle");
const policyPath = process.env.XHS_EXTENSION_POLICY_PATH ?? resolve(import.meta.dirname, "policy.json");
const policy = JSON.parse(await readFile(policyPath, "utf8"));
const template = JSON.parse(await readFile(resolve(import.meta.dirname, "public/manifest.json"), "utf8"));

const outputParent = dirname(output);
const lock = join(outputParent, ".extension-bundle.lock");
const acquirePublisherLock = async () => {
  try {
    const handle = await open(lock, "wx");
    await handle.writeFile(`${process.pid}\n`);
    return handle;
  } catch {
    const stalePid = Number.parseInt((await readFile(lock, "utf8")).trim(), 10);
    if (!Number.isSafeInteger(stalePid) || stalePid <= 0) {
      throw new Error("extension bundle publisher lock is invalid");
    }
    try {
      process.kill(stalePid, 0);
    } catch (error) {
      if (!(error instanceof Error) || !("code" in error) || error.code !== "ESRCH") throw error;
      await unlink(lock);
      const handle = await open(lock, "wx");
      await handle.writeFile(`${process.pid}\n`);
      return handle;
    }
    throw new Error("another extension build is already publishing the bundle");
  }
};
const lockHandle = await acquirePublisherLock();
try {
  const stranded = (await readdir(outputParent)).filter((name) => name.startsWith(".extension-bundle-previous-"));
  if (!existsSync(output)) {
    if (stranded.length !== 1 || stranded[0] === undefined) throw new Error("extension bundle recovery is ambiguous");
    await rename(join(outputParent, stranded[0]), output);
  } else if (stranded.length > 0) {
    throw new Error("extension bundle recovery is required before another build");
  }
const staging = await mkdtemp(join(outputParent, ".extension-bundle-"));
try {
await build({
  entryPoints: [resolve(source, "service-worker.ts")],
  bundle: true,
  format: "esm",
  platform: "browser",
  target: "es2022",
  minify: true,
  sourcemap: false,
  outfile: resolve(staging, "service-worker.js")
});
await build({
  entryPoints: [resolve(source, "content-script.ts")],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: "es2022",
  minify: true,
  sourcemap: false,
  outfile: resolve(staging, "content-script.js")
});
const buildRevision = createHash("sha256")
  .update(await readFile(resolve(staging, "content-script.js")))
  .digest("hex")
  .slice(0, 12);
await build({
  entryPoints: [resolve(source, "popup/popup.ts")],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: "es2022",
  minify: true,
  sourcemap: false,
  define: { "globalThis.__XHS_BUILD_REVISION__": JSON.stringify(buildRevision) },
  outfile: resolve(staging, "popup.js")
});
await cp(resolve(import.meta.dirname, "public"), staging, { recursive: true });
await cp(resolve(source, "popup/popup.html"), resolve(staging, "popup.html"));
await cp(resolve(source, "popup/popup.css"), resolve(staging, "popup.css"));
await writeFile(
  resolve(staging, "manifest.json"),
  `${JSON.stringify({ ...template, permissions: policy.required_permissions, optional_host_permissions: policy.optional_host_permissions }, null, 2)}\n`,
  "utf8"
);

const allowedUrls = new Set(policy.optional_host_permissions);
const allowedStaticXhsUrls = new Set(["https://www.xiaohongshu.com/search_result"]);
const allowedDynamicXhsPrefix = "https://www.xiaohongshu.com${";
for (const filename of ["manifest.json", "service-worker.js", "content-script.js", "popup.js"]) {
  const content = await readFile(resolve(staging, filename), "utf8");
  const urls = content.match(/https?:\/\/[^\s"']+/gu) ?? [];
  if (urls.some((url) => {
    const normalized = url.replace(/[",}\]]+$/u, "");
    return !allowedUrls.has(normalized) && !allowedStaticXhsUrls.has(normalized) && !normalized.startsWith(allowedDynamicXhsPrefix);
  })) throw new Error("unexpected remote URL in bundle");
  if (
    policy.forbidden_bundle_markers.some((marker) => content.includes(marker)) ||
    policy.forbidden_api_patterns.some((pattern) => content.includes(pattern)) ||
    /chrome\.tabs\.(?!create\b|update\b|remove\b|sendMessage\b|onRemoved\b|query\b)/u.test(content)
  ) {
    throw new Error("forbidden extension bundle content");
  }
}
const previous = await mkdtemp(join(outputParent, ".extension-bundle-previous-"));
await rm(previous, { recursive: true, force: true });
try {
  await rename(output, previous);
  await rename(staging, output);
} catch (error) {
  if (existsSync(previous) && !existsSync(output)) await rename(previous, output);
  throw error;
}
await rm(previous, { recursive: true, force: true });
} finally {
  await rm(staging, { recursive: true, force: true });
}
} finally {
  await lockHandle.close();
  await unlink(lock).catch(() => undefined);
}
