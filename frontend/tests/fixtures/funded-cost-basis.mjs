export const refund = { id: 'funded-refund', order_id: 'order', order_no: 'ORD-FUNDED', refund_no: 'REF-FUNDED', amount: '30.00', status: 'completed' };
export const enterprise = { id: 'provider-tenant', verification_status: 'verified' };
export const provider = { id: 'provider-user', verified_status: 'verified', enterprise_memberships: [
  { enterprise_id: enterprise.id, role: 'enterprise_admin', status: 'active', business_roles: ['provider'] },
] };
export function fundedBasis(count = 1) {
  return { refund_id: refund.id, order_id: refund.order_id, status: 'ready', kind: 'confirmed_funded_refund',
    basis_kind: 'funded', basis_origin: 'raw_funded_basis', proposal_allowed: true,
    references: Array.from({ length: count }, (_, index) => ({ segment_id: `segment-${index}`,
      source_order_id: refund.order_id, reference_settlement_id: `source-${index}`, baseline_hash: String(index + 1).repeat(64),
      currency: 'CNY', cost_amount: index ? '-10.00' : '-20.00', order_no: refund.order_no,
      settlement_no: `SET-${index}`, batch_no: `BATCH-${index}` })) };
}
export function ordinaryBasis(count = 1) {
  return { ...fundedBasis(count), kind: count === 1 ? 'ordinary_single_refund' : 'multi_source_refund',
    basis_kind: count === 1 ? 'single' : 'multi', basis_origin: count === 1 ? 'raw_single_basis' : 'raw_multi_basis' };
}
