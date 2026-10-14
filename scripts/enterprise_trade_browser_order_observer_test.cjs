const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const expected = {origin:'http://fixture.invalid',product:'owned-p',version:'owned-v',enterprise:'owned-e',buyer:'owned-u'};
const good = {id:'a'.repeat(32),product_id:expected.product,product_version_id:expected.version,
  buyer_enterprise_id:expected.enterprise,buyer_user_id:expected.buyer,order_no:'owned-no',secret:'must-not-store'};
let payload = good, status = 200, clones = 0, stored, sends = 0;
const realResponse = {get status(){return status;}, clone(){clones++;return {json:async()=>payload};}};
class XHR {
  open() {}
  send() {sends++;}
  addEventListener(name, callback) {assert.equal(name,'load');this.loaded=callback;}
}
const context = {URL, WeakMap, XMLHttpRequest:XHR,location:{href:expected.origin+'/products/owned-p'},
  sessionStorage:{setItem(key,value){assert.equal(key,'market_enterprise_trade_order');stored=JSON.parse(value);}}};
context.window=context;
context.fetch=async()=>realResponse;
vm.createContext(context);
const source=fs.readFileSync(__dirname+'/enterprise_trade_browser_order_observer.js','utf8');
vm.runInContext(source+'('+JSON.stringify(expected)+');',context);
(async()=>{
  const returned=await context.fetch('/api/orders',{method:'POST'});
  assert.equal(returned,realResponse);assert.equal(clones,1);assert.deepEqual(stored,{id:good.id});
  for(const [key,value] of [['product_id','foreign'],['product_version_id','foreign'],
      ['buyer_enterprise_id','foreign'],['buyer_user_id','foreign'],['id','bad'],['order_no','ORD-261012005144E1B3']]) {
    stored=undefined;payload={...good,[key]:value};
    await context.fetch('/api/orders',{method:'POST'});assert.equal(stored,undefined,key);
  }
  payload=good;
  for(const url of ['http://foreign.invalid/api/orders','/api/orders?extra=1','/api/orders/quote']) {
    stored=undefined;await context.fetch(url,{method:'POST'});assert.equal(stored,undefined,url);
  }
  status=403;stored=undefined;await context.fetch('/api/orders',{method:'POST'});assert.equal(stored,undefined);
  status=200;
  const xhr=new context.XMLHttpRequest();xhr.open('POST','/api/orders');xhr.send();
  xhr.status=200;xhr.responseText=JSON.stringify(good);xhr.loaded();
  assert.equal(sends,1);assert.deepEqual(stored,{id:good.id});
  const wrapped=context.fetch;vm.runInContext(source+'('+JSON.stringify(expected)+');',context);
  assert.equal(context.fetch,wrapped);
  const quote={product_id:expected.product,product_version_id:expected.version,subscription_months:1,quote_id:'b'.repeat(64)};
  const body={product_id:expected.product,product_version_id:expected.version,buyer_enterprise_id:expected.enterprise,subscription_months:1};
  payload=quote;
  await context.fetch('/api/orders/quote',{method:'POST',body:JSON.stringify(body)});
  assert.equal(context.__enterpriseTradeQuoteId,quote.quote_id);
  for(const key of ['product_id','product_version_id','buyer_enterprise_id']) {
    context.__enterpriseTradeQuoteId=undefined;
    await context.fetch('/api/orders/quote',{method:'POST',body:JSON.stringify({...body,[key]:'foreign'})});
    assert.equal(context.__enterpriseTradeQuoteId,undefined,key);
  }
  const quoteXhr=new context.XMLHttpRequest();quoteXhr.open('POST','/api/orders/quote');quoteXhr.send(JSON.stringify(body));
  quoteXhr.status=200;quoteXhr.responseText=JSON.stringify(quote);quoteXhr.loaded();
  assert.equal(context.__enterpriseTradeQuoteId,quote.quote_id);
  console.log('passed: exact real-response observer capture, rejection boundaries, ID-only storage, registration idempotence');
})().catch(error=>{console.error(error);process.exitCode=1;});
