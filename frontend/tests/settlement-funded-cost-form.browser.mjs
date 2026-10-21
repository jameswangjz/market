// Local browser + mock discovery contract, not real finance/backend E2E.
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { mkdir } from 'node:fs/promises';
import { createServer } from 'vite';
import { actualDiscoveryDtos } from './fixtures/cost-basis-http-dto.mjs';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || '/tmp/market-funded-form-tools/node_modules/playwright-core/index.mjs');
let server, browser, url, realDtos;
const output = process.env.FUNDED_COST_SCREENSHOTS || '/tmp/market-funded-cost-form';
const entry = `
import {createApp,h,reactive} from '/node_modules/.vite/deps/vue.js';
import Management from '/src/SettlementManagement.vue';
import {fundedBasis,ordinaryBasis,refund as sampleRefund,provider,enterprise} from '/tests/fixtures/funded-cost-basis.mjs';
import realDtos from '/__funded-cost-real-dtos.js';
const params=new URLSearchParams(location.search),selected=realDtos[params.get('real')],refund=selected?.refund||sampleRefund;
const count=selected?.basis.references.length||Number(params.get('count')||1);
const state=window.fixture={refund,basis:selected?.basis||(params.has('ordinary')?ordinaryBasis(count):fundedBasis(count)),reads:[],writes:[],exceptions:[]};
const context=reactive({user:provider,enterprise,businessView:'provider'});
window.reviewAsPlatform=()=>{context.user={id:'ops',platform_role:'platform_operator'};context.businessView='platform'};
const api={get:async(path,options)=>{state.reads.push({path,options});
  if(path==='/settlement-cost-exceptions/basis')return{data:state.basis};
  if(path==='/refunds')return{data:{items:[refund],total:1}};
  if(path==='/settlement-reporting/report')return{data:{items:state.basis.references.map(r=>({id:r.reference_settlement_id,order_id:refund.order_id,order_no:r.order_no,settlement_no:r.settlement_no,batch_no:r.batch_no,baseline_hash:r.baseline_hash})),total:count}};
  if(path==='/settlement-parties')return{data:{items:[{id:'expert',kind:'expert',name:'Fixture expert'}]}};
  if(path==='/settlement-cost-exceptions')return{data:{items:state.exceptions}};
  throw Error('Unexpected read '+path);
},post:async(path,body)=>{state.writes.push({path,body});
  if(path==='/settlement-cost-exceptions'){
    const row={...body,id:'exception',order_id:refund.order_id,currency:'CNY',version:1,status:'pending',creator_id:provider.id,creator_side:'provider',bearer_participant_name:'Fixture expert',
      source_allocations:(body.source_allocations||[]).map(a=>({...a,baseline_hash:state.basis.references.find(r=>r.segment_id===a.segment_id).baseline_hash,basis:state.basis.references.find(r=>r.segment_id===a.segment_id)}))};
    state.exceptions=[row];return{data:row};
  }
  if(path==='/settlement-cost-exceptions/exception/decision'){state.exceptions[0].status='approved';return{data:state.exceptions[0]}};
  throw Error('Unexpected write '+path);
}};
createApp({render:()=>h(Management,{...context,api})}).mount('#app');
`;
before(async () => {
  realDtos = actualDiscoveryDtos();
  server = await createServer({ root: new URL('..', import.meta.url).pathname,
    server: { host: '127.0.0.1', port: 0, strictPort: false },
    plugins: [{ name: 'funded-cost-form-fixture', configureServer(vite) {
      vite.middlewares.use((req, res, next) => {
        if (req.url?.split('?')[0] === '/__funded-cost-form') {
          res.setHeader('Content-Type', 'text/html');
          res.end('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>@font-face{font-family:FixtureCJK;src:url(/__funded-cost-font)}body,button,input,select,textarea{font-family:FixtureCJK,sans-serif}</style><div id="app"></div><script type="module" src="/__funded-cost-form.js"></script>');
        } else if (req.url === '/__funded-cost-font') {
          res.setHeader('Content-Type', 'font/collection');
          res.end(readFileSync(process.env.CJK_FONT_PATH || '/mnt/c/Windows/Fonts/msyh.ttc'));
        } else if (req.url === '/__funded-cost-real-dtos.js') {
          res.setHeader('Content-Type', 'text/javascript'); res.end(`export default ${JSON.stringify(realDtos)};`);
        } else if (req.url === '/__funded-cost-form.js') {
          res.setHeader('Content-Type', 'text/javascript'); res.end(entry);
        } else next();
      });
    } }] });
  await server.listen(); url = `http://127.0.0.1:${server.httpServer.address().port}`;
  await mkdir(output, { recursive: true });
  browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/home/james/.cache/puppeteer/chrome/linux-148.0.7778.97/chrome-linux64/chrome',
    headless: true, args: ['--no-sandbox'] });
});
after(async () => { await browser?.close(); await server?.close(); });
async function openForm(page, query = '') {
  await page.goto(`${url}/__funded-cost-form${query}`);
  await page.getByRole('tab', { name: '成本例外', exact: true }).click();
  await page.getByRole('button', { name: '申请成本例外', exact: true }).click();
  await page.locator('form.editor select[required]').first().selectOption(await page.evaluate(() => window.fixture.refund.id));
}
for (const count of [1, 2]) test(`real Vue funded ${count}-source form, ceilings, exact payload and whole opposite-side review`, async () => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  try {
    await openForm(page, `?count=${count}`);
    await page.waitForFunction(n => document.querySelectorAll('form.editor .cost-source').length === n, count);
    const rows = page.locator('form.editor .cost-source');
    assert.match(await rows.first().innerText(), /ORD-FUNDED.*SET-0/s);
    await rows.first().locator('input').fill('20.01');
    for (let i = 0; i < count; i++) await rows.nth(i).locator('textarea').fill(`invoice:${i}`);
    await page.locator('form.editor select[required]').nth(1).selectOption('expert');
    await page.getByLabel('申请依据', { exact: true }).fill('Invoice evidence');
    await page.getByRole('button', { name: '提交整单申请', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[role=alert]')?.textContent.includes('上限'));
    assert.equal(await page.evaluate(() => window.fixture.writes.length), 0);
    await rows.first().locator('input').fill('0.01');
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1100 });
      await page.evaluate(() => document.fonts.ready);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: `${output}/funded-${count}-${width}.png`, fullPage: true });
    }
    await page.getByRole('button', { name: '提交整单申请', exact: true }).click();
    await page.waitForFunction(() => window.fixture.writes.length === 1 && !document.querySelector('form.editor'));
    const body = await page.evaluate(() => window.fixture.writes[0].body);
    assert.equal(body.basis_kind, 'funded'); assert.equal(body.schema_version, 2);
    assert.equal(body.retained_cost, '0.01'); assert.equal(body.source_allocations.length, count);
    assert.ok(body.request_id); assert.ok(body.source_allocations.every(s => s.evidence_refs.length === 1));
    if (count > 1) assert.equal(body.source_allocations[1].retained_cost, '0.00');
    assert.equal(await page.getByRole('button', { name: '批准', exact: true }).count(), 0);
    await page.evaluate(() => window.reviewAsPlatform());
    await page.getByRole('button', { name: '批准', exact: true }).click();
    await page.waitForFunction(n => document.querySelectorAll('form.editor .cost-source').length === n, count);
    assert.equal(await page.locator('form.editor .cost-source input, form.editor .cost-source textarea').count(), 0);
    await page.getByLabel('整单审批依据', { exact: true }).fill('Independent whole-proposal review');
    await page.getByRole('button', { name: '确认整单批准', exact: true }).click();
    await page.waitForFunction(() => window.fixture.writes.length === 2);
    const decision = await page.evaluate(() => window.fixture.writes[1]);
    assert.equal(decision.path, '/settlement-cost-exceptions/exception/decision');
    assert.equal(decision.body.expected_version, 1); assert.equal(decision.body.source_allocations, undefined);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
});
test('browser form blocks waiting, denied and applied origins, without assessment fallback', async () => {
  const page = await browser.newPage();
  try {
    for (const patch of [{ status: 'waiting' }, { proposal_allowed: false }, { basis_origin: 'applied_funded_basis' }]) {
      await page.goto(`${url}/__funded-cost-form`);
      await page.waitForFunction(() => !!window.fixture);
      await page.evaluate(patch => Object.assign(window.fixture.basis, patch), patch);
      await page.getByRole('tab', { name: '成本例外', exact: true }).click();
      await page.getByRole('button', { name: '申请成本例外', exact: true }).click();
      await page.locator('form.editor select[required]').first().selectOption('funded-refund');
      await page.waitForFunction(() => !!document.querySelector('[role=alert]'));
      assert.ok(await page.getByRole('button', { name: '提交整单申请', exact: true }).isDisabled());
      assert.equal(await page.evaluate(() => window.fixture.writes.length), 0);
      assert.ok(await page.evaluate(() => !window.fixture.reads.some(r => /report|settlements\//.test(r.path))));
    }
  } finally { await page.close(); }
});
test('real backend HTTP DTOs render business refs and submit legacy single versus schema2 multi/funded forms', async () => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  try {
    for (const [kind, { basis }] of Object.entries(realDtos)) {
      await openForm(page, `?real=${kind}`);
      await page.waitForFunction(n => document.querySelectorAll('form.editor .cost-source').length === n, basis.references.length);
      const rows = page.locator('form.editor .cost-source');
      for (let i = 0; i < basis.references.length; i++) {
        const ref = basis.references[i], text = await rows.nth(i).innerText();
        assert.ok(text.includes(ref.order_no)); assert.ok(text.includes(ref.settlement_no));
        ref.batch_nos.forEach(no => assert.ok(text.includes(no)));
        assert.ok(text.includes(ref.cost_amount.replace(/^-/, '')));
        await rows.nth(i).locator('input').fill(i ? '0.00' : '0.01');
        await rows.nth(i).locator('textarea').fill(`invoice:${i}`);
      }
      await page.locator('form.editor select[required]').nth(1).selectOption('expert');
      await page.getByLabel('申请依据', { exact: true }).fill('Invoice evidence');
      await page.getByRole('button', { name: '提交整单申请', exact: true }).click();
      await page.waitForFunction(() => window.fixture.writes.length === 1);
      const body = await page.evaluate(() => window.fixture.writes[0].body);
      assert.equal(body.schema_version, kind === 'single' ? undefined : 2);
      assert.equal(body.source_allocations?.length, kind === 'single' ? undefined : basis.references.length);
      assert.equal(body.basis_kind, kind === 'funded' ? 'funded' : undefined);
    }
  } finally { await page.close(); }
});
