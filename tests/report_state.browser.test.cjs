const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const {createServer} = require('node:http');
const {readFileSync, existsSync} = require('node:fs');
const {resolve, join, extname, sep} = require('node:path');
const {spawnSync} = require('node:child_process');
const {createHash} = require('node:crypto');

// Optional browser dependency; no CAD, historical report or network service is used.
let chromium;
try { ({chromium} = require(process.env.REPORT_TEST_PLAYWRIGHT || 'playwright')); } catch {}
const browserPath = process.env.VIEWER_TEST_BROWSER || [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe', '/usr/bin/chromium'
].find(existsSync);
const enabled = Boolean(chromium && browserPath);
const root = resolve(__dirname, '..');
const mesh = {vertices: [0,0,0, 40,0,0, 0,30,0, 0,0,20], indices: [0,2,1,0,1,3,0,3,2,1,2,3]};
const view = frame => ({candidate: 'mesh.json', reference: 'mesh.json',
  difference: {status:'ready',scope:'whole_model',layers:{matched:'mesh.json'},surface_layers:{matched:'surface.json'},
    visualization:{kind:'surface-distance-tolerance',tolerance_mm:.01,description:'显示容差 0.01 mm；不改变评分。'}},
  metadata: {preview: {overlay_kind:'ordinary_overlay',difference_kind:'difference'}},
  previews: Object.fromEntries(['iso','front','top','right'].map(angle => [angle,
    Object.fromEntries(['split','overlay','difference'].map(kind => [kind, `${frame}-${angle}-${kind}.svg`]))]))});
const row = id => ({case:id, enterprise_id:id==='A'?'E005':'E006', enterprise_title:id==='A'?'Guide pin':'Support plate', status:'ok', score_kind:'measured', final_score:.8,
  participates_in_scoring:true, integrity_status:'verified', geometry:{candidate_status:'valid'},
  measurement_weight_coverage:1, weights:{shape:6/13,dimension:5/13,mass:2/13},
  metrics:{}, display:{views:{scoring:view('scoring'),world:view('world')}},
  prompt:'Build a part.', input_materials:{items:[{kind:'pdf',label:'Drawing',url:'inputs/drawing.pdf',
    previews:[{url:'inputs/page1.svg',label:'Page 1'},{url:'inputs/page2.svg',label:'Page 2'}]}]}});
let server, browser, base;
before(async () => {
  if (!enabled) return;
  const rendered = spawnSync(process.env.FUSION_STEP_PYTHON || process.env.PYTHON || 'python', ['-B','-c',
    'import json,sys; from src.html_reporting import render_html; print(render_html(json.load(sys.stdin)))'],
    {cwd:root,input:JSON.stringify({run_name:'State regression',coordinate_mode:'geometry',cases:[row('A'),row('B')],summary:{}}),encoding:'utf8'});
  assert.equal(rendered.status,0,rendered.stderr);
  server = createServer((req,res) => {
    const pathname = new URL(req.url,'http://localhost').pathname;
    if (pathname === '/' || pathname === '/index.html') {res.setHeader('Content-Type','text/html; charset=utf-8');res.end(rendered.stdout);return;}
    if (['/mesh.json','/surface.json'].includes(pathname)) {res.setHeader('Content-Type','application/json');res.end(JSON.stringify(mesh));return;}
    if (pathname.endsWith('.svg')) {res.setHeader('Content-Type','image/svg+xml');res.end('<svg xmlns="http://www.w3.org/2000/svg" width="2000" height="1000"><rect x="200" y="100" width="1600" height="800" fill="#35baf2"/><text x="400" y="500" font-size="80">'+pathname+'</text></svg>');return;}
    const file = resolve(root,'report_assets',pathname.replace(/^\/assets\//,''));
    if (!file.startsWith(join(root,'report_assets')+sep) || !existsSync(file)) {res.writeHead(404).end();return;}
    res.setHeader('Content-Type',extname(file)==='.js'?'application/javascript':extname(file)==='.css'?'text/css':'application/octet-stream');res.end(readFileSync(file));
  });
  await new Promise(r => server.listen(0,'127.0.0.1',r));
  base = `http://127.0.0.1:${server.address().port}/index.html`;
  browser = await chromium.launch({headless:true,executablePath:browserPath,args:['--no-first-run','--no-default-browser-check']});
});
after(async () => {await browser?.close();if(server)await new Promise(r => server.close(r));});
const options = {skip:!enabled,timeout:30000};
async function open(t) {
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  t.after(() => page.close());
  await page.goto(base+'#fixture=A');await ready(page);return page;
}
async function ready(page) {await page.locator('.case-detail.active .viewer-status').filter({hasText:'拖动旋转'}).waitFor();}
async function state(page) {return page.locator('.case-detail.active').evaluate(s => ({
  frame:s.querySelector('[data-frame][aria-pressed="true"]')?.dataset.frame,
  mode:s.querySelector('[data-mode][aria-pressed="true"]')?.dataset.mode,
  angle:s.querySelector('[data-view][aria-pressed="true"]')?.dataset.view,
  opacity:[...s.querySelectorAll('[data-opacity]')].map(n=>n.value),
  open:s.querySelectorAll('details[open]').length,page:s.querySelector('.input-gallery')?.dataset.page
}));}
async function inspect(page) {
  const section=page.locator('.case-detail.active');
  await section.locator('[data-mode="overlay"]').click();
  await section.locator('[data-view="front"]').click();
  await section.locator('[data-opacity="candidate"]').fill('0.2');
  return section;
}
test('changing coordinate frame preserves direction, mode and opacity', options, async t => {
  const page=await open(t),section=await inspect(page);
  await section.locator('[data-frame="world"]').click();await ready(page);
  assert.deepEqual(await state(page),{frame:'world',mode:'overlay',angle:'front',opacity:['0.2','0.35'],open:0,page:'0'});
});
test('changing coordinate frame preserves a freely rotated and zoomed view',options,async t=>{
  const page=await open(t),section=page.locator('.case-detail.active');
  const canvas=section.locator('canvas');await canvas.scrollIntoViewIfNeeded();
  const bounds=await canvas.boundingBox(),x=bounds.x+bounds.width*.25,y=bounds.y+bounds.height*.5;
  await page.mouse.move(x,y);await page.mouse.down();await page.mouse.move(x+80,y-40,{steps:8});await page.mouse.up();
  await page.mouse.wheel(0,-150);
  await section.locator('.view-state').filter({hasText:'自由视角'}).waitFor();
  const axes=()=>section.locator('[data-direction-axes] line').evaluateAll(nodes=>nodes.map(n=>[n.getAttribute('x2'),n.getAttribute('y2')]));
  const before=await axes(),image=await canvas.screenshot();
  await section.locator('[data-frame="world"]').click();await ready(page);
  assert.equal(await section.locator('.view-state').textContent(),'自由视角');
  assert.deepEqual(await axes(),before);
  const after=await canvas.screenshot();
  assert.equal(createHash('sha256').update(after).digest('hex'),createHash('sha256').update(image).digest('hex'),
    'identical source geometry should retain the same camera projection across coordinate views');
});
test('reentering a case and resetting comparison restore the same complete defaults as reload',options,async t=>{
  const page=await open(t),section=await inspect(page);
  await section.locator('.input-materials > summary').click();
  await section.locator('[data-gallery-step="1"]').click();
  await page.goto(base+'#fixture=B');await ready(page);
  await page.goto(base+'#fixture=A');await ready(page);
  const defaults={frame:'scoring',mode:'split',angle:'iso',opacity:['0.65','0.35'],open:0,page:'0'};
  assert.deepEqual(await state(page),defaults);
  await inspect(page);await section.locator('[data-frame="world"]').click();await ready(page);
  await section.locator('[data-reset]').click();await ready(page);
  assert.deepEqual(await state(page),defaults);
  await page.reload();await ready(page);assert.deepEqual(await state(page),defaults);
});
test('split, overlay and difference galleries open their own four-image sets',options,async t=>{
  const page=await open(t);
  for(const kind of ['split','overlay','difference']) {
    await page.locator(`.case-detail.active button[data-gallery-kind="${kind}"]`).click();
    const gallery=page.locator(`.case-detail.active [data-preview-kind="${kind}"]`);
    assert.equal(await gallery.count(),1,`${kind} gallery must exist`);
    assert.equal(await gallery.locator('[data-lightbox]').count(),4);
    await gallery.locator('[data-lightbox]').first().click();
    assert.match(await page.locator('.lightbox-image').getAttribute('src'),new RegExp(`-iso-${kind}\\.svg$`));
    assert.equal(await page.locator('.lightbox-count').textContent(),'1 / 4');
    await page.keyboard.press('ArrowRight');
    assert.match(await page.locator('.lightbox-image').getAttribute('src'),new RegExp(`-front-${kind}\\.svg$`));
    await page.keyboard.press('Escape');
  }
});
test('lightbox zoom changes image size, drag moves it, and fit and actual size reset predictably',options,async t=>{
  const page=await open(t);
  await page.locator('.case-detail.active [data-preview-kind="split"] [data-lightbox]').first().click();
  const viewport=page.locator('.lightbox-viewport'),image=page.locator('.lightbox-image');
  assert.equal(await viewport.count(),1,'images need an interactive viewport');
  await page.waitForFunction(()=>document.querySelector('.lightbox-image').naturalWidth>0);
  const before=await image.boundingBox();
  await page.locator('[data-lightbox-zoom="in"]').click();
  const zoomed=await image.boundingBox();assert.ok(zoomed.width>before.width*1.1);
  const box=await viewport.boundingBox();
  await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();
  await page.mouse.move(box.x+box.width/2+90,box.y+box.height/2+50,{steps:5});await page.mouse.up();
  const moved=await image.boundingBox();assert.ok(Math.abs(moved.x-zoomed.x)>20);
  await page.locator('[data-lightbox-zoom="actual"]').click();
  assert.ok(Math.abs((await image.boundingBox()).width-2000)<1);
  await page.locator('[data-lightbox-zoom="fit"]').click();
  assert.ok(Math.abs((await image.boundingBox()).width-before.width)<1);
  await page.keyboard.press('Escape');assert.equal(await page.locator('#image-lightbox').evaluate(n=>n.open),false);
});
test('failed WebGL keeps independent overlay and difference static views usable',options,async t=>{
  const page=await browser.newPage({viewport:{width:1280,height:900}});t.after(()=>page.close());
  await page.route('**/assets/viewer/comparison-view.js',route=>route.abort());
  await page.goto(base+'#fixture=A');
  const section=page.locator('.case-detail.active');
  await section.locator('.viewer-status').filter({hasText:'WebGL'}).waitFor();
  await section.locator('[data-view="top"]').click();
  for(const mode of ['overlay','difference','split']) {
    await section.locator(`[data-mode="${mode}"]`).click();
    assert.match(await section.locator('.preview-host img').getAttribute('src'),new RegExp(`scoring-top-${mode}\\.svg$`));
  }
  assert.equal(await section.locator('canvas').count(),0);
});
test('rapid coordinate and case navigation cannot attach stale canvases to inactive cases',options,async t=>{
  const page=await open(t);
  await page.route('**/mesh.json',async route=>{await new Promise(r=>setTimeout(r,100));await route.continue();});
  await page.locator('.case-detail.active [data-frame="world"]').click();
  await page.goto(base+'#fixture=B');await page.goto(base+'#fixture=A');await ready(page);
  assert.equal(await page.locator('.viewer-mount canvas').count(),1);
  assert.equal(await page.locator('.case-detail:not(.active) canvas').count(),0);
  assert.equal((await state(page)).frame,'scoring');
});
test('legacy and malformed hash links have predictable canonical routes',options,async t=>{
  const page=await open(t);
  await page.goto(base+'#case-1');await ready(page);
  assert.equal(new URL(page.url()).hash,'#fixture=B');
  await page.goto(base+'#fixture=%E0%A4');
  await page.locator('#overview.active').waitFor();
  assert.equal(new URL(page.url()).hash,'#overview');
  await page.goBack();await ready(page);
  assert.equal(new URL(page.url()).hash,'#fixture=B');
});
test('case search finds enterprise identifiers and original titles',options,async t=>{
  const page=await open(t);await page.goto(base+'#overview');
  for(const query of ['E005','Guide pin']) {
    await page.locator('#case-search').fill(query);
    const visible=page.locator('.case-card:visible');assert.equal(await visible.count(),1);
    assert.equal(await visible.getAttribute('href'),'#fixture=A');
  }
});
test('difference legend distinguishes display tolerance from raw intersection',options,async t=>{
  const page=await open(t),section=page.locator('.case-detail.active');
  await section.locator('[data-mode="difference"]').click();
  assert.equal(await section.locator('.viewer-difference-legend .matched').textContent(),'重合或容差内');
  assert.match(await section.locator('.viewer-difference-legend').textContent(),/0\.01 mm/);
  await section.locator('[data-mode="difference_raw"]').click();
  assert.equal(await section.locator('.viewer-difference-legend .matched').textContent(),'重合');
});
test('surface download failure discloses the raw-volume fallback instead of claiming a tolerance',options,async t=>{
  const page=await browser.newPage();t.after(()=>page.close());
  await page.route('**/surface.json',route=>route.abort());
  await page.goto(base+'#fixture=A');await ready(page);
  const section=page.locator('.case-detail.active');await section.locator('[data-mode="difference"]').click();
  assert.equal(await section.locator('.viewer-difference-legend .matched').textContent(),'重合');
  assert.match(await section.locator('.viewer-status').textContent(),/回退原始体积差异/);
});
test('enlarged comparison switches modes at the same angle and preserves zoom',options,async t=>{
  const page=await open(t);
  await page.locator('.case-detail.active [data-preview-kind="split"] [data-angle="front"] a').click();
  await page.waitForFunction(()=>document.querySelector('.lightbox-image').naturalWidth>0);
  await page.locator('[data-lightbox-zoom="actual"]').click();
  await page.locator('[data-lightbox-mode="overlay"]').click();
  await page.waitForFunction(()=>document.querySelector('.lightbox-image').complete);
  assert.match(await page.locator('.lightbox-image').getAttribute('src'),/front-overlay\.svg$/);
  assert.equal(await page.locator('.lightbox-zoom-level').textContent(),'100%');
  await page.locator('[data-lightbox-mode="difference"]').click();
  assert.match(await page.locator('.lightbox-image').getAttribute('src'),/front-difference\.svg$/);
});
test('difference layers can be isolated and reset restores every layer',options,async t=>{
  const page=await open(t),s=page.locator('.case-detail.active');
  await s.locator('button[data-mode="difference_raw"]').click();
  const canvas=s.locator('canvas'),before=await canvas.screenshot();
  await s.locator('[data-difference-layer="matched"]').uncheck();
  const after=await canvas.screenshot();assert.notEqual(createHash('sha256').update(before).digest('hex'),createHash('sha256').update(after).digest('hex'));
  await s.locator('[data-reset]').click();await ready(page);
  await s.locator('button[data-mode="difference_raw"]').click();
  assert.equal(await s.locator('[data-difference-layer="matched"]').isChecked(),true);
});
