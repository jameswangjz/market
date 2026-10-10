import test from 'node:test';
import assert from 'node:assert/strict';
import { createSSEParser } from '../src/useNotificationStream.js';

test('SSE preserves Unicode, multiline data and CRLF across every byte boundary', () => {
  const payload = ': heartbeat\r\nevent: notification_changed\r\nid: 42\r\ndata: {"title":"\u6d4b\u8bd5"}\r\ndata: second line\r\n\r\n';
  const bytes = new TextEncoder().encode(payload);
  for (let boundary = 0; boundary <= bytes.length; boundary++) {
    const events = [];
    const parse = createSSEParser(event => events.push(event));
    const decoder = new TextDecoder();
    parse(decoder.decode(bytes.slice(0, boundary), { stream: true }));
    parse(decoder.decode(bytes.slice(boundary), { stream: true }));
    parse(decoder.decode());
    assert.deepEqual(events, [{ event: 'notification_changed', data: '{"title":"\u6d4b\u8bd5"}\nsecond line', id: '42' }]);
  }
});

test('SSE ignores comments and separates consecutive events', () => {
  const events = [];
  const parse = createSSEParser(event => events.push(event));
  parse(': ping\n\nevent: connected\ndata: {}\n\nevent: auth_expired\ndata: {}\n\n');
  assert.deepEqual(events.map(event => event.event), ['connected', 'auth_expired']);
});

test('SSE maintains the last event id and rejects null characters', () => {
  const events = [];
  const parse = createSSEParser(event => events.push(event));
  parse('id: stable\ndata: first\n\nid: bad\0id\ndata: second\n\n');
  assert.equal(events[0].id, 'stable');
  assert.equal(events[1].id, 'stable');
});
