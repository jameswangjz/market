// Read-only installed source and HTTP asset verification against a trusted export.
import fs from 'node:fs';
import crypto from 'node:crypto';
const [manifestPath, installedHashesPath, outputPath] = process.argv.slice(2);
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const expected = Object.fromEntries(Object.entries(manifest.source_sha256)
  .filter(([path]) => path.startsWith('backend/app/'))
  .map(([path, hash]) => [path.slice('backend/'.length), hash]));
const actual = Object.fromEntries(fs.readFileSync(installedHashesPath, 'utf8').trim().split('\n').map(line => {
  const match = line.match(/^([a-f0-9]{64})\s+(app\/.+)$/);
  if (!match) throw new Error('Invalid installed hash line');
  return [match[2], match[1]];
}));
if (JSON.stringify(Object.keys(expected).sort()) !== JSON.stringify(Object.keys(actual).sort()) ||
    Object.keys(expected).some(path => expected[path] !== actual[path])) throw new Error('Installed backend mismatch');
const base = 'http://192.168.10.10:30080/';
for (const [path, hash] of Object.entries(manifest.dist_sha256)) {
  const response = await fetch(new URL(path, base), { signal: AbortSignal.timeout(30000), cache: 'no-store' });
  if (!response.ok) throw new Error(`Asset HTTP ${response.status}: ${path}`);
  if (crypto.createHash('sha256').update(Buffer.from(await response.arrayBuffer())).digest('hex') !== hash)
    throw new Error(`Asset mismatch: ${path}`);
}
const marker = await (await fetch(base + 'market-build.json', { signal: AbortSignal.timeout(30000) })).json();
if (marker.commit !== manifest.commit || marker.tag !== manifest.tag) throw new Error('Build marker mismatch');
const result = { status: 'passed', commit: manifest.commit, tag: manifest.tag,
  backend_files: Object.keys(expected).length, web_assets: Object.keys(manifest.dist_sha256).length,
  missing: [], extra: [], mismatched: [] };
fs.writeFileSync(outputPath, JSON.stringify(result, null, 2) + '\n', { flag: 'wx' });
console.log(JSON.stringify(result));
