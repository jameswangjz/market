param(
    [Parameter(Mandatory=$true)][string]$FixtureFile,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [string]$NodePath = 'node',
    [string]$ChromePath = 'C:\Program Files\Google\Chrome\Application\chrome.exe',
    [switch]$OfflineSyntaxCheck
)
$ErrorActionPreference = 'Stop'
# Node 22+ supplies native WebSocket. No npm dependencies or remote scripts.
$probe = @'
'use strict';
const fs = require('node:fs');
const origin = 'http://192.168.10.10:30080';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const check = (ok, message, code='assertion_failed') => { if (!ok) throw Object.assign(Error(message),{safe:true,code}); };
let fixture;
try { fixture=JSON.parse(fs.readFileSync(process.argv[2], 'utf8').replace(/^\uFEFF/, '')); }
catch { throw Error('Cannot read private fixture'); }
check(/^qa-r3-reg-[0-9a-f]{12}$/.test(fixture.run_id), 'Invalid run');
check(fixture.user === fixture.run_id+'-u' && fixture.enterprise === fixture.run_id+'-e' &&
  fixture.email === fixture.user+'@example.invalid' && fixture.origin === origin &&
  fixture.mode === 'single-training-save' && fixture.product_name===fixture.run_id+'-training' &&
  fixture.logo_name===fixture.run_id+'-logo.svg' &&
  typeof fixture.password === 'string' && fixture.password.length >= 32, 'Invalid scope');
let socket, serial = 0, loginCount = 0, blocked = 0, productAttempts = 0;
const pending = new Map();
let fatal;
let stage='fixture_setup';
const stages=new Set(['fixture_setup','chrome_connect','bootstrap_login','provider_identity',
  'draft_validation','complete_form','logo_input','required_field_validation','arm_writes','single_save','persisted_readback']);
const errorCodes=new Set(['assertion_failed','bootstrap_request_blocked','cdp_timeout',
  'browser_condition_timeout','request_guard_failed','browser_operation_failed']);
const logoText='<svg xmlns="http://www.w3.org/2000/svg" width="380" height="280" viewBox="0 0 380 280"><rect width="380" height="280" fill="#147d64"/></svg>';
const state={identity:false,armed:false,creates:0,uploads:0,updates:0,product:null,file:null,
  versions:[],object:null,payload:null,uploadIntent:null};
let receiptFd;
function journal() {
  const body=JSON.stringify({run_id:fixture.run_id,enterprise:fixture.enterprise,
    product_name:fixture.product_name,product:state.product,versions:state.versions,file:state.file,
    object:state.object,
    creates:state.creates,uploads:state.uploads,updates:state.updates});
  fs.ftruncateSync(receiptFd,0);fs.writeSync(receiptFd,body,0,'utf8');fs.fsyncSync(receiptFd);
}
const report = {status:'failed', mode:'single-training-save', origin, checks:[],
  uncovered:['valid public page-2 jump', 'public page-2 F5',
    'public page-size reset', 'foreign-provider 404'], blockedRoutes:[]};
function cdp(method, params={}) {
  return new Promise((resolve,reject) => {
    const id=++serial, timer=setTimeout(()=>{pending.delete(id);reject(Object.assign(Error('CDP timeout'),{code:'cdp_timeout'}));},15000);
    pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));
  });
}
async function js(expression) {
  if(fatal) throw fatal;
  const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
  check(!r.exceptionDetails,'Browser evaluation failed');return r.result.value;
}
async function wait(expression) {
  for(let i=0;i<100;i++){if(await js(expression))return;await sleep(100);}
  throw Object.assign(Error('Browser condition timed out'),{code:'browser_condition_timeout'});
}
async function click(selector) {
  const p=await js(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});
    if(!n)return null;n.scrollIntoView({block:'center'});const r=n.getBoundingClientRect();
    return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  check(p,'Missing click target');
  await cdp('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...p});
  await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...p});
}
function validPayload(body, fixture) {
  const fields='name product_type catalog_name category_id provider_name provider_type description usage_scenarios delivery_method upstream_url application_url integration_api_url oidc_redirect_uris download_limit logo_file_id price pricing_strategy version quality_level security_level authorization_conditions data_source_statement compliance_statement settlement_rule_mode settlement_rule_id settlement_rule versions'.split(' ');
  const vf='version_code description compatibility_mode compatibility_notes price cost rate_limit_per_minute daily_quota monthly_quota quota_unit quota_amount status'.split(' ');
  const v=body?.versions?.[0],rule=body?.settlement_rule;
  return body && Object.keys(body).every(k=>fields.includes(k)) &&
    body.name===fixture.product_name && body.product_type==='training' && body.delivery_method==='training' &&
    body.provider_name===fixture.enterprise && body.description===fixture.run_id+' synthetic training' &&
    body.authorization_conditions===fixture.run_id+' isolated QA only' &&
    typeof body.category_id==='string' && body.category_id.length>0 &&
    !body.upstream_url && !body.application_url && !body.integration_api_url &&
    Array.isArray(body.oidc_redirect_uris) && body.oidc_redirect_uris.length===0 &&
    body.settlement_rule_mode==='custom' && !body.settlement_rule_id && rule &&
    Object.keys(rule).length===5 && rule.platform_rate===0 && rule.provider_rate===100 &&
    rule.service_rate===0 && rule.expert_rate===0 && rule.channel_rate===0 &&
    Array.isArray(body.versions) && body.versions.length===1 && v && Object.keys(v).every(k=>vf.includes(k)) &&
    v.version_code==='v1.0' && v.description===fixture.run_id+' training version' &&
    v.price===12 && v.cost===7 && v.status==='active';
}
function requestAllowed(r, fixture, loginCount, s=state) {
  const u=new URL(r.url);let allow=false;
  if(u.origin===origin && !u.username && !u.password) {
    const enterpriseHeader=Object.entries(r.headers||{}).find(([k])=>k.toLowerCase()==='x-enterprise-id')?.[1];
    if((enterpriseHeader && enterpriseHeader!==fixture.enterprise) ||
      (u.searchParams.has('enterprise_id') && u.searchParams.get('enterprise_id')!==fixture.enterprise))return false;
    // Reads are restricted to console bootstrap, static assets and registration metadata.
    allow=['GET','HEAD'].includes(r.method) && (u.pathname==='/console' ||
      /^\/assets\/[A-Za-z0-9_.-]+$/.test(u.pathname) || u.pathname==='/favicon.ico' ||
      /^\/api\/(auth\/me|products|product-categories|settlement-rules|settings\/application-address-policy|notifications\/summary|storefront\/buyer-enterprises)$/.test(u.pathname));
    if(r.method==='GET' && ['/api/notifications/stream','/api/notifications/sender-context'].includes(u.pathname))allow=true;
    if(r.method==='POST' && u.pathname==='/api/auth/login' && loginCount===0) {
      let body;try{body=JSON.parse(r.postData||'');}catch{}
      allow=body && body.email===fixture.email && body.password===fixture.password &&
        Object.keys(body).every(k=>['email','password'].includes(k));
    }
    if(s.identity && s.armed && blocked===0 && !u.search && ['POST','PUT'].includes(r.method)) {
      let body;try{body=JSON.parse(r.postData||'');}catch{}
      if(r.method==='POST' && u.pathname==='/api/products')
        allow=s.creates===0 && validPayload(body,fixture) && !body.logo_file_id;
      if(r.method==='POST' && u.pathname==='/api/files/upload') {
        const i=s.uploadIntent;
        allow=s.creates===1 && s.product && s.uploads===0 && i?.product===s.product &&
          i.role==='product_logo' && i.name===fixture.logo_name && i.type==='image/svg+xml' &&
          i.content===logoText && /multipart\/form-data;\s*boundary=/i.test(
            Object.entries(r.headers||{}).find(([k])=>k.toLowerCase()==='content-type')?.[1]||'');
      }
      if(r.method==='PUT' && s.product && u.pathname==='/api/products/'+s.product)
        allow=s.uploads===1 && s.file && s.updates===0 && validPayload(body,fixture) &&
          body.logo_file_id===s.file && Object.keys({...s.payload,...body}).every(k=>
            ['logo_file_id','catalog_name'].includes(k) || JSON.stringify(body[k])===JSON.stringify(s.payload[k]));
    }
  }
  return Boolean(allow);
}
function armWrites(){
  check(blocked===0,'Bootstrap guard blocked requests; writes remain disarmed','bootstrap_request_blocked');
  state.armed=true;
}
async function pauseRequest(p) {
  const r=p.request, u=new URL(r.url);
  if(p.responseStatusCode!==undefined) {
    if(['POST','PUT'].includes(r.method)) {
      check(p.responseStatusCode===200,'Owned mutation rejected');
      const raw=await cdp('Fetch.getResponseBody',{requestId:p.requestId});
      const body=JSON.parse(raw.base64Encoded?Buffer.from(raw.body,'base64').toString('utf8'):raw.body);
      if(u.pathname==='/api/products' && r.method==='POST') {
        check(/^[0-9a-f]{32}$/.test(body.id) && body.enterprise_id===fixture.enterprise &&
          body.name===fixture.product_name && body.product_type==='training' &&
          body.delivery_method==='training' && body.status==='draft' && body.versions?.length===1 &&
          /^[0-9a-f]{32}$/.test(body.versions[0].id),'Unexpected created identity');
        state.product=body.id;state.versions=body.versions.map(v=>v.id);
      } else if(u.pathname==='/api/files/upload') {
        check(/^[0-9a-f]{32}$/.test(body.id) && body.product_id===state.product &&
          body.file_role==='product_logo' && body.original_name===fixture.logo_name &&
          body.size===Buffer.byteLength(logoText) && typeof body.object_name==='string' &&
          body.object_name.startsWith(fixture.user+'/') && body.object_name.endsWith('-'+fixture.logo_name),
          'Unexpected uploaded identity');
        state.file=body.id;state.object=body.object_name;
      } else if(r.method==='PUT') {
        check(body.id===state.product && body.enterprise_id===fixture.enterprise &&
          body.logo_file_id===state.file,'Unexpected updated identity');
      }
      journal();
    }
    await cdp('Fetch.continueRequest',{requestId:p.requestId});return;
  }
  if(r.method==='POST' && u.pathname==='/api/products')productAttempts++;
  if(r.method==='POST' && u.pathname==='/api/files/upload')
    state.uploadIntent=await js('window.__r3UploadIntent || null');
  const allow=requestAllowed(r,fixture,loginCount);
  if(allow && r.method==='POST' && u.pathname==='/api/auth/login')loginCount++;
  if(allow && r.method==='POST' && u.pathname==='/api/products') {
    state.creates++;state.payload=JSON.parse(r.postData);journal();
  }
  if(allow && r.method==='POST' && u.pathname==='/api/files/upload'){state.uploads++;journal();}
  if(allow && r.method==='PUT'){state.updates++;journal();}
  if(!allow){blocked++;
    if(report.blockedRoutes.length<30)report.blockedRoutes.push({method:r.method,path:u.origin===origin?u.pathname:'external'});}
  await cdp(allow?'Fetch.continueRequest':'Fetch.failRequest',
    allow?{requestId:p.requestId}:{requestId:p.requestId,errorReason:'BlockedByClient'});
}
(async()=>{
  try {
    receiptFd=fs.openSync(process.argv[2]+'.receipt.json','wx',0o600);journal();
    const logoPath=process.argv[5]+'/'+fixture.logo_name;
    fs.writeFileSync(logoPath,logoText,{flag:'wx',mode:0o600});
    stage='chrome_connect';
    let target;
    for(let i=0;i<60;i++){
      try{target=(await(await fetch(`http://127.0.0.1:${process.argv[3]}/json/list`)).json()).find(x=>x.type==='page');}catch{}
      if(target)break;await sleep(200);
    }
    check(target,'Chrome unavailable');
    socket=new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=()=>reject(Error('CDP unavailable'));});
    socket.onmessage=event=>{
      const msg=JSON.parse(event.data);
      if(msg.id){const p=pending.get(msg.id);if(p){clearTimeout(p.timer);pending.delete(msg.id);
        msg.error?p.reject(Error('CDP failed')):p.resolve(msg.result);}return;}
      if(msg.method==='Fetch.requestPaused')pauseRequest(msg.params).catch(e=>{fatal=e.safe?e:Object.assign(Error('Guard failed'),{code:'request_guard_failed'});socket.close();});
      if(msg.method==='Page.javascriptDialogOpening'){
        fatal=Error('Unexpected native dialog');cdp('Page.handleJavaScriptDialog',{accept:false}).catch(()=>{});
      }
    };
    await cdp('Page.enable');await cdp('Runtime.enable');
    await cdp('Browser.setDownloadBehavior',{behavior:'deny'});
    await cdp('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'},
      ...['products*','files/upload','auth/login'].map(p=>({urlPattern:origin+'/api/'+p,requestStage:'Response'}))]});
    stage='bootstrap_login';
    await cdp('Page.navigate',{url:origin+'/console?view=products'});
    await wait('!!document.querySelector(".login-card input[type=password]")');
    await js(`(()=>{const f=document.querySelector('.login-card'),inputs=f.querySelectorAll('input');
      const email=[...inputs].find(n=>n.type!=='password'),password=f.querySelector('input[type=password]');
      for(const [n,v] of [[email,${JSON.stringify(fixture.email)}],[password,${JSON.stringify(fixture.password)}]]){
        n.value=v;n.dispatchEvent(new Event('input',{bubbles:true}));}return true;})()`);
    await click('.login-card button[type=submit]');
    await wait('!document.querySelector(".login-card") && !!document.querySelector(".app-shell")');
    stage='provider_identity';
    const identity=await js(`(async()=>{const r=await fetch('/api/auth/me',{headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}});if(!r.ok)return null;return r.json();})()`);
    // Fail closed if the served auth/me contract differs.
    check(identity?.user?.id===fixture.user && !identity.user.platform_role &&
      identity.enterprise?.id===fixture.enterprise && identity.role==='super_admin' &&
      identity.membership_status==='active' && identity.enterprise.verification_status==='verified' &&
      identity.business_roles?.includes('provider'),'Unexpected provider identity/contract');
    report.checks.push('owned provider password login');
    state.identity=true;
    await wait(`[...document.querySelectorAll('button')].some(n=>n.textContent.trim()==='\u65b0\u5efa\u4ea7\u54c1')`);
    check(blocked===0,'Bootstrap guard blocked requests; writes remain disarmed','bootstrap_request_blocked');
    {
      stage='draft_validation';
      const width=1440;
      await cdp('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:width===390});
      await js(`(()=>{const b=[...document.querySelectorAll('button')].find(n=>n.textContent.trim()==='\u65b0\u5efa\u4ea7\u54c1');if(!b)return false;b.click();return true;})()`);
      await wait('!!document.querySelector(".product-modal")');
      // Use the current rendered product type options; never invent delivery combinations.
      check(await js(`(()=>{const f=document.querySelector('.product-modal'),labels=[...f.querySelectorAll('label')];
        const field=s=>labels.find(l=>l.textContent.trim().startsWith(s))?.querySelector('input,select,textarea');
        const type=field('\u4ea7\u54c1\u7c7b\u578b');if(![...type.options].some(o=>o.value==='training'))return false;
        type.value='training';type.dispatchEvent(new Event('change',{bubbles:true}));
        const name=field('\u4ea7\u54c1\u6216\u670d\u52a1\u540d\u79f0');name.value=${JSON.stringify(fixture.product_name)};name.dispatchEvent(new Event('input',{bubbles:true}));
        window.__r3Form=f;window.__r3Invalid=0;window.__r3Submit=0;
        f.addEventListener('invalid',()=>window.__r3Invalid++,true);
        f.addEventListener('submit',()=>window.__r3Submit++,true);return true;})()`),'Training registration option unavailable');
      check(await js(`(()=>{const f=window.__r3Form;return [...f.querySelectorAll('select')].some(n=>n.value==='training') && [...f.querySelectorAll('[required]')].some(n=>!n.disabled && n.validity.valueMissing) && !f.checkValidity();})()`),'Missing training type/required-field failure');
      await js('window.__r3Invalid=0;true');
      await click('.product-modal button[type=submit]');
      check(await js('window.__r3Invalid>0 && window.__r3Submit===0 && document.querySelector(".product-modal")===window.__r3Form'),'Native required validation failed');
      const before=await js(`JSON.stringify([...window.__r3Form.querySelectorAll('input,textarea,select')].map(n=>[n.type,n.value,n.checked]))`);
      // Real pointer event on the scrim, outside the centered modal.
      const point=await js(`(()=>{const s=window.__r3Form.parentElement,r=window.__r3Form.getBoundingClientRect();return document.elementFromPoint(1,1)===s && !(r.left<=1&&r.top<=1);})()`);
      check(point,'No safe backdrop click point');
      await cdp('Input.dispatchMouseEvent',{type:'mousePressed',x:1,y:1,button:'left',clickCount:1});
      await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:1,y:1,button:'left',clickCount:1});
      await js('window.__r3Form.querySelector("input").focus();true');
      await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
      await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
      await sleep(200);
      check(await js('document.querySelector(".product-modal")===window.__r3Form'),'Modal dismissed');
      check(before===await js(`JSON.stringify([...window.__r3Form.querySelectorAll('input,textarea,select')].map(n=>[n.type,n.value,n.checked]))`),'Draft changed');
      check(productAttempts===0,'Product create attempted');
      report.checks.push(`${width}: native invalid submit blocks create; backdrop/Escape retain same draft`);
      // Fill current rendered fields. No Vue internals or fabricated API responses.
      stage='complete_form';
      await js(`(()=>{const f=window.__r3Form;
        const set=(root,label,value)=>{const n=[...root.querySelectorAll('label')].find(l=>l.textContent.trim().startsWith(label))?.querySelector('input,textarea,select');
          if(!n)throw Error('Missing registration field');n.value=value;n.dispatchEvent(new Event(n.tagName==='SELECT'?'change':'input',{bubbles:true}));};
        set(f,'\u63d0\u4f9b\u65b9\u540d\u79f0',${JSON.stringify(fixture.enterprise)});
        set(f,'\u4ea7\u54c1\u6216\u670d\u52a1\u63cf\u8ff0',${JSON.stringify(fixture.run_id+' synthetic training')});
        set(f,'\u6388\u6743\u6761\u4ef6',${JSON.stringify(fixture.run_id+' isolated QA only')});
        set(f,'\u6570\u636e\u6765\u6e90\u58f0\u660e','Synthetic owned QA fixture');
        set(f,'\u5408\u6cd5\u5408\u89c4\u58f0\u660e','Synthetic QA only; no external delivery');
        const row=f.querySelector('.version-row');set(row,'\u7248\u672c\u53f7','v1.0');
        set(row,'\u7248\u672c\u4ef7\u683c',12);set(row,'\u7248\u672c\u6210\u672c',7);
        set(row,'\u7248\u672c\u7b80\u8981\u4ecb\u7ecd',${JSON.stringify(fixture.run_id+' training version')});
        const category=[...f.querySelectorAll('label')].find(l=>l.textContent.trim().startsWith('\u6240\u5c5e\u76ee\u5f55')).querySelector('select');
        if(!category.value){category.value=[...category.options].find(o=>!o.disabled&&o.value)?.value||'';category.dispatchEvent(new Event('change',{bubbles:true}));}
        set(f,'\u89c4\u5219\u6765\u6e90','custom');return true;})()`);
      await wait('!!document.querySelector(".settlement-product-rule")');
      await js(`(()=>{const root=document.querySelector('.settlement-product-rule');
        const values=[0,100,0,0,0],inputs=[...root.querySelectorAll('input')];if(inputs.length!==5)throw Error('Rule shape');
        inputs.forEach((n,i)=>{n.value=values[i];n.dispatchEvent(new Event('input',{bubbles:true}));});return true;})()`);
      // Axios sends the real FormData through the original XHR method, after validating the
      // exact File bytes and structured metadata. CDP independently checks this intent.
      await js(`(()=>{const original=XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.send=function(body){
          if(!(body instanceof FormData))return original.call(this,body);
          const xhr=this,keys=[...body.keys()].sort(),file=body.get('upload');
          if(JSON.stringify(keys)!==JSON.stringify(['file_role','product_id','upload']) ||
            !(file instanceof File) || file.name!==${JSON.stringify(fixture.logo_name)} ||
            file.type!=='image/svg+xml' || body.get('file_role')!=='product_logo')throw Error('Unexpected upload');
          file.text().then(content=>{
            if(content!==${JSON.stringify(logoText)})throw Error('Unexpected logo bytes');
            window.__r3UploadIntent={product:body.get('product_id'),role:body.get('file_role'),name:file.name,type:file.type,content};
            original.call(xhr,body);
          }).catch(()=>{window.__r3UploadFailure=true;xhr.abort();});
        };return true;})()`);
      stage='logo_input';
      const doc=await cdp('DOM.getDocument');
      const input=await cdp('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'.product-modal input[type=file]'});
      check(input.nodeId,'Missing real logo input');
      await cdp('DOM.setFileInputFiles',{nodeId:input.nodeId,files:[logoPath]});
      await wait('window.__r3Form.querySelector("input[type=file]").files.length===1');
      check(await js('window.__r3Form.checkValidity() && window.__r3Form.querySelectorAll(".version-row").length===1'),'Complete form validity');
      // Isolate the invalid-required-field case on this otherwise valid form.
      stage='required_field_validation';
      await js(`(()=>{const n=[...window.__r3Form.querySelectorAll('label')].find(l=>l.textContent.trim().startsWith('\u6388\u6743\u6761\u4ef6')).querySelector('textarea');
        window.__r3Required=n;n.value='';n.dispatchEvent(new Event('input',{bubbles:true}));window.__r3Invalid=0;window.__r3Submit=0;return true;})()`);
      await click('.product-modal button[type=submit]');
      check(await js('window.__r3Required.validity.valueMissing && window.__r3Invalid>0 && window.__r3Submit===0'),'Required authorization did not prevent submit');
      check(productAttempts===0,'Invalid form attempted create');
      await js(`window.__r3Required.value=${JSON.stringify(fixture.run_id+' isolated QA only')};window.__r3Required.dispatchEvent(new Event('input',{bubbles:true}));true`);
      check(await js('window.__r3Form.checkValidity()'),'Restored form invalid');
      stage='arm_writes';
      armWrites();
      stage='single_save';
      await js(`(()=>{const b=window.__r3Form.querySelector('button[type=submit]');b.click();b.click();return true;})()`);
      await wait('!document.querySelector(".product-modal")');
      check(state.product && state.file && state.versions.length===1 && state.creates===1 &&
        state.uploads===1 && state.updates===1 && productAttempts===1,'Incomplete single-save sequence');
      stage='persisted_readback';
      const persisted=await js(`(async()=>{const r=await fetch('/api/products',{headers:{Authorization:'Bearer '+localStorage.getItem('market_token')}});if(!r.ok)return null;return r.json();})()`);
      const own=persisted?.items?.filter(p=>p.enterprise_id===fixture.enterprise);
      check(own?.length===1 && own[0].id===state.product && own[0].name===fixture.product_name &&
        own[0].logo_file_id===state.file && own[0].versions?.length===1,'Exactly one persisted owned product required');
      report.checks.push('otherwise valid training form: missing authorization prevents create',
        'real DOM.setFileInputFiles SVG upload; POST product + POST logo + PUT same product',
        'immediate double click generates exactly one create; owned list has one product/version/logo');
    }
    check(loginCount===1 && productAttempts===1 && blocked===0 && !fatal,'Unexpected mutation or blocked request');
    report.status='passed';
  } catch(e) {
    report.failureStage=stages.has(stage)?stage:'unknown';
    report.errorCode=errorCodes.has(e.code)?e.code:'browser_operation_failed';
    report.error=e.safe?e.message:'Browser check failed; inspect served selectors/contracts and guard allowlist';process.exitCode=1;
  }
  finally {
    report.loginRequests=loginCount;report.blockedRequests=blocked;report.productCreateAttempts=productAttempts;
    report.createRequests=state.creates;report.logoUploads=state.uploads;report.productUpdates=state.updates;
    fs.writeFileSync(process.argv[4],JSON.stringify(report,null,2));
    if(receiptFd!==undefined)fs.closeSync(receiptFd);
    if(socket)socket.close();for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error('Closed'));}
  }
})().catch(()=>{process.exitCode=1;});
'@

$temporary = Join-Path $env:TEMP ('r3-reg-' + [Guid]::NewGuid().ToString('N'))
$chrome = $null
try {
    New-Item -ItemType Directory -Path $temporary | Out-Null
    $scriptFile = Join-Path $temporary 'probe.cjs'
    [IO.File]::WriteAllText($scriptFile, $probe, [Text.UTF8Encoding]::new($false))
    & $NodePath --check $scriptFile
    if ($LASTEXITCODE -ne 0) { throw 'Node syntax failed' }
    if ($OfflineSyntaxCheck) { return }
    $fixturePath = [IO.Path]::GetFullPath($FixtureFile)
    $repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')) + [IO.Path]::DirectorySeparatorChar
    if ($fixturePath.StartsWith($repo,[StringComparison]::OrdinalIgnoreCase)) { throw 'Credentials must be outside repository' }
    if ((Get-Item -LiteralPath $fixturePath).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Credential links refused' }
    $output = [IO.Path]::GetFullPath($OutputDir)
    if (Test-Path -LiteralPath $output) { throw 'Output directory must be new' }
    New-Item -ItemType Directory -Path $output | Out-Null
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $listener.Start(); $port = $listener.LocalEndpoint.Port; $listener.Stop()
    $profile = Join-Path $temporary 'profile'
    $chrome = Start-Process $ChromePath -ArgumentList @('--headless=new',"--remote-debugging-port=$port",'--remote-debugging-address=127.0.0.1',"--user-data-dir=`"$profile`"",'--no-first-run','--disable-background-networking','about:blank') -PassThru
    & $NodePath $scriptFile $fixturePath $port (Join-Path $output 'browser-results.json') $temporary
    if ($LASTEXITCODE -ne 0) { throw 'Browser checks failed; see redacted result' }
} finally {
    if ($chrome -and -not $chrome.HasExited) { Stop-Process -Id $chrome.Id -Force; $chrome.WaitForExit() }
    # Exact GUID directory created above, never a prefix/glob or supplied directory.
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
}
