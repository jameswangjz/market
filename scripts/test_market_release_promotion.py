"""Regress the failure boundaries of the Harbor-to-Flux promotion."""
from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from urllib.error import HTTPError

from market_release_bundle import IMAGES
from promote_market_release import GROUPS, Promotion, extract_bundle, utcnow

OLD = '20261119123151-9312b3f'
NEW = '20261120091650-e4b9cf4'
OLD_OCI = 'sha256:' + 'a' * 64
NEW_OCI = 'sha256:' + 'b' * 64
HOST = 'harbor.test:30443'
SOURCE_REPO = 'harbor.build:30443/market'


def fixture_release(tag, bit):
    return {'version': tag, 'source_repository': SOURCE_REPO,
            'images': {name: 'sha256:' + bit * 64 for name in IMAGES},
            'database_types': ['Postgres', 'Dameng']}


class RegistryFixture:
    registry = HOST
    versions = {'old': fixture_release(OLD, '1'), 'new': fixture_release(NEW, '2')}
    candidates = [OLD, NEW]
    missing = False

    def tags(self):
        return self.candidates

    def digest(self, repo, ref):
        if repo == 'market/market-release':
            return {OLD: OLD_OCI, NEW: NEW_OCI}[ref]
        if self.missing and repo == 'market/market-gateway':
            raise HTTPError(None, 404, 'missing', None, None)
        return 'sha256:' + '2' * 64

    def bundle(self, ref):
        return self.versions['old' if ref == OLD_OCI else 'new'], 'verified-fingerprint'


class ClusterFixture:
    def __init__(self):
        self.edits = []
        old = RegistryFixture.versions['old']
        self.objects = {
            ('source', None): self.resource({
                'url': f'oci://{HOST}/market/market-release', 'ref': {'digest': OLD_OCI}}),
            ('state', None): {'metadata': {'resourceVersion': '1'}, 'data': {'paused': 'false', 'state.json': '{}'}},
            ('lease', None): {'metadata': {'resourceVersion': '1'}, 'spec': {}},
        }
        for group in GROUPS:
            for stage in group:
                deps = {'workers': ['api'], 'web': ['api'], 'jobs': ['api', 'workers']}.get(stage, [])
                self.objects[('stage', stage)] = self.resource({
                    'targetNamespace': 'market', 'path': './apps/' + stage,
                    'sourceRef': {'kind': 'OCIRepository', 'name': 'market-release'},
                    'wait': True, 'dependsOn': [{'name': 'market-' + x} for x in deps],
                    'images': [{'name': f'{SOURCE_REPO}/{name}', 'newName': f'{HOST}/market/{name}',
                                'digest': digest} for name, digest in old['images'].items()],
                })

    def resource(self, spec):
        return {'metadata': {'resourceVersion': '1', 'generation': 1}, 'spec': spec,
                'status': self.ready_status(OLD_OCI, 1)}

    @staticmethod
    def ready_status(digest, gen):
        return {'observedGeneration': gen,
                'conditions': [{'type': 'Ready', 'status': 'True', 'observedGeneration': gen}],
                'artifact': {'revision': digest}, 'lastAppliedRevision': digest}

    def mark_ready(self, kind, obj=None):
        current = self.objects[kind, obj]
        current['status'] = self.ready_status(NEW_OCI, current['metadata']['generation'])

    def call(self, kind, obj=None):
        return deepcopy(self.objects[kind, obj])

    def patch(self, kind, previous, data, obj=None):
        current = self.objects[kind, obj]
        assert previous['metadata']['resourceVersion'] == current['metadata']['resourceVersion']
        for key, value in data.items():
            for field, item in value.items():
                if item is None:
                    current[key].pop(field, None)
                else:
                    current[key][field] = deepcopy(item)
        meta = current['metadata']
        meta['resourceVersion'] = str(int(meta['resourceVersion']) + 1)
        if kind in ('source', 'stage'):
            meta['generation'] += 1
        self.edits.append((kind, obj, deepcopy(data)))
        return self.call(kind, obj)

    def state(self):
        return json.loads(self.objects['state', None]['data']['state.json'])


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.cluster = ClusterFixture()
        self.registry = RegistryFixture()
        self.promoter = Promotion(self.cluster, self.registry, 'market', 'Postgres')

    def edits(self):
        return [e for e in self.cluster.edits if e[0] in ('source', 'stage')]

    def test_complete_release_moves_all_stages_in_dependency_order(self):
        self.promoter.run()
        edits = self.edits()
        self.assertEqual([x[0] for x in edits], ['stage'] * 4 + ['source'])
        self.assertTrue(all(x[2]['spec']['suspend'] for x in edits[:-1]))
        self.promoter.run()
        self.assertEqual(self.edits(), edits)
        self.cluster.mark_ready('source')
        self.promoter.run()
        self.assertEqual([x[1] for x in self.edits()[5:]], ['api'])
        self.promoter.run()  # A stale Ready condition must not release workers.
        self.assertEqual([x[1] for x in self.edits()[5:]], ['api'])
        self.cluster.mark_ready('stage', 'api')
        self.promoter.run()
        self.assertEqual([x[1] for x in self.edits()[5:]], ['api', 'workers', 'web'])
        self.cluster.mark_ready('stage', 'workers')
        self.cluster.mark_ready('stage', 'web')
        self.promoter.run()
        self.assertEqual([x[1] for x in self.edits()[5:]], ['api', 'workers', 'web', 'jobs'])
        self.cluster.mark_ready('stage', 'jobs')
        self.promoter.run()
        state = self.cluster.state()
        self.assertEqual(state['active']['digest'], NEW_OCI)
        self.assertEqual(state['rollback']['active']['digest'], OLD_OCI)
        self.assertNotIn('pending', state)
        self.promoter.run()
        self.assertEqual(len(self.edits()), 9)

    def test_missing_images_and_old_tags_never_change_flux(self):
        self.registry.missing = True
        self.promoter.run()
        self.assertEqual(self.edits(), [])
        self.assertNotIn('pending', self.cluster.state())
        self.registry.candidates = ['20261119110000-aaaaaaa', OLD]
        self.promoter.run()
        self.assertEqual(self.edits(), [])

    def test_rollback_hold_blocks_the_rejected_version(self):
        self.cluster.objects['state', None]['data']['skip-through'] = NEW
        self.promoter.run()
        self.assertEqual(self.edits(), [])
        self.assertNotIn('pending', self.cluster.state())

    def test_manual_image_drift_does_not_get_overwritten(self):
        self.registry.candidates = [OLD]
        self.promoter.run()
        self.cluster.objects['stage', 'api']['spec']['images'][0]['digest'] = NEW_OCI
        self.registry.candidates = [OLD, NEW]
        with self.assertRaisesRegex(ValueError, 'changed outside'):
            self.promoter.run()
        self.assertEqual(self.edits(), [])

    def test_pause_foreign_lease_and_incompatible_database(self):
        cm = self.cluster.objects['state', None]
        cm['data']['paused'] = 'true'
        self.promoter.run()
        self.assertEqual(self.edits(), [])
        cm['data']['paused'] = 'false'
        self.cluster.objects['lease', None]['spec'] = {'holderIdentity': 'other', 'renewTime': utcnow().isoformat(),
                                                       'leaseDurationSeconds': 180}
        self.promoter.run()
        self.assertEqual(self.edits(), [])
        self.cluster.objects['lease', None]['spec']['renewTime'] = (utcnow() - timedelta(minutes=4)).isoformat()
        self.promoter.database = 'Unsupported'
        with self.assertRaisesRegex(ValueError, 'compatibility'):
            self.promoter.run()
        self.assertEqual(self.edits(), [])

    def test_interrupted_prepare_resumes_and_timeout_blocks_downstream(self):
        patch = self.cluster.patch
        interrupted = False
        def with_failure(kind, current, payload, obj=None):
            nonlocal interrupted
            if kind == 'stage' and obj == 'workers' and not interrupted:
                interrupted = True
                raise RuntimeError('Interrupted')
            return patch(kind, current, payload, obj)
        self.cluster.patch = with_failure
        with self.assertRaisesRegex(RuntimeError, 'Interrupted'):
            self.promoter.run()
        self.assertEqual(self.cluster.call('source')['spec']['ref']['digest'], OLD_OCI)
        self.promoter.run()
        self.assertEqual(self.cluster.call('source')['spec']['ref']['digest'], NEW_OCI)
        cm = self.cluster.objects['state', None]
        state = self.cluster.state()
        state['pending']['timeout'] = (utcnow() - timedelta(seconds=1)).isoformat()
        cm['data']['state.json'] = json.dumps(state)
        self.promoter.run()
        self.assertEqual(self.cluster.state()['pending']['blocked'], 'flux_readiness_timeout')
        self.assertTrue(all(self.cluster.call('stage', stage)['spec']['suspend'] for group in GROUPS for stage in group))

    def test_rejects_tar_links_and_traversal(self):
        for name, kind in (('release.json', tarfile.SYMTYPE), ('../release.json', tarfile.REGTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                memory = io.BytesIO()
                with tarfile.open(fileobj=memory, mode='w:gz') as archive:
                    item = tarfile.TarInfo(name)
                    item.type = kind
                    archive.addfile(item)
                with self.assertRaises(ValueError):
                    extract_bundle(memory.getvalue(), Path(tmp))


if __name__ == '__main__':
    unittest.main()
