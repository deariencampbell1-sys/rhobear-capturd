// Fetch the pinned @rhobear/family-kit release tarball and refuse it unless
// its SHA-256 is the one recorded in package.json (MIGRATING.md §1: pin by
// version + SOURCE_SHA, never a range). The tarball is 78 MB, so it is not
// committed; `npm ci` then installs it from vendor/ and the lockfile's
// integrity hash pins it a second time.
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const pin = JSON.parse(readFileSync(join(root, "package.json"), "utf8")).rhobear.familyKit;
const vendor = join(root, "vendor");
const file = join(vendor, pin.asset);

const sha256 = (path) => createHash("sha256").update(readFileSync(path)).digest("hex");

if (!existsSync(file) || sha256(file) !== pin.sha256) {
  mkdirSync(vendor, { recursive: true });
  execFileSync("gh", ["release", "download", pin.release, "-R", pin.repo,
    "-p", pin.asset, "-D", vendor, "--clobber"], { stdio: "inherit" });
}
const got = sha256(file);
if (got !== pin.sha256) {
  throw new Error(`family kit tarball sha256 ${got} is not the pinned ${pin.sha256}`);
}
console.log(`family kit ${pin.version} (${pin.sourceSha}) verified ${got}`);
