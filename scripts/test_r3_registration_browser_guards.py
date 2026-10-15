"""Offline scope and actual embedded request-policy checks; never imports app."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

import r3_registration_browser_scope as scope


class ScopeGuards(unittest.TestCase):
    def test_private_manifest_round_trip_and_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            value = scope.ids('qa-r3-reg-0123456789ab')
            scope.safe.private_json(path, value, create=True)
            self.assertEqual(scope.read_scope(path), value)
            for key, replacement in [('user', 'other'), ('enterprise', 'other'),
                                     ('products', ['qa-vcq-a044ea9ea181-p2']),
                                     ('origin', 'http://example.invalid'), ('run_id', None)]:
                with self.subTest(key=key):
                    scope.safe.private_json(path, {**value, key: replacement})
                    with self.assertRaises(scope.safe.AcceptanceFailure):
                        scope.read_scope(path)

    def test_private_file_permissions_links_and_overwrite_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            scope.safe.private_json(path, scope.ids(), create=True)
            with self.assertRaises(FileExistsError):
                scope.safe.private_json(path, {}, create=True)
            os.chmod(path, 0o644)
            with self.assertRaises(scope.safe.AcceptanceFailure):
                scope.read_scope(path)
            link = Path(directory) / 'link.json'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                scope.read_scope(link)

    def test_reject_noncanonical_scope_and_paths(self):
        for run in ['qa-r3-reg-*', 'ORD-261012005144E1B3', 'qa-r3-reg-abc', 'qa-vcq-0123456789ab']:
            with self.subTest(run=run), self.assertRaises(scope.safe.AcceptanceFailure):
                scope.ids(run)
        with self.assertRaises(scope.safe.AcceptanceFailure):
            scope.paths('/tmp/nested/manifest.json')

    def test_embedded_javascript_syntax_and_actual_guard(self):
        source = Path(__file__).with_name('r3_registration_browser_cdp.ps1').read_text()
        probe = source.split("$probe = @'\n", 1)[1].split("\n'@", 1)[0]
        syntax = subprocess.run(['node', '--check'], input=probe, text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        # Evaluate only definitions, using a fake private fixture; no browser/network code.
        definitions = probe.split('(async()=>{\n  try {', 1)[0]
        fixture = dict(**scope.ids('qa-r3-reg-0123456789ab'),
                       email='qa-r3-reg-0123456789ab-u@example.invalid', password='x'*40)
        harness = """
const vm=require('node:vm'),assert=require('node:assert/strict');
const sandbox={URL,require:()=>({readFileSync:()=>JSON.stringify(FIXTURE)}),process:{argv:[]}};
vm.createContext(sandbox);vm.runInContext(DEFINITIONS,sandbox);
const allowed=(method,path,body,count=0)=>vm.runInContext(
 'requestAllowed('+JSON.stringify({method,url:path,postData:body&&JSON.stringify(body)})+',fixture,'+count+')',sandbox);
const origin='http://192.168.10.10:30080';
assert(allowed('GET',origin+'/console?view=products'));
assert(allowed('GET',origin+'/api/products'));
for(const path of ['/api/notifications/stream','/api/notifications/sender-context']){
 assert(allowed('GET',origin+path));
 for(const method of ['HEAD','POST','PUT','PATCH','DELETE'])assert(!allowed(method,origin+path));
 assert(!allowed('GET','https://example.invalid'+path));
}
assert(!allowed('GET',origin+'/api/notifications/stream/foreign'));
assert(allowed('POST',origin+'/api/auth/login',{email:FIXTURE.email,password:FIXTURE.password}));
assert(!allowed('POST',origin+'/api/auth/login',{email:FIXTURE.email,password:FIXTURE.password},1));
assert(!allowed('POST',origin+'/api/auth/login',{email:'foreign',password:FIXTURE.password}));
assert(!allowed('POST',origin+'/api/auth/login',{email:FIXTURE.email,password:FIXTURE.password,identifier:'foreign'}));
for(const method of ['POST','PUT','PATCH','DELETE']){
 for(const path of ['/api/products','/api/files/upload','/api/products/qa-vcq-a044ea9ea181-p2','/api/orders/ORD-261012005144E1B3'])
  assert(!allowed(method,origin+path));
}
for(const path of ['https://example.invalid/assets/a.js','http://192.168.10.10:30081/api/products',
 origin+'/api/products/foreign/oauth-credentials-download',origin+'/api/admin/settings',
 'http://user@192.168.10.10:30080/api/products'])assert(!allowed('GET',path));
const payload={name:FIXTURE.product_name,product_type:'training',delivery_method:'training',
 provider_name:FIXTURE.enterprise,description:FIXTURE.run_id+' synthetic training',category_id:'existing-category',
 authorization_conditions:FIXTURE.run_id+' isolated QA only',oidc_redirect_uris:[],logo_file_id:'',
 settlement_rule_mode:'custom',settlement_rule_id:'',
 settlement_rule:{platform_rate:0,provider_rate:100,service_rate:0,expert_rate:0,channel_rate:0},
 versions:[{version_code:'v1.0',description:FIXTURE.run_id+' training version',price:12,cost:7,status:'active'}]};
const s={identity:true,armed:true,creates:0,uploads:0,updates:0,product:null,file:null,payload};
const mutate=(method,path,body,headers={})=>vm.runInContext(
 'requestAllowed('+JSON.stringify({method,url:origin+path,postData:JSON.stringify(body),headers})+
 ',fixture,1,'+JSON.stringify(s)+')',sandbox);
assert(mutate('POST','/api/products',payload));
for(const patch of [{name:'foreign'},{product_type:'api'},{delivery_method:'file'},
 {provider_name:'foreign'},{enterprise_id:'foreign'},{settlement_rule_mode:'global'},
 {logo_file_id:'shared'},{versions:[...payload.versions,...payload.versions]}])
 assert(!mutate('POST','/api/products',{...payload,...patch}));
assert(!mutate('POST','/api/products',payload,{'X-Enterprise-Id':'foreign'}));
vm.runInContext('blocked=1',sandbox);
assert(!mutate('POST','/api/products',payload));
assert.throws(()=>vm.runInContext('armWrites()',sandbox),e=>e.code==='bootstrap_request_blocked');
assert.equal(vm.runInContext('state.armed',sandbox),false);
vm.runInContext('blocked=0',sandbox);
s.creates=1;s.product='a'.repeat(32);
assert(!mutate('POST','/api/products',payload));
s.uploadIntent={product:s.product,role:'product_logo',name:FIXTURE.logo_name,type:'image/svg+xml',
 content:vm.runInContext('logoText',sandbox)};
assert(mutate('POST','/api/files/upload',null,{'Content-Type':'multipart/form-data; boundary=owned'}));
for(const [key,value] of [['product','foreign'],['name','foreign.svg'],['role','product_data'],['content','wrong bytes']]){
 const previous=s.uploadIntent[key];s.uploadIntent[key]=value;
 assert(!mutate('POST','/api/files/upload',null,{'Content-Type':'multipart/form-data; boundary=owned'}));
 s.uploadIntent[key]=previous;
}
s.uploads=1;s.file='b'.repeat(32);
assert(!mutate('POST','/api/files/upload',null,{'Content-Type':'multipart/form-data; boundary=owned'}));
assert(mutate('PUT','/api/products/'+s.product,{...payload,logo_file_id:s.file}));
assert(!mutate('PUT','/api/products/foreign',{...payload,logo_file_id:s.file}));
assert(!mutate('PUT','/api/products/'+s.product,{...payload,logo_file_id:'shared'}));
s.updates=1;
assert(!mutate('PUT','/api/products/'+s.product,{...payload,logo_file_id:s.file}));
""".replace('DEFINITIONS', json.dumps(definitions)).replace('FIXTURE', json.dumps(fixture))
        result = subprocess.run(['node'], input=harness, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


try:
    import sqlalchemy as sa
    from sqlalchemy.orm import declarative_base, sessionmaker
except ImportError:
    sa = None


@unittest.skipIf(sa is None, 'Run with existing /tmp/market-r3-python312/bin/python for SQLite ownership checks')
class DatabaseOwnershipGuards(unittest.TestCase):
    def setUp(self):
        self.base = declarative_base()

        def model(name, columns):
            values = dict(__tablename__=name.lower(), id=sa.Column(sa.String, primary_key=True))
            values.update({key: sa.Column(sa.String, *([sa.ForeignKey(foreign)] if foreign else []))
                           for key, foreign in columns.items()})
            return type(name, (self.base,), values)

        user = model('User', dict(name=None, email=None, platform_role=None))
        enterprise = model('Enterprise', dict(name=None, credit_code=None))
        membership = model('Membership', dict(user_id='user.id', enterprise_id='enterprise.id',
                           role=None, status=None, business_roles=None))
        product = model('Product', dict(enterprise_id='enterprise.id', name=None, product_type=None,
                        delivery_method=None, status=None, provider_name=None, description=None,
                        authorization_conditions=None, logo_file_id=None, logo_thumbnail_file_id=None))
        version = model('ProductReleaseVersion', dict(product_id='product.id', version_code=None,
                        description=None, price=None, cost=None, status=None))
        file = model('FileObject', dict(owner_id=None, product_id='product.id',
                     version_id='productreleaseversion.id', file_role=None, original_name=None,
                     content_type=None, checksum=None, object_name=None))
        # Size is numeric in production and must compare as such.
        file.size = sa.Column(sa.Integer)
        audit = model('AuditLog', dict(actor=None, target_id=None, order_id=None, action=None))
        order = model('Order', dict(product_id='product.id', buyer_user_id='user.id',
                      buyer_enterprise_id='enterprise.id', provider_enterprise_id='enterprise.id'))
        preference = model('Preference', dict(user_id='user.id'))
        engine = sa.create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        self.base.metadata.create_all(engine)
        self.m = SimpleNamespace(Base=self.base, SessionLocal=sessionmaker(engine), User=user,
            Enterprise=enterprise, Membership=membership, Product=product,
            ProductReleaseVersion=version, FileObject=file, AuditLog=audit, Order=order,
            message_center={'Preference': preference})
        self.scope = scope.ids('qa-r3-reg-0123456789ab')
        self.db = self.m.SessionLocal()
        self.addCleanup(self.db.close)
        uid, eid = self.scope['user'], self.scope['enterprise']
        self.db.add_all([user(id=uid, name=uid, email=uid+'@example.invalid', platform_role=''),
                        enterprise(id=eid, name=eid, credit_code=eid)])
        self.db.flush()
        self.db.add(membership(id=self.scope['membership'], user_id=uid, enterprise_id=eid,
                              role='super_admin', status='active', business_roles='provider,user'))
        self.db.add(product(id='p', enterprise_id=eid, name=self.scope['product_name'],
                    product_type='training', delivery_method='training', status='draft', provider_name=eid,
                    description=self.scope['run_id']+' synthetic training',
                    authorization_conditions=self.scope['run_id']+' isolated QA only',
                    logo_file_id='f', logo_thumbnail_file_id=''))
        self.db.flush()
        self.db.add(version(id='v', product_id='p', version_code='v1.0',
                    description=self.scope['run_id']+' training version', price='12', cost='7', status='active'))
        self.db.add(file(id='f', owner_id=uid, product_id='p', file_role='product_logo',
                    original_name=self.scope['logo_name'], content_type='image/svg+xml',
                    size=len(scope.logo_text().encode()),
                    checksum=scope.hashlib.sha256(scope.logo_text().encode()).hexdigest(),
                    object_name=uid+'/20261015/0123456789ab-'+self.scope['logo_name']))
        self.db.commit()

    def test_exact_owned_product_version_logo_keys(self):
        owned = scope.owned_rows(self.m, self.db, self.scope)
        self.assertEqual(owned['product'], {('p',)})
        self.assertEqual(owned['productreleaseversion'], {('v',)})
        self.assertEqual(owned['fileobject'], {('f',)})

    def test_orders_refuse_cleanup(self):
        self.db.add(self.m.Order(id='unexpected-order', product_id='p'))
        self.db.commit()
        with self.assertRaisesRegex(scope.safe.AcceptanceFailure, 'Unexpected order'):
            scope.owned_rows(self.m, self.db, self.scope)

    def test_foreign_logo_reference_refuses_cleanup(self):
        self.db.add(self.m.Product(id='foreign', enterprise_id='foreign-enterprise', logo_file_id='f'))
        self.db.commit()
        with self.assertRaisesRegex(scope.safe.AcceptanceFailure, 'Foreign logo reference'):
            scope.owned_rows(self.m, self.db, self.scope)

    def test_unexpected_second_product_refuses_cleanup(self):
        self.db.add(self.m.Product(id='unexpected', enterprise_id=self.scope['enterprise']))
        self.db.commit()
        with self.assertRaisesRegex(scope.safe.AcceptanceFailure, 'More than one product'):
            scope.owned_rows(self.m, self.db, self.scope)

    def test_changed_object_or_file_owner_refuses_cleanup(self):
        item = self.db.get(self.m.FileObject, 'f')
        item.object_name = 'shared-businessdata/logo.svg'
        self.db.commit()
        with self.assertRaisesRegex(scope.safe.AcceptanceFailure, 'Unexpected file owner/object'):
            scope.owned_rows(self.m, self.db, self.scope)


if __name__ == '__main__':
    unittest.main()
