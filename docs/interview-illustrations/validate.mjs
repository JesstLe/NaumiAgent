import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const manifest = JSON.parse(readFileSync(resolve(root, 'docs/interview-illustrations/manifest.json'), 'utf8'));
const args = process.argv.slice(2);
assert.ok(args.every(arg => arg === '--require-original-text'), 'Unknown validation option');
const requireOriginalText = args.includes('--require-original-text');
const expected = [...Array(26)].map((_, i) => String(i).padStart(2, '0')).concat('nav');
assert.deepEqual(manifest.items.map(item => item.id).sort(), expected.sort(), 'Chapter coverage must be exactly 00-25 and nav');
const hashes = new Set();
const report = [];

for (const item of manifest.items) {
  assert.equal(item.visual_review, 'passed_with_chapter_caption', `${item.id}: visual review missing`);
  assert.equal(item.local_inserted, true, `${item.id}: chapter insertion incomplete`);
  assert.ok(item.prompt.length > 100 && item.caption.length > 30, `${item.id}: prompt or caption missing`);
  const file = resolve(root, item.path);
  assert.ok(file.startsWith(resolve(root, 'docs/interview/assets/illustrations') + '/'), 'Image path escapes asset directory');
  const bytes = readFileSync(file);
  assert.equal(bytes.subarray(0, 8).toString('hex'), '89504e470d0a1a0a', `${item.id}: invalid PNG signature`);
  assert.equal(bytes.subarray(-8, -4).toString(), 'IEND', `${item.id}: PNG missing final chunk`);
  const width = bytes.readUInt32BE(16);
  const height = bytes.readUInt32BE(20);
  assert.ok(width >= 1024 && height >= 768, `${item.id}: insufficient dimensions`);
  const hash = createHash('sha256').update(bytes).digest('hex');
  assert.ok(!hashes.has(hash), `${item.id}: duplicate image`);
  hashes.add(hash);
  const document = resolve(root, item.document);
  const text = readFileSync(document, 'utf8');
  const start = `<!-- teaching-figure:${item.id}:start -->`;
  const end = `<!-- teaching-figure:${item.id}:end -->`;
  assert.equal(text.split(start).length, 2, `${item.id}: figure missing or duplicated`);
  assert.equal(text.split(end).length, 2, `${item.id}: figure closing marker missing or duplicated`);
  const block = text.slice(text.indexOf(start), text.indexOf(end) + end.length);
  const reference = block.match(/!\[[^\]]+\]\(([^)]+)\)/);
  assert.ok(reference, `${item.id}: accessible image reference missing`);
  assert.equal(resolve(dirname(document), reference[1]), file, `${item.id}: incorrect image link`);
  assert.ok(block.includes(item.caption), `${item.id}: chapter and manifest captions differ`);
  assert.match(manifest.source_revision, /^[a-f0-9]{40}$/, 'Source revision must be a full commit hash');
  const original = execFileSync('git', ['show', `${manifest.source_revision}:${item.document}`], { cwd: root, encoding: 'utf8', maxBuffer: 2_000_000 });
  const withoutFigure = text.replace(block + '\n\n', '');
  const textPreserved = withoutFigure === original;
  if (requireOriginalText) {
    assert.equal(withoutFigure, original, `${item.id}: text outside figure was changed`);
  }
  report.push({ chapter: item.id, width, height, bytes: bytes.length, sha256: hash, text_preserved: textPreserved, feishu: item.feishu });
}
const imageFiles = readdirSync(resolve(root, 'docs/interview/assets/illustrations')).filter(file => file.endsWith('.png'));
assert.equal(imageFiles.length, 27, 'Asset folder must contain exactly the selected 27 PNGs');
console.log(JSON.stringify({ checked: report.length, unique_images: hashes.size, original_text_preserved: report.every(item => item.text_preserved), original_text_required: requireOriginalText, report }, null, 2));
