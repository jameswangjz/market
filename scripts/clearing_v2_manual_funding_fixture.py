"""Manual funding browser fixture, isolated SQLite only; never deployed data."""
from unittest.mock import patch
import clearing_v2_funding_browser_fixture as funding


def main():
    original = funding.seed

    def seed(case):
        original(case)
        m = case.m
        with m.SessionLocal() as db:
            db.get(m.Enterprise, 'provider_tenant').verification_status = 'verified'
            db.flush()
            proposal = m.settlement_finance['propose_funding'](db, db.get(m.User, 'platform'), dict(
                request_id='manual-ui-propose', settlement_id=funding.SETTLEMENT_ID,
                kind='external_receipt', amount='10.00', bearer_enterprise_id='provider_tenant',
                reason='Synthetic manual funding evidence', evidence_refs=['synthetic-manual:gap']))
            m.settlement_finance['decide_funding'](db, db.get(m.User, 'ops'), proposal['id'], dict(
                request_id='manual-ui-approve', expected_version=1, decision='approved', note='Synthetic independent approval'))
            db.commit()

    with patch.object(funding, 'seed', seed):
        funding.main()


if __name__ == '__main__':
    main()
