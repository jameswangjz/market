"""Packaged definitions and read-only development task HTTP enrichment."""
import json
from pathlib import Path
import unittest

from sqlalchemy import event, select

from backend.app.development_steps import subtasks_for
import test_message_business_events as fixture


RUNTIME_PATH = Path(__file__).resolve().parents[1] / 'app' / 'development_steps.json'
SOURCE_PATH = Path(__file__).resolve().parents[2] / 'docs' / '新版商城售后退款SLA开发任务-20261014.json'


def definitions():
    return json.loads(RUNTIME_PATH.read_text(encoding='utf-8'))['tasks']


class DevelopmentStepDefinitionTests(unittest.TestCase):
    def test_packaged_definitions_exactly_match_32_tasks_and_124_source_titles(self):
        source = json.loads(SOURCE_PATH.read_text(encoding='utf-8'))
        tasks = definitions()
        self.assertEqual(tasks, {task['code']: task['subtasks'] for task in source['tasks']})
        self.assertEqual(len(tasks), 32)
        self.assertEqual(sum(len(titles) for titles in tasks.values()), 124)

    def test_stable_unique_ids_and_fresh_untracked_results(self):
        ids = set()
        for code, titles in definitions().items():
            expected = [{'id': f'{code}-S{index:02d}', 'title': title, 'status': 'untracked'}
                        for index, title in enumerate(titles, start=1)]
            self.assertEqual(subtasks_for(code), expected)
            self.assertEqual(subtasks_for(code), expected)
            ids.update(item['id'] for item in expected)
            changed = subtasks_for(code)
            changed[0]['status'] = 'done'
            changed[0]['title'] = 'Modified response'
            changed.pop()
            self.assertEqual(subtasks_for(code), expected)
        self.assertEqual(len(ids), 124)

    def test_legacy_and_unknown_tasks_have_no_steps(self):
        for code in ('', 'TRD-BE-001', 'TRD-R3-BE-999'):
            self.assertEqual(subtasks_for(code), [])


class DevelopmentStepHTTPTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixture.BusinessRoutesHTTPTests.setUp
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request

    def test_all_124_steps_do_not_infer_parent_status_or_mutate_database(self):
        m = self.m
        states = ('done', 'review', 'in_progress', 'blocked', 'todo')
        with m.SessionLocal() as db:
            for index, code in enumerate(definitions()):
                db.add(m.DevelopmentTask(code=code, title=code, owner='test', area='test',
                    status=states[index % len(states)], progress=index % 101))
            db.commit()
            before = {task.code: (task.status, task.progress, task.updated_at)
                      for task in db.scalars(select(m.DevelopmentTask))}
        statements = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.lstrip().split()[0].upper())

        event.listen(m.engine, 'before_cursor_execute', capture)
        try:
            result = self.request('GET', '/api/development/tasks', actor='platform')
        finally:
            event.remove(m.engine, 'before_cursor_execute', capture)
        self.assertTrue(statements)
        self.assertEqual(set(statements), {'SELECT'})
        self.assertEqual((result['total'], result['subtask_count']), (32, 124))
        for item in result['items']:
            self.assertEqual((item['status'], item['progress']), before[item['code']][:2])
            self.assertEqual(item['subtasks'], subtasks_for(item['code']))
            self.assertTrue(all(step['status'] == 'untracked' for step in item['subtasks']))
        expected_counts = {state: sum(status == state for status, _, _ in before.values())
                           for state in states}
        self.assertEqual(result['counts'], expected_counts)
        self.assertEqual(result['completion_rate'], round(expected_counts['done'] / 32 * 100, 1))
        with m.SessionLocal() as db:
            after = {task.code: (task.status, task.progress, task.updated_at)
                     for task in db.scalars(select(m.DevelopmentTask))}
        self.assertEqual(before, after)

    def test_count_includes_only_returned_known_tasks_and_legacy_steps_are_empty(self):
        empty = self.request('GET', '/api/development/tasks', actor='platform')
        self.assertEqual((empty['total'], empty['subtask_count'], empty['items']), (0, 0, []))
        with self.m.SessionLocal() as db:
            db.add(self.m.DevelopmentTask(code='TRD-BE-001', title='Legacy', owner='test',
                area='test', status='done', progress=100))
            db.commit()
        legacy = self.request('GET', '/api/development/tasks', actor='platform')
        self.assertEqual(legacy['subtask_count'], 0)
        self.assertEqual(legacy['items'][0]['subtasks'], [])
        code = next(iter(definitions()))
        with self.m.SessionLocal() as db:
            db.add(self.m.DevelopmentTask(code=code, title='Known', owner='test', area='test',
                status='done', progress=100))
            db.commit()
        result = self.request('GET', '/api/development/tasks', actor='platform')
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['subtask_count'], len(definitions()[code]))
        known = next(item for item in result['items'] if item['code'] == code)
        self.assertTrue(all(step['status'] == 'untracked' for step in known['subtasks']))

    def test_super_admin_permission_is_preserved(self):
        for actor in ('ops', 'finance', 'reviewer', 'quality', 'buyer', 'buyer_admin', 'provider'):
            self.request('GET', '/api/development/tasks', actor=actor, expected=403)


if __name__ == '__main__':
    unittest.main()
