"""Release packaging and deployment preflight regression tests."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.error import HTTPError

import yaml

import market_release_bundle as package
import verify_market_release as verify


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        path = Path(self.temporary.name)
        root = Path(__file__).resolve().parents[1]
        commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        version = '20261118203000-' + commit[:7]
        self.args = argparse.Namespace(repo=root, commit=commit, version=version,
                                       registry='harbor.example:30443', project='market-ci-verify',
                                       metadata=path / 'images', output=path / 'bundle')
        self.args.metadata.mkdir()
        for i, name in enumerate(package.IMAGES):
            digest = 'sha256:' + hex(i + 1)[2:] * 64
            value = {'image.name': f'harbor.example:30443/market-ci-verify/{name}:{version}',
                     'containerimage.digest': digest, 'containerimage.descriptor': {'digest': digest}}
            (self.args.metadata / f'{name}.json').write_text(json.dumps(value))

    def test_six_images_and_stages_are_pinned_without_environment_secrets(self):
        package.build(self.args)
        release, _ = verify.load_bundle(self.args.output)
        self.assertEqual(set(release['images']), set(package.IMAGES))
        self.assertEqual(release['stages']['jobs']['depends_on'], ['api', 'workers'])
        self.assertTrue(release['stages']['dev-mocks']['optional'])
        self.assertIn({'kind': 'Secret', 'name': 'market-harbor-pull'}, release['environment_resources'])
        for folder in (self.args.output / 'apps').iterdir():
            resources = list(yaml.safe_load_all((folder / 'resources.yaml').read_text()))
            for obj in resources:
                self.assertIn(obj['kind'], {'Deployment', 'CronJob', 'ConfigMap'})
                if obj['kind'] in {'Deployment', 'CronJob'}:
                    for container in package.containers(package.pod_spec(obj)):
                        self.assertRegex(container['image'], r'@sha256:[a-f0-9]{64}$')
                        self.assertEqual(container['imagePullPolicy'], 'IfNotPresent')

    def test_missing_build_metadata_prevents_package_creation(self):
        (self.args.metadata / 'market-web.json').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing BuildKit metadata'):
            package.build(self.args)
        self.assertFalse(self.args.output.exists())

    def test_mixed_version_prevents_package_creation(self):
        path = self.args.metadata / 'market-gateway.json'
        data = json.loads(path.read_text())
        data['image.name'] = data['image.name'].replace(self.args.version, 'different-version')
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'expected image and tag'):
            package.build(self.args)
        self.assertFalse(self.args.output.exists())

    def test_changed_file_in_transit_is_rejected(self):
        package.build(self.args)
        manifest = self.args.output / 'apps/api/resources.yaml'
        with manifest.open('a') as file:
            file.write('# modified after publication\n')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            verify.load_bundle(self.args.output)

    def test_metadata_digest_must_match_workload_images(self):
        package.build(self.args)
        index = self.args.output / 'release.json'
        release = json.loads(index.read_text())
        release['images']['market-api'] = 'sha256:' + 'f' * 64
        index.write_text(json.dumps(release))
        with self.assertRaisesRegex(ValueError, 'not bound to release metadata'):
            verify.load_bundle(self.args.output)

    def test_preflight_blocks_missing_image_and_wrong_tag(self):
        package.build(self.args)
        release, _ = verify.load_bundle(self.args.output)

        class FixtureRegistry:
            def digest(self, name, ref):
                image = name.split('/')[-1]
                if image == 'market-api':
                    raise HTTPError(name, 404, 'artifact missing', None, None)
                if image == 'market-web' and ref == release['version']:
                    return release['images']['market-api']
                return release['images'][image]

        issues = verify.check_images(release, 'target-project', FixtureRegistry())
        self.assertEqual({(x['image'], x['reason']) for x in issues},
                         {('market-api', 'missing'), ('market-web', 'digest_mismatch')})

    def test_duplicate_conflict_and_stale_notifications_do_not_replace_state(self):
        state = Path(self.temporary.name) / 'accepted.json'
        record = {'target': 'harbor.example/market', 'version': self.args.version, 'fingerprint': 'same'}
        self.assertEqual(verify.record_ready(state, record), 'ready')
        self.assertEqual(verify.record_ready(state, record), 'duplicate')
        self.assertEqual(verify.record_ready(state, {**record, 'fingerprint': 'different'}), 'version_conflict')
        self.assertEqual(verify.record_ready(state, {**record, 'version': '20261117123456-abcdef0'}), 'stale')
        self.assertEqual(json.loads(state.read_text()), record)


if __name__ == '__main__':
    unittest.main()
