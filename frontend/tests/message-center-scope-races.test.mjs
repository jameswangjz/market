import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/MessageCenter.vue', import.meta.url), 'utf8');
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function state() {
  const current = { scopeVersion: 0, disposed: false, saving: { value: false }, uploading: { value: false }, downloading: { value: false }, healthBusy: { value: false }, error: { value: '' }, notice: { value: '' }, composer: { value: null }, settings: { value: { attachment_max_mb: 20 } }, FormData, Blob, Uint8Array, URL,
    closeDetail: () => assert.fail('stale detail close'), load: () => assert.fail('stale refresh'), failure: () => assert.fail('stale error'), closePreview: () => {}, preview: { value: null } };
  current.scopeActive = version => !current.disposed && version === current.scopeVersion;
  return vm.createContext(current);
}
function install(context, name, next) {
  vm.runInContext(source.slice(source.indexOf(`async function ${name}(`), source.indexOf(next, source.indexOf(`async function ${name}(`))), context);
}

test('old mutation success and failure cannot release saving in the new scope', async () => {
  for (const success of [true, false]) {
    const request = deferred(); const context = state();
    context.api = { post: () => request.promise };
    install(context, 'action', 'async function readAll(');
    const completion = context.action('archive', ['old']);
    context.scopeVersion++; context.saving.value = true;
    if (success) request.resolve({}); else request.reject(new Error('old request failed'));
    await completion;
    assert.equal(context.saving.value, true);
  }
});

test('old upload cannot append attachments, upload another file, or release a new upload', async () => {
  const request = deferred(); const context = state(); let calls = 0;
  const draft = { attachments: [] }; context.composer.value = draft;
  context.api = { post: () => { calls++; return request.promise; } };
  install(context, 'upload', 'async function send(');
  const completion = context.upload({ target: { files: [new Blob(['a']), new Blob(['b'])], value: 'selected' } });
  context.scopeVersion++; context.composer.value = { attachments: [] }; context.uploading.value = true;
  request.resolve({ data: { id: 'old-attachment' } });
  await completion;
  assert.equal(calls, 1); assert.equal(draft.attachments.length, 0); assert.equal(context.uploading.value, true);
});

test('scope switch while decoding preview text cannot publish preview or release a new download', async () => {
  const text = deferred(); const context = state();
  const blob = { slice: (start, end) => end === 16 ? { arrayBuffer: async () => new ArrayBuffer(0) } : { text: () => text.promise } };
  context.api = { get: async () => ({ data: blob }) };
  context.Blob = class { slice() { return { text: () => text.promise }; } };
  install(context, 'attachment', 'async function navigate(');
  const completion = context.attachment({ id: 'old', name: 'old.txt' }, true);
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  context.scopeVersion++; context.downloading.value = true;
  text.resolve('old content');
  await completion;
  assert.equal(context.preview.value, null); assert.equal(context.downloading.value, true);
});
