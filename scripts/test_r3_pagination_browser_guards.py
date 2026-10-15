"""Offline checks of the pager's actual embedded GET policy and response guard."""
import json
from pathlib import Path
import subprocess
import unittest


class PaginationGuards(unittest.TestCase):
    def test_javascript_syntax_and_guarded_public_contract(self):
        source = Path(__file__).with_name('r3_pagination_browser_cdp.ps1').read_text()
        probe = source.split("$probe = @'\n", 1)[1].split("\n'@", 1)[0]
        checked = subprocess.run(['node', '--check'], input=probe, text=True, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        definitions = probe.split('(async()=>{\n  try{', 1)[0]
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const query='qa-r3-page-0123456789ab',origin='http://192.168.10.10:30080';
function context(q=query){const c={URL,process:{argv:['node','probe',q]},require:()=>({})};vm.createContext(c);return c;}
const c=context();vm.runInContext(DEFINITIONS,c);
const allow=(method,path,headers={})=>vm.runInContext('allowedRequest('+JSON.stringify({method,url:path,headers})+')',c);
for(const size of [10,20])for(const page of [1,2]){
 assert(allow('GET',origin+'/?q='+query+'&page_size='+size+'&page='+page));
 assert(allow('GET',origin+'/api/storefront/products?q='+query+'&page_size='+size+'&page='+page+'&product_type=&parent_id=&category_id='));
}
assert(allow('GET',origin+'/api/storefront/categories'));
assert(allow('GET',origin+'/assets/Storefront-owned.js'));
assert(allow('GET',origin+'/?q='+query+'-missing&page_size=20'));
for(const path of ['/?q=foreign&page_size=10','/','/console','/api/products','/api/auth/me',
 '/api/storefront/products?q='+query+'&page_size=10&q=foreign',
 '/api/storefront/products?q='+query+'&page_size=10&enterprise_id=foreign',
 '/api/storefront/products?q='+query+'&page_size=10&product_type=training',
 '/api/storefront/products?q='+query+'&page_size=100',
 '/api/storefront/products?q='+query+'&page_size=10&page=3',
 '/api/storefront/products/foreign/logo'])assert(!allow('GET',origin+path));
assert(!allow('GET','https://example.invalid/assets/a.js'));
assert(!allow('GET','http://user@192.168.10.10:30080/assets/a.js'));
assert(!allow('GET',origin+'/assets/a.js',{Authorization:'Bearer private'}));
for(const method of ['POST','PUT','PATCH','DELETE','HEAD','OPTIONS'])
 assert(!allow(method,origin+'/api/storefront/categories'));
function page(q,size,index,count,total){return {total,page:index,page_size:size,
 items:Array.from({length:count},(_,i)=>({id:'owned-'+((index-1)*size+i),name:q+'-training-'+((index-1)*size+i)}))};}
function validate(body,q=query,size=10,index=1){
 const u=origin+'/api/storefront/products?q='+q+'&page_size='+size+'&page='+index;
 return vm.runInContext('validatePage('+JSON.stringify(body)+',new URL('+JSON.stringify(u)+'))',c);
}
validate(page(query,10,1,10,12));validate(page(query,10,2,2,12),query,10,2);
validate(page(query,20,1,12,12),query,20,1);
validate(page(query+'-missing',20,1,0,0),query+'-missing',20,1);
assert(allow('GET',origin+'/api/storefront/products/owned-0/logo'));
for(const change of [{total:11},{page:2},{page_size:20},{items:[]},
 {items:page(query,10,1,10,12).items.map((p,i)=>i===0?{...p,name:'foreign'}:p)},
 {items:page(query,10,1,10,12).items.map((p,i)=>i===0?{...p,id:'qa-vcq-a044ea9ea181-p2'}:p)},
 {items:Array(10).fill({id:'duplicate',name:query+'-duplicate'})}])
 assert.throws(()=>validate({...page(query,10,1,10,12),...change}));
for(const q of ['qa-r3-page-*','qa-r3-page-ABCDEF123456','qa-r3-page-123','foreign'])
 assert.throws(()=>vm.runInContext(DEFINITIONS,context(q)));
'''.replace('DEFINITIONS', json.dumps(definitions))
        result = subprocess.run(['node'], input=harness, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
