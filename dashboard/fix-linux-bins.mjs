// Fetch the linux platform binaries for rollup + esbuild directly from the npm
// registry and extract them into node_modules, so vite build works inside this
// Linux container. The Windows node_modules already has the win32 binaries; we
// only add the linux ones alongside — does NOT disturb the host install.
import { mkdir, writeFile, readdir, rm } from "node:fs/promises";
import { readFileSync } from "node:fs";
import { execSync } from "node:child_process";

const REG = "https://registry.npmjs.org";
const NM = "/workspace/dashboard/node_modules";

async function meta(pkg) {
  const r = await fetch(`${REG}/${pkg}`);
  if (!r.ok) throw new Error(`${pkg} meta -> ${r.status}`);
  return r.json();
}

async function extractTarball(url, dest) {
  await mkdir(dest, { recursive: true });
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  const tmp = `${NM}/.${Date.now()}.tgz`;
  await writeFile(tmp, buf);
  execSync(`tar -xzf ${tmp} -C ${dest}`, { stdio: "inherit" });
  await rm(tmp);
}

async function install(pkg, version) {
  console.log(`=== ${pkg}@${version} ===`);
  const m = await meta(pkg);
  const v = m.versions[version];
  if (!v) throw new Error(`version ${version} not found for ${pkg}`);
  const dest = `${NM}/${pkg}`;
  await extractTarball(v.dist.tarball, dest);
  const files = await readdir(dest);
  console.log(`  installed -> ${files.join(", ")}`);
}

// 1) rollup linux binary — match installed rollup version
const rollupPkg = JSON.parse(readFileSync(`${NM}/rollup/package.json`, "utf8"));
await install("@rollup/rollup-linux-x64-gnu", rollupPkg.version);

// 2) esbuild linux binary — match installed esbuild version
const esbuildPkg = JSON.parse(readFileSync(`${NM}/esbuild/package.json`, "utf8"));
await install("@esbuild/linux-x64", esbuildPkg.version);

console.log("DONE");
