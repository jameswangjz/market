"""Bounded receiver admission, with no live database or runtime operations."""
import asyncio
from contextlib import contextmanager
from threading import Event, Lock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import anyio
import httpx
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.sla_gateway_events import ENDPOINT
import test_sla_gateway_events as fixture


class IngestAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.GatewayEventsTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_saturation_rejects_without_database_and_sync_business_route_runs(self):
        entered, release, mutex = Event(), Event(), Lock()
        calls = []
        rollbacks = []

        @self.f.ns['app'].get('/owned-business')
        def business():
            return {'ok': True}

        def execute(statement):
            with mutex:
                calls.append(str(statement))
                if len(calls) == 4:
                    entered.set()
            if not release.wait(5):
                raise RuntimeError('test worker release timed out')
            original = Exception('injected lock timeout')
            original.sqlstate = '55P03'
            raise OperationalError('test statement', {}, original)

        @contextmanager
        def begin():
            try:
                yield SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name='postgresql')),
                                      execute=execute)
            except BaseException:
                with mutex:
                    rollbacks.append(True)
                raise

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.f.ns['app']),
                                        base_url='http://owned-test') as client:
                async def post():
                    return await client.post(ENDPOINT, json={'events': [fixture.fact()]},
                                             headers={'X-SLA-Gateway-Key': fixture.KEY})
                pending = [asyncio.create_task(post()) for _ in range(4)]
                try:
                    with anyio.fail_after(3):
                        while not entered.is_set():
                            await anyio.sleep(.01)
                    with anyio.fail_after(2):
                        rejected = await post()
                        business_response = await client.get('/owned-business')
                    self.assertEqual(rejected.status_code, 503)
                    self.assertEqual(rejected.headers['Retry-After'], '2')
                    self.assertNotIn('accepted', rejected.json())
                    self.assertEqual(len(calls), 4)
                    self.assertEqual(business_response.json(), {'ok': True})
                finally:
                    release.set()
                    responses = await asyncio.gather(*pending, return_exceptions=True)
                self.assertTrue(all(isinstance(r, httpx.Response) and r.status_code == 503 for r in responses), responses)

        with patch.dict(self.f.ns, SessionLocal=SimpleNamespace(begin=begin)):
            anyio.run(scenario)
        self.assertEqual(len(rollbacks), 4)
        # All slots were released, including timeout paths; a new loop can ingest.
        self.assertEqual(self.f.post().json(), {'accepted': 1, 'duplicates': 0})

    def test_repeated_testclient_loops_release_on_success_and_unexpected_error(self):
        @contextmanager
        def fail():
            raise RuntimeError('injected connection failure')
            yield

        for i in range(7):
            with TestClient(self.f.ns['app']) as client:
                headers = {'X-SLA-Gateway-Key': fixture.KEY}
                with patch.dict(self.f.ns, SessionLocal=SimpleNamespace(begin=fail)):
                    with self.assertRaisesRegex(RuntimeError, 'injected connection failure'):
                        client.post(ENDPOINT, json={'events': [fixture.fact(i)]}, headers=headers)
                response = client.post(ENDPOINT, json={'events': [fixture.fact(i)]}, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.f.count(), 7)


if __name__ == '__main__':
    unittest.main()
