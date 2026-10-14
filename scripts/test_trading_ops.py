from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import trading_artifact_gate as gate
import trading_rollback_rehearsal as rollback

REAL_RUN = subprocess.run


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        for name, data in {'src/app.js': 'current source', 'public/info.txt': 'public',
                           'package.json': '{}', 'package-lock.json': '{}', 'index.html': 'input'}.items():
            path = self.repo / 'frontend' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        for args in [('init',), ('add', 'frontend'), ('-c', 'user.name=Test', '-c',
                     'user.email=test@example.invalid', 'commit', '-m', 'fixture')]:
            subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True)
        sha = gate.run('git', 'rev-parse', '--short=12', 'HEAD', cwd=self.repo).decode().strip()
        self.tag = '20261013120000-' + sha
        self.sha = sha
        self.output = self.repo / 'export'
        self.build(self.repo / 'frontend')

    def build(self, root):
        (root / 'dist').mkdir(exist_ok=True)
        (root / 'dist/index.html').write_bytes((root / 'src/app.js').read_bytes())

    def npm(self, args, cwd, **kwargs):
        if args[0] != 'npm':
            return REAL_RUN(args, cwd=cwd, **kwargs)
        if args[1] == 'run':
            self.build(Path(cwd))
        return subprocess.CompletedProcess(args, 0)

    def verify(self):
        with patch.object(gate.subprocess, 'run', side_effect=self.npm):
            return gate.prepare(self.repo, self.sha, self.tag, self.output)

    def test_clean_rebuild_comparison(self):
        evidence = self.verify()
        self.assertFalse((self.output / '.git').exists())
        self.assertFalse((self.output / 'frontend/node_modules').exists())
        with patch.object(gate.subprocess, 'run', side_effect=AssertionError('remote must not execute build')):
            result = gate.verify_export(self.output, self.sha, self.tag, evidence['manifest_sha256'])
        self.assertEqual(result, evidence)

    def test_stale_dist_even_with_correct_marker_rejected(self):
        (self.repo / 'frontend/dist/index.html').write_text('old compiled source')
        with self.assertRaisesRegex(ValueError, 'dist differs'):
            self.verify()

    def test_dirty_source_public_and_extra_dist_rejected(self):
        for name in ('src/app.js', 'public/extra.txt', 'dist/extra.js'):
            path = self.repo / 'frontend' / name
            original = path.read_bytes() if path.exists() else None
            path.write_text('changed')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.verify()
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)

    def test_bad_tag_and_symlink_rejected(self):
        with self.assertRaises(ValueError):
            gate.prepare(self.repo, self.sha, 'latest', self.output)
        path = self.repo / 'frontend/dist/link'
        path.symlink_to(self.repo / 'frontend/index.html')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.verify()

    def test_remote_tampering_and_wrong_release_rejected(self):
        evidence = self.verify()
        for name in ('frontend/src/app.js', 'frontend/dist/index.html', 'frontend/dist/market-build.json'):
            path = self.output / name
            original = path.read_bytes()
            path.write_text('altered')
            with self.subTest(name=name), self.assertRaises(ValueError):
                gate.verify_export(self.output, self.sha, self.tag, evidence['manifest_sha256'])
            path.write_bytes(original)
        with self.assertRaises(ValueError):
            gate.verify_export(self.output, 'a' * 40, self.tag, evidence['manifest_sha256'])
        manifest = self.output / gate.MANIFEST
        manifest.write_text(json.dumps({'commit': 'a' * 40}))
        with self.assertRaisesRegex(ValueError, 'trusted pipeline digest'):
            gate.verify_export(self.output, self.sha, self.tag, evidence['manifest_sha256'])


class RollbackTests(unittest.TestCase):
    def exercise(self, fail=False, leftover=False, hook=False, image='docker.io/library/postgres:16-alpine'):
        calls = []
        def command(args, **kwargs):
            calls.append((args, kwargs))
            if fail and 'psql' in args:
                raise subprocess.CalledProcessError(1, args)
            if 'pg_dump' in args:
                kwargs['stdout'].write(b'stable snapshot\n')
            if 'ps' in args:
                name = next(a[a.index('--name') + 1] for a, _ in calls if 'run' in a)
                return subprocess.CompletedProcess(args, 0, stdout=(name.encode() + b'\n') if leftover else b'')
            return subprocess.CompletedProcess(args, 0, stdout=b'')
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / 'backup.sql'
            backup.write_text('CREATE TABLE fixture (id integer);')
            with patch.object(rollback.subprocess, 'run', side_effect=command):
                if fail or leftover:
                    with self.assertRaises(ValueError if leftover else subprocess.CalledProcessError):
                        rollback.rehearse(backup, 'postgres:16-alpine')
                else:
                    result = rollback.rehearse(backup, image,
                                              compatibility_hook=['probe'] if hook else None)
                    self.assertEqual(result['restores'], 2)
                    self.assertTrue(result['cleanup_verified'])
        return calls

    def test_isolation_restore_and_cleanup(self):
        calls = self.exercise()
        run = next(args for args, _ in calls if 'run' in args)
        self.assertIn('--network=none', run)
        self.assertIn('--pull=never', run)
        self.assertNotIn('-p', run)
        self.assertNotIn('-v', run)
        self.assertEqual(sum('psql' in args for args, _ in calls), 2)
        self.assertEqual(run[:3], ['nerdctl', '--namespace', 'k8s.io'])
        self.assertTrue(all('stdin' in kwargs and 'input' not in kwargs
                            for args, kwargs in calls if 'psql' in args))
        self.assertIn('rm', calls[-2][0])
        self.assertIn('ps', calls[-1][0])

    def test_restore_failure_still_cleans_up(self):
        calls = self.exercise(fail=True)
        self.assertIn('rm', calls[-2][0])
        self.assertIn('ps', calls[-1][0])

    def test_cleanup_absence_required(self):
        self.exercise(leftover=True)

    def test_separate_compatibility_hook(self):
        calls = self.exercise(hook=True)
        probes = [kwargs for args, kwargs in calls if args == ['probe']]
        self.assertEqual(len(probes), 2)
        self.assertTrue(probes[0]['env']['REHEARSAL_CONTAINER'].startswith('market-rollback-'))

    def test_digest_image_reference(self):
        self.exercise(image='docker.io/library/postgres@sha256:' + 'a' * 64)


if __name__ == '__main__':
    unittest.main()
