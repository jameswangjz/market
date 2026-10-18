import test from 'node:test';
import assert from 'node:assert/strict';
import { actualDiscoveryDtos } from './fixtures/cost-basis-http-dto.mjs';
import { costExceptionSources, costExceptionPayload, costSourceLabel } from '../src/settlementManagement.js';

test('real disposable HTTP discovery DTOs preserve human refs, raw caps and single/multi/funded wire formats', () => {
  const cases = actualDiscoveryDtos();
  for (const [kind, { refund, basis }] of Object.entries(cases)) {
    const sources = costExceptionSources(refund, [], basis);
    assert.equal(sources.length, basis.references.length);
    sources.forEach((source, index) => {
      const ref = basis.references[index];
      assert.ok(ref.order_no); assert.ok(ref.settlement_no); assert.ok(ref.batch_nos.length);
      assert.equal(source.order_no, ref.order_no); assert.equal(source.settlement_no, ref.settlement_no);
      assert.equal(source.batch_no, ref.batch_no || ref.batch_nos.join(' / '));
      assert.equal(costSourceLabel({ ...ref, batch_no: '' }).batch_no, ref.batch_nos.join(' / '));
      assert.equal(source.max_retained_cost, ref.cost_amount.replace(/^-/, ''));
    });
    const form = { refund_id: refund.id, bearer_participant_id: 'fixture-expert', reason: 'Invoice evidence',
      source_allocations: sources.map((s, i) => ({ segment_id: s.segment_id, baseline_id: s.baseline_id,
        retained_cost: i ? '0.00' : '0.01', evidence_refs: [`invoice:${i}`] })) };
    const body = costExceptionPayload(form, sources);
    assert.equal(body.schema_version, kind === 'single' ? undefined : 2);
    assert.equal(body.source_allocations?.length, kind === 'single' ? undefined : sources.length);
    assert.equal(body.basis_kind, kind === 'funded' ? 'funded' : undefined);
    assert.equal(body.retained_cost, '0.01');
    assert.throws(() => costExceptionSources(refund, [], { ...basis, proposal_allowed: false }));
  }
});
