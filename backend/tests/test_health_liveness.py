"""The actual liveness endpoint must not depend on worker-thread availability."""
import ast
import asyncio
from pathlib import Path
import unittest

import anyio.to_thread
from fastapi import FastAPI
import httpx


class HealthLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_health_responds_with_saturated_thread_limiter(self):
        path = Path(__file__).resolve().parents[1] / 'app' / 'main.py'
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        endpoint = next(node for node in tree.body
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == 'health')
        self.assertIsInstance(endpoint, ast.AsyncFunctionDef)
        self.assertFalse(any(isinstance(node, ast.Await) for node in ast.walk(endpoint)))
        app = FastAPI()
        namespace = {'app': app, 'now': lambda: 'fixed-health-time'}
        exec(compile(ast.Module(body=[endpoint], type_ignores=[]), str(path), 'exec'), namespace)

        limiter = anyio.to_thread.current_default_thread_limiter()
        borrowers = [object() for _ in range(limiter.total_tokens)]
        try:
            for borrower in borrowers:
                limiter.acquire_on_behalf_of_nowait(borrower)
            self.assertEqual(limiter.available_tokens, 0)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://health.test') as client:
                response = await asyncio.wait_for(client.get('/api/health'), timeout=1)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {
                'status': 'ok', 'service': 'market-api', 'time': 'fixed-health-time',
            })
            self.assertEqual(limiter.available_tokens, 0)
        finally:
            for borrower in borrowers:
                if borrower in limiter.statistics().borrowers:
                    limiter.release_on_behalf_of(borrower)


if __name__ == '__main__':
    unittest.main()
