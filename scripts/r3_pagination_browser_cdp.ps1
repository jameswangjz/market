param(
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [Parameter(Mandatory=$true)][ValidatePattern('^qa-r3-page-[a-f0-9]{12}$')][string]$Query,
    [string]$NodePath = 'node',
    [ValidatePattern('^[A-Za-z0-9_.-]+\.js$')][string]$ExpectedAsset,
    [string]$ChromePath = 'C:\Program Files\Google\Chrome\Application\chrome.exe',
    [switch]$OfflineSyntaxCheck
)
$ErrorActionPreference = 'Stop'
# Native Node 22 WebSocket/CDP, as in the registration runner. No fixture writes.
$probe = @'
'use strict';
const fs=require('node:fs');
const origin='http://192.168.10.10:30080', query=process.argv[2], missing=query+'-missing';
const check=(ok,message)=>{if(!ok)throw Object.assign(Error(message),{safe:true});};
check(/^qa-r3-page-[a-f0-9]{12}$/.test(query),'Invalid owned query');
const expectedAsset=process.argv[5]||'';
check(!expectedAsset || /^[A-Za-z0-9_.-]+\.js$/.test(expectedAsset),'Invalid expected asset');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let socket,serial=0,fatal,mutationAttempts=0,blocked=0;
const pending=new Map(),ownedIds=new Set(),snapshots=new Map();
const result={status:'failed',origin,query,mode:'GET-only synthetic pagination',checks:[],screenshots:[],
  limitations:['Synthetic published fixture; not approval or registration evidence'],blockedRoutes:[]};
function cdp(method,params={}){
  return new Promise((resolve,reject)=>{const id=++serial;
    const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout'));},15000);
    pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));});
}
async function js(expression){
  if(fatal)throw fatal;
  const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
  check(!r.exceptionDetails,'Browser evaluation failed');return r.result.value;
}
async function wait(expression){
  for(let i=0;i<150;i++){if(await js(expression))return;await sleep(100);}
  check(false,'Storefront did not reach expected state');
}
function queryAllowed(u,api=false){
  const allowed=api?['q','page','page_size','product_type','parent_id','category_id']:['q','page','page_size'];
  return [...u.searchParams.keys()].every(k=>allowed.includes(k) && u.searchParams.getAll(k).length===1) &&
    [query,missing].includes(u.searchParams.get('q')) &&
    ['1','2'].includes(u.searchParams.get('page')||'1') &&
    ['10','20'].includes(u.searchParams.get('page_size')) &&
    ['product_type','parent_id','category_id'].every(k=>!u.searchParams.get(k));
}
function allowedRequest(r){
  const u=new URL(r.url);
  if(r.method!=='GET' || u.origin!==origin || u.username || u.password || u.hash ||
    Object.keys(r.headers||{}).some(k=>['authorization','cookie','x-enterprise-id'].includes(k.toLowerCase())))return false;
  if(u.pathname==='/')return queryAllowed(u);
  if(/^\/assets\/[A-Za-z0-9_.-]+$/.test(u.pathname) || u.pathname==='/favicon.ico')return !u.search;
  if(u.pathname==='/api/storefront/categories')return !u.search;
  if(u.pathname==='/api/storefront/products')return queryAllowed(u,true);
  const logo=u.pathname.match(/^\/api\/storefront\/products\/([A-Za-z0-9_-]{1,80})\/logo$/);
  return Boolean(logo && ownedIds.has(logo[1]) && !u.search);
}
function validatePage(body,u){
  const q=u.searchParams.get('q'),page=Number(u.searchParams.get('page')||1),size=Number(u.searchParams.get('page_size'));
  const total=q===query?12:0,count=Math.max(0,Math.min(size,total-(page-1)*size));
  check(body.total===total && body.page===page && body.page_size===size &&
    Array.isArray(body.items) && body.items.length===count,'Unexpected public pagination contract');
  const names=body.items.map(p=>p.name),ids=body.items.map(p=>p.id);
  check(names.every(n=>typeof n==='string' && n.startsWith(query)) && new Set(names).size===count &&
    ids.every(id=>typeof id==='string' && /^[A-Za-z0-9_-]{1,80}$/.test(id) &&
      !['qa-vcq-a044ea9ea181-p2','ORD-261012005144E1B3'].includes(id)) && new Set(ids).size===count,
    'Foreign or duplicate product in owned search');
  ids.forEach(id=>ownedIds.add(id));
  snapshots.set(`${q}:${size}:${page}`,{names,ids});
}
async function paused(p){
  const r=p.request,u=new URL(r.url);
  if(p.responseStatusCode!==undefined){
    check(p.responseStatusCode===200,'Public product GET failed');
    const raw=await cdp('Fetch.getResponseBody',{requestId:p.requestId});
    validatePage(JSON.parse(raw.base64Encoded?Buffer.from(raw.body,'base64').toString('utf8'):raw.body),u);
    await cdp('Fetch.continueRequest',{requestId:p.requestId});return;
  }
  const allowed=allowedRequest(r);
  if(!allowed){
    blocked++;if(r.method!=='GET')mutationAttempts++;
    if(result.blockedRoutes.length<20)result.blockedRoutes.push({method:r.method,path:u.origin===origin?u.pathname:'external'});
  }
  await cdp(allowed?'Fetch.continueRequest':'Fetch.failRequest',allowed?{requestId:p.requestId}:
    {requestId:p.requestId,errorReason:'BlockedByClient'});
  check(allowed,'Request outside GET-only storefront allowlist');
}
async function click(selector){
  const p=await js(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});if(!n||n.disabled)return null;
    n.scrollIntoView({block:'center'});const r=n.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  check(p,'Missing enabled pager control');
  for(const type of ['mousePressed','mouseReleased'])await cdp('Input.dispatchMouseEvent',{type,...p,button:'left',clickCount:1});
}
async function ready(q,size,page,count){
  await wait(`(()=>{const p=document.querySelector('.pagination-controls'),u=new URL(location.href);
    return p?.getAttribute('aria-busy')==='false' && !document.querySelector('.store-state[role=alert]') &&
      u.searchParams.get('q')===${JSON.stringify(q)} && u.searchParams.get('page_size')===${JSON.stringify(String(size))} &&
      (u.searchParams.get('page')||'1')===${JSON.stringify(String(page))} &&
      document.querySelectorAll('.store-card').length===${count} &&
      p.querySelector('select')?.value===${JSON.stringify(String(size))} &&
      p.querySelector('input')?.value===${JSON.stringify(String(page))};})()`);
  const observed=await js(`(()=>{const p=document.querySelector('.pagination-controls');return {
    names:[...document.querySelectorAll('.store-card h2')].map(n=>n.textContent.trim()),
    ids:[...document.querySelectorAll('.store-card-title')].map(n=>decodeURIComponent(new URL(n.href).pathname.split('/').pop())),
    position:p.querySelector('.pagination-position').textContent.trim(),
    total:p.querySelector('.pagination-total').textContent.trim()};})()`);
  const snapshot=snapshots.get(`${q}:${size}:${page}`);
  check(snapshot && JSON.stringify(observed.names)===JSON.stringify(snapshot.names) &&
    JSON.stringify(observed.ids)===JSON.stringify(snapshot.ids),'DOM differs from guarded public response');
  check(observed.position===`${page} / ${Math.max(1,Math.ceil((q===query?12:0)/size))}`,'Pager position differs');
  return observed;
}
async function evidence(width,label){
  // Every card was privacy-checked at the response boundary before rendering.
  const bounds=await js(`(()=>{const nav=document.querySelector('.pagination-controls');nav.scrollIntoView({block:'center'});
    const r=nav.getBoundingClientRect(),controls=[...nav.querySelectorAll('button,input,select')].map(n=>n.getBoundingClientRect());
    return {overflow:document.documentElement.scrollWidth>innerWidth+1,left:r.left,right:r.right,
      inside:controls.every(b=>b.left>=r.left-1&&b.right<=r.right+1&&b.top>=r.top-1&&b.bottom<=r.bottom+1&&b.top>=-1&&b.bottom<=innerHeight+1),
      overlap:controls.some((a,i)=>controls.slice(i+1).some(b=>Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)),
      cardsFit:[...document.querySelectorAll('.store-card,.store-card h2')].every(n=>{const b=n.getBoundingClientRect();return b.left>=-1&&b.right<=innerWidth+1&&n.scrollWidth<=n.clientWidth+1;})};})()`);
  check(!bounds.overflow && bounds.left>=-1 && bounds.right<=width+1 && bounds.inside && !bounds.overlap && bounds.cardsFit,'Card/pager bounds failed');
  const filename=`${width}-${label}.png`,shot=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
  fs.writeFileSync(process.argv[4]+'/'+filename,Buffer.from(shot.data,'base64'),{flag:'wx'});
  result.screenshots.push(filename);
}
(async()=>{
  try{
    let target;
    for(let i=0;i<60;i++){
      try{target=(await(await fetch(`http://127.0.0.1:${process.argv[3]}/json/list`)).json()).find(p=>p.type==='page');}catch{}
      if(target)break;await sleep(200);
    }
    check(target,'Chrome unavailable');socket=new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=()=>reject(Error('CDP unavailable'));});
    socket.onmessage=event=>{
      const m=JSON.parse(event.data);
      if(m.id){const p=pending.get(m.id);if(p){clearTimeout(p.timer);pending.delete(m.id);m.error?p.reject(Error('CDP failed')):p.resolve(m.result);}return;}
      if(m.method==='Fetch.requestPaused')paused(m.params).catch(e=>{fatal=e;socket.close();});
      if(m.method==='Page.javascriptDialogOpening'){
        fatal=Object.assign(Error('Unexpected native dialog'),{safe:true});cdp('Page.handleJavaScriptDialog',{accept:false}).catch(()=>{});
      }
    };
    await cdp('Page.enable');await cdp('Runtime.enable');
    await cdp('Browser.setDownloadBehavior',{behavior:'deny'});
    await cdp('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'},
      {urlPattern:origin+'/api/storefront/products?*',requestStage:'Response'}]});
    for(const width of [1440,390]){
      await cdp('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:width===390});
      await cdp('Page.navigate',{url:origin+'/?q='+query+'&page_size=10'});
      const first=await ready(query,10,1,10);
      if(expectedAsset)check(await js(`performance.getEntriesByType('resource').some(r=>new URL(r.name).pathname===${JSON.stringify('/assets/'+expectedAsset)})`),'Expected storefront asset absent');
      await evidence(width,'page1');
      await js(`(()=>{const n=document.querySelector('.pagination-controls input');n.value='2';n.dispatchEvent(new Event('input',{bubbles:true}));return true;})()`);
      await click('.pagination-controls button[title="\u8df3\u8f6c\u5230\u6307\u5b9a\u9875"]');
      const second=await ready(query,10,2,2);
      check(new Set([...first.ids,...second.ids]).size===12 && new Set([...first.names,...second.names]).size===12,'Page1/page2 overlap or missing fixture');
      await evidence(width,'page2');
      await js('window.__r3BeforeReload=true;true');
      await cdp('Page.reload',{ignoreCache:true});
      await wait('window.__r3BeforeReload!==true && !!document.querySelector(".pagination-controls")');
      const reloaded=await ready(query,10,2,2);
      check(JSON.stringify(reloaded.ids)===JSON.stringify(second.ids),'F5-equivalent reload changed page2');
      await evidence(width,'page2-reload');
      await js(`(()=>{const n=document.querySelector('.pagination-controls select');n.value='20';n.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
      const all=await ready(query,20,1,12);
      check([...first.ids,...second.ids].every(id=>all.ids.includes(id)),'Page-size20 changed fixture set');
      await evidence(width,'size20-reset');
      await js(`(()=>{const n=document.querySelector('.store-search input');n.value=${JSON.stringify(missing)};n.dispatchEvent(new Event('input',{bubbles:true}));return true;})()`);
      await click('.store-filters button[type=submit]');
      await ready(missing,20,1,0);
      check(await js(`(()=>{const p=document.querySelector('.pagination-controls');return p.querySelector('input').disabled && [...p.querySelectorAll('button')].every(n=>n.disabled) && !!document.querySelector('.store-state');})()`),'Empty search jump/navigation not disabled');
      await evidence(width,'empty-search');
      result.checks.push(`${width}: 10/12 cards; jump2 gives2; reload retains2; size20 resets1 with12; empty search disables jump; guarded names/IDs and bounds`);
    }
    check(!fatal && mutationAttempts===0 && blocked===0,'Unexpected request or mutation');result.status='passed';
  }catch(e){result.error=e.safe?e.message:'Browser check failed; inspect storefront contract and allowlist';process.exitCode=1;}
  finally{
    result.blockedRequests=blocked;result.mutationAttempts=mutationAttempts;
    fs.writeFileSync(process.argv[4]+'/browser-results.json',JSON.stringify(result,null,2),{flag:'wx'});
    if(socket)socket.close();for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error('Closed'));}
  }
})().catch(()=>{process.exitCode=1;});
'@
$temporary = Join-Path $env:TEMP ('r3-page-' + [Guid]::NewGuid().ToString('N'))
$chrome = $null
try {
    New-Item -ItemType Directory -Path $temporary | Out-Null
    $scriptFile = Join-Path $temporary 'probe.cjs'
    [IO.File]::WriteAllText($scriptFile,$probe,[Text.UTF8Encoding]::new($false))
    & $NodePath --check $scriptFile
    if ($LASTEXITCODE -ne 0) { throw 'Node syntax failed' }
    if ($OfflineSyntaxCheck) { return }
    $output = [IO.Path]::GetFullPath($OutputDir)
    if (Test-Path -LiteralPath $output) { throw 'Output directory must be new' }
    New-Item -ItemType Directory -Path $output | Out-Null
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $listener.Start(); $port=$listener.LocalEndpoint.Port; $listener.Stop()
    $profile=Join-Path $temporary 'profile'
    $chrome=Start-Process $ChromePath -ArgumentList @('--headless=new',"--remote-debugging-port=$port",'--remote-debugging-address=127.0.0.1',"--user-data-dir=`"$profile`"",'--no-first-run','--disable-background-networking','about:blank') -PassThru
    & $NodePath $scriptFile $Query $port $output $ExpectedAsset
    if ($LASTEXITCODE -ne 0) { throw 'Pagination checks failed; see redacted browser-results.json' }
} finally {
    if ($chrome -and -not $chrome.HasExited) { Stop-Process -Id $chrome.Id -Force; $chrome.WaitForExit() }
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
}
