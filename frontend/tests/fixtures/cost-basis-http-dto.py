"""Generate real discovery DTOs using disposable existing HTTP fixtures only."""
import json

from test_settlement_cost_exception_basis_http import CostBasisSingleHTTPTests, CostBasisMultiHTTPTests


def capture(cls, kind):
    cls.setUpClass()
    case = cls('runTest')
    try:
        case.setUp()
        try:
            if kind == 'multi':
                case.completed()
                refund = dict(id='multi')
            else:
                if kind == 'funded':
                    case.funded_source()
                else:
                    case.original(adjusted=True)
                refund = case.complete()
            basis = case.readonly_basis(refund)
            assert basis['status'] == 'ready' and basis['proposal_allowed'], basis
            with case.m.SessionLocal() as db:
                row = db.get(case.m.Refund, refund['id'])
                order = db.get(case.m.Order, row.order_id)
                choice = dict(id=row.id, order_id=row.order_id, refund_no=row.refund_no,
                    order_no=order.order_no, amount=str(row.amount), status=row.status)
            return dict(refund=choice, basis=basis)
        finally:
            case.tearDown()
    finally:
        case.doCleanups()
        cls.tearDownClass()


print(json.dumps({kind: capture(CostBasisMultiHTTPTests if kind == 'multi' else CostBasisSingleHTTPTests, kind)
    for kind in ('single', 'multi', 'funded')}))
