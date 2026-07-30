import { createHash } from "node:crypto";
import {
  copyFile,
  mkdir,
  readFile,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";


const DOCX_VERSION = "9.7.1";
const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const runtimeDirectory = path.resolve(scriptDirectory, "..");
const vendorDirectory = path.join(runtimeDirectory, "vendor");
const packagePath = path.join(runtimeDirectory, "package.json");
const lockPath = path.join(runtimeDirectory, "package-lock.json");
const sourceBundlePath = path.join(
  runtimeDirectory,
  "node_modules",
  "docx",
  "dist",
  "index.mjs",
);
const sourceLicensePath = path.join(
  runtimeDirectory,
  "node_modules",
  "docx",
  "LICENSE",
);
const vendorBundlePath = path.join(vendorDirectory, "docx.mjs");
const vendorLicensePath = path.join(vendorDirectory, "LICENSE.docx.txt");


function sha256(payload) {
  return createHash("sha256").update(payload).digest("hex");
}


async function readJson(filePath) {
  return JSON.parse(await readFile(filePath, "utf8"));
}


async function main() {
  const packageJson = await readJson(packagePath);
  const packageLock = await readJson(lockPath);
  const lockedVersion = packageLock.packages?.["node_modules/docx"]?.version;
  if (
    packageJson.dependencies?.docx !== DOCX_VERSION
    || packageJson.engines?.node !== ">=20"
    || packageLock.packages?.[""]?.dependencies?.docx !== DOCX_VERSION
    || packageLock.packages?.[""]?.engines?.node !== ">=20"
    || lockedVersion !== DOCX_VERSION
  ) {
    throw new Error("package.json、package-lock.json 与 bundle 版本不一致。");
  }

  const sourceBundle = await readFile(sourceBundlePath);
  const sourceText = sourceBundle.toString("utf8");
  if (
    /^\s*import(?:\s|\{)/mu.test(sourceText)
    || /^\s*export\s+.+\s+from\s+/mu.test(sourceText)
  ) {
    throw new Error("docx 发布文件不是可独立交付的 ESM bundle。");
  }

  await mkdir(vendorDirectory, { recursive: true });
  await copyFile(sourceBundlePath, vendorBundlePath);
  await copyFile(sourceLicensePath, vendorLicensePath);

  const runtimeFiles = [
    "scripts/check.mjs",
    "scripts/create.mjs",
    "vendor/docx.mjs",
  ];
  const files = {};
  for (const relativePath of runtimeFiles) {
    files[relativePath] = sha256(
      await readFile(path.join(runtimeDirectory, relativePath)),
    );
  }
  const manifest = {
    schema_version: 1,
    bundle_version: 1,
    minimum_node_major: 20,
    dependency: {
      name: "docx",
      version: DOCX_VERSION,
    },
    package_lock_sha256: sha256(await readFile(lockPath)),
    files,
  };
  await writeFile(
    path.join(runtimeDirectory, "bundle-manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8",
  );
  process.stdout.write(
    `${JSON.stringify({ ok: true, dependency: `docx@${DOCX_VERSION}` })}\n`,
  );
}


try {
  await main();
} catch (error) {
  process.stderr.write(
    `${JSON.stringify({
      ok: false,
      error_type: "bundle_build_failed",
      message: error instanceof Error ? error.message : "bundle 生成失败。",
    })}\n`,
  );
  process.exitCode = 1;
}
