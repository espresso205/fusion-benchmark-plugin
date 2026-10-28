import {test} from 'node:test';
import assert from 'node:assert/strict';
import {existsSync,mkdtempSync,readFileSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';

const moduleURL = new URL('../report_assets/viewer/comparison-view.js', import.meta.url);
const triangle = {vertices:[0,0,0,10,0,0,0,10,0], indices:[0,1,2]};

async function inspectionView() {
  const {ComparisonView}=await import(moduleURL);
  const THREE=await import('../report_assets/vendor/three.module.js');
  const {prepareGeometry}=await import('../report_assets/viewer/preview-geometry-worker.js');
  const view=Object.create(ComparisonView.prototype);
  Object.assign(view,{host:{dataset:{},title:''},scene:new THREE.Scene(),models:{},layers:{},
    workers:[],meshToken:0,frame:0,disposed:false,width:960,height:360,mode:'split',
    camera:new THREE.OrthographicCamera(-1,1,1,-1,.01,100000),target:new THREE.Vector3(),
    radius:80,azimuth:0,elevation:0,visible:{candidate:true,reference:true},
    opacity:{candidate:.55,reference:.2},request(){},
    prepare:async (positions,indices)=>prepareGeometry({positions,indices}),
    renderer:{setScissorTest(){},setScissor(){},setViewport(){},render(){}}});
  return view;
}

test('standard directions match the static render convention including top-up Y',async()=>{
  const view=await inspectionView();
  const directions={iso:[1,-1,1],front:[0,-1,0],top:[0,0,1],right:[1,0,0]};
  for(const [name,direction] of Object.entries(directions)){
    view.setView(name);view.draw();
    const actual=view.camera.position.clone().sub(view.target).normalize().toArray();
    const length=Math.hypot(...direction);
    direction.forEach((value,index)=>assert.ok(Math.abs(actual[index]-value/length)<1e-12,`${name} direction ${index}`));
    assert.deepEqual(view.camera.up.toArray(),name==='top'?[0,1,0]:[0,0,1]);
  }
});

test('mode changes preserve camera inspection and reset restores the common fitted ISO view',async()=>{
  const view=await inspectionView(),events=[];
  view.onViewChange=state=>events.push(state);
  await view.setMeshes({candidate:triangle,reference:{...triangle,vertices:triangle.vertices.map((v,i)=>v+(i%3===0?20:0))}});
  view.setView('front');view.target.set(7,8,9);view.camera.zoom=3;
  view.setMode('overlay');
  assert.deepEqual(view.target.toArray(),[7,8,9]);assert.equal(view.camera.zoom,3);
  view.resetView();view.draw();
  assert.deepEqual(view.target.toArray(),[0,0,0]);assert.equal(view.camera.zoom,1);
  assert.equal(events.at(-1).view,'iso');assert.equal(events.at(-1).mode,'overlay');
  assert.equal(events.at(-1).custom,false);
});

test('difference partitions use one world origin and distinct matched/extra/missing materials',async()=>{
  const view=await inspectionView();
  const translate=x=>({...triangle,vertices:triangle.vertices.map((v,i)=>v+(i%3===0?1e9+x:1e9))});
  const outcome=await view.setMeshes({candidate:translate(10),reference:translate(0)},
    {status:'ready',scope:'whole_model',layers:{matched:translate(1),extra:translate(11),missing:translate(2)}});
  assert.equal(outcome.difference.status,'ready');
  view.setMode('difference');view.display('difference');
  assert.equal(view.models.candidate.visible,false);assert.equal(view.models.reference.visible,false);
  const ghost=view.models.candidate.children[0];
  assert.equal(ghost.visible,true);
  assert.deepEqual(Object.keys(view.layers).sort(),['extra','matched','missing']);
  assert.equal(view.layers.extra.children[0].geometry.attributes.position.array[0]-view.layers.matched.children[0].geometry.attributes.position.array[0],10);
  for(const [name,color] of [['matched',0x35baf2],['extra',0xef4444],['missing',0xf59e0b]]){
    assert.equal(view.layers[name].visible,true);
    assert.equal(view.layers[name].children[0].material.color.getHex(),color);
    const body=view.layers[name].children[0];
    assert.equal(body.material.transparent,false);
    assert.equal(body.material.opacity,1);
    assert.equal(body.material.depthWrite,true,'opaque differences must obey physical occlusion');
    assert.equal(view.layers[name].children[1].visible,false,'Boolean cut edges must not become colored stripes');
  }
  view.display('candidate');
  assert.equal(view.models.candidate.visible,true);
  assert.equal(ghost.material.color.getHex(),0xffa34c);assert.equal(ghost.material.opacity,1);
  assert.equal(ghost.material.transparent,false);assert.equal(ghost.material.depthWrite,true);
  assert.ok(Object.values(view.layers).every(layer=>!layer.visible));
  view.clear();
});

test('failed difference layers retain valid layers and empty partitions are not failures',async()=>{
  const view=await inspectionView();
  const partial=await view.setMeshes({candidate:triangle},{status:'ready',scope:'interface',
    layers:{matched:triangle,extra:{vertices:[],indices:[]},missing:{...triangle,indices:[0,1,99]}}});
  assert.equal(partial.status,'ready');assert.equal(partial.difference.status,'partial');
  assert.equal(partial.difference.scope,'interface');assert.deepEqual(Object.keys(view.layers),['matched']);
  const empty=await view.setMeshes({candidate:triangle},{status:'ready',scope:'whole_model',
    layers:{matched:null,extra:{vertices:[],indices:[]},missing:null}});
  assert.equal(empty.difference.status,'empty');assert.deepEqual(Object.keys(view.layers),[]);
  const unavailable=await view.setMeshes({candidate:triangle},{status:'unavailable',scope:'interface',
    reason:'interface_not_supplied',layers:{}});
  assert.equal(unavailable.difference.status,'unavailable');
  assert.equal(unavailable.difference.reason,'interface_not_supplied');
  view.clear();
});

test('surface and raw difference layers are exclusive and internal differences can be isolated',async()=>{
  const view=await inspectionView();
  await view.setMeshes({candidate:triangle,reference:triangle},{status:'completed',
    layers:{matched:triangle,missing:triangle},surface_layers:{matched:triangle},
    visualization:{kind:'surface-distance-tolerance',tolerance_mm:.02}});
  view.setMode('difference');view.display('difference');
  assert.equal(view.surfaceLayers.matched.visible,true);
  assert.ok(Object.values(view.layers).every(layer=>!layer.visible));
  view.setMode('difference_raw');view.setDifferenceLayers({matched:false});view.display('difference_raw');
  assert.equal(view.layers.matched.visible,false);assert.equal(view.layers.missing.visible,true);
  assert.ok(Object.values(view.surfaceLayers).every(layer=>!layer.visible));
  assert.ok(Object.values(view.models).every(layer=>!layer.visible));view.clear();
});

test('failed surface preparation falls back as one unit and removes the tolerance claim',async()=>{
  const view=await inspectionView();
  await view.setMeshes({candidate:triangle,reference:triangle},{status:'completed',
    layers:{matched:triangle,extra:triangle},surface_layers:{matched:triangle,extra:{...triangle,indices:[0,1,999]}},
    visualization:{kind:'surface-distance-tolerance',tolerance_mm:.02}});
  assert.deepEqual(Object.keys(view.surfaceLayers),[]);
  assert.equal(view.difference.visualization.tolerance_mm,0);
  assert.equal(view.difference.visualization.kind,'raw-volume');
  view.display('difference');assert.ok(Object.values(view.layers).every(layer=>layer.visible));view.clear();
});

test('completed and ok backend partitions enable difference inspection',async()=>{
  const view=await inspectionView();
  for(const status of ['ok','completed']){
    const result=await view.setMeshes({candidate:triangle},{status,scope:'whole_model',layers:{extra:triangle}});
    view.setMode('difference');view.display('difference');
    assert.equal(result.difference.status,'ready');
    assert.equal(view.layers.extra?.visible,true,`${status} material partition must be visible`);
  }
  view.clear();
});

test('one malformed model does not suppress the remaining model or valid difference partition',async()=>{
  const view=await inspectionView();
  const result=await view.setMeshes({candidate:triangle,reference:{...triangle,indices:[-1,1,2]}},
    {status:'partial',scope:'whole_model',reason:'reference_unavailable',layers:{extra:triangle}});
  assert.equal(result.status,'ready');assert.deepEqual(Object.keys(view.models),['candidate']);
  assert.deepEqual(Object.keys(view.layers),['extra']);assert.equal(result.difference.status,'partial');
  view.clear();
});

test('case clearing disposes every difference geometry and material exactly once',async()=>{
  const view=await inspectionView();
  await view.setMeshes({candidate:triangle},{status:'ready',scope:'whole_model',layers:{matched:triangle,extra:triangle}});
  const resources=[];
  for(const group of [...Object.values(view.models),...Object.values(view.layers)])group.traverse(child=>{
    for(const resource of [child.geometry,child.material].filter(Boolean)){
      const entry={disposed:0};resource.addEventListener('dispose',()=>entry.disposed++);resources.push(entry);
    }
  });

  assert.equal(resources.length,12);
  view.clear();view.clear();
  assert.ok(resources.every(resource=>resource.disposed===1));
  assert.equal(view.scene.children.length,0);assert.deepEqual(view.layers,{});
});

test('viewer validates triangle indices before preparing WebGL buffers', async () => {
  assert.ok(existsSync(moduleURL), 'comparison viewer implementation is required');
  const {validateMesh} = await import('../report_assets/viewer/mesh-validation.js');
  validateMesh(triangle);
  for (const indices of [[0,1,3], [0,-1,2], [0,1.5,2], [0,1]]) {
    assert.throws(() => validateMesh({...triangle,indices}));
  }
  assert.throws(() => validateMesh({...triangle,vertices:[NaN,...triangle.vertices.slice(1)]}));
});

test('geometry preparation retains shared coordinates and creates normals', async () => {
  assert.ok(existsSync(moduleURL), 'comparison viewer implementation is required');
  const {prepareGeometry} = await import('../report_assets/viewer/preview-geometry-worker.js');
  const positions = new Float32Array([30,0,0,41,0,0,30,11,0]);
  const result = prepareGeometry({positions,indices:new Uint32Array([0,1,2])});
  assert.deepEqual([...result.positions], [30,0,0,41,0,0,30,11,0]);
  assert.equal(result.normals.length,9);
  assert.equal(result.normals[2],1);
  assert.ok(result.edges.length > 0);
});

test('changing cases clears old meshes immediately and cancels old preparation', async () => {
  assert.ok(existsSync(moduleURL), 'comparison viewer implementation is required');
  const {ComparisonView} = await import(moduleURL);
  const THREE = await import('../report_assets/vendor/three.module.js');
  const view = Object.create(ComparisonView.prototype);
  const geometry = new THREE.BufferGeometry();
  let disposed = 0; geometry.addEventListener('dispose',()=>disposed++);
  const old = new THREE.Group(); old.add(new THREE.Mesh(geometry,new THREE.MeshBasicMaterial()));
  Object.assign(view, {host:{dataset:{},title:''}, scene:new THREE.Scene(), models:{reference:old},
    workers:[], meshToken:0, frame:0, disposed:false, request(){}, fit(){}});
  view.scene.add(old);
  const workers = [];
  const OriginalWorker = globalThis.Worker;
  globalThis.Worker = class {
    constructor(){workers.push(this);}
    postMessage(){}
    terminate(){this.terminated=true;}
  };
  try {
    const first = view.setMeshes({reference:triangle});
    assert.equal(Object.keys(view.models).length,0);
    assert.equal(view.scene.children.length,0);
    assert.equal(disposed,1);
    const second = view.setMeshes({});
    await Promise.all([first,second]);
    assert.ok(workers[0].terminated);
    assert.equal(Object.keys(view.models).length,0);
    assert.equal(view.host.dataset.geometryState,'unavailable');
  } finally {view.clear(); globalThis.Worker=OriginalWorker;}
});

test('a stalled geometry worker times out and is terminated', async (context) => {
  const {ComparisonView} = await import(moduleURL);
  const view=Object.create(ComparisonView.prototype);view.workers=[];
  const workers=[];const OriginalWorker=globalThis.Worker;
  globalThis.Worker=class {
    constructor(){workers.push(this);}
    postMessage(){}
    terminate(){this.terminated=true;}
  };
  context.mock.timers.enable({apis:['setTimeout']});
  try {
    const pending=view.prepare(new Float32Array(triangle.vertices),new Uint32Array(triangle.indices));
    const rejected=assert.rejects(pending,/mesh_prepare_timeout/);
    context.mock.timers.tick(30001);
    await rejected;
    assert.ok(workers[0].terminated);
    assert.equal(view.workers.length,0);
  } finally {context.mock.timers.reset();globalThis.Worker=OriginalWorker;}
});

test('one common origin preserves small differences at large world coordinates', async () => {
  const {commonOrigin}=await import('../report_assets/viewer/mesh-validation.js');
  const origin=commonOrigin({reference:{vertices:[1e9,1e9,1e9,1e9+10,1e9+20,1e9+30]},
    candidate:{vertices:[1e9+30,1e9,1e9,1e9+41,1e9+22,1e9+33]}});
  assert.deepEqual(origin,[1e9+20.5,1e9+11,1e9+16.5]);
  const ref=Float32Array.from([1e9,1e9+10],value=>value-origin[0]);
  const cand=Float32Array.from([1e9+30,1e9+41],value=>value-origin[0]);
  assert.equal(cand[1]-cand[0],11);
  assert.equal(ref[1]-ref[0],10);
  assert.equal(cand[0]-ref[0],30);
});

test('split view renders candidate on the left and reference on the right', async () => {
  const {ComparisonView}=await import(moduleURL);
  const view=Object.create(ComparisonView.prototype),rendered=[];
  let viewport,role;
  Object.assign(view,{mode:'split',width:960,height:360,radius:80,azimuth:0,elevation:0,
    target:{x:0,y:0,z:0},camera:{position:{set(){}},lookAt(){}},
    renderer:{setScissorTest(){},setScissor(){},setViewport(x,y,width,height){viewport=[x,y,width,height];},
      render(){rendered.push({role,viewport});}},display(value){role=value;}});
  view.draw();
  assert.deepEqual(rendered,[{role:'candidate',viewport:[0,0,480,360]},
    {role:'reference',viewport:[480,0,480,360]}]);
});

const browserPath=process.env.VIEWER_TEST_BROWSER || [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/chromium','/usr/bin/chromium-browser','/usr/bin/google-chrome'
].find(existsSync);

test('browser canvas remains bounded and direction axes follow the camera and are removed on disposal',
  {skip:!browserPath,timeout:25000},()=>{
    const folder=mkdtempSync(join(tmpdir(),'viewer-layout-'));
    const page=join(folder,'layout.html');
    writeFileSync(page,`<!doctype html><style>
      body{margin:0}#grid{display:grid;grid-template-columns:1fr;width:613px}
      #host{height:300px}
      </style><div id="grid"><div id="host"></div></div><pre id="result">pending</pre>
      <script type="module">
      import {ComparisonView} from ${JSON.stringify(moduleURL.href)};
      const result=document.querySelector('#result'),host=document.querySelector('#host');
      try {
        const events=[],viewer=new ComparisonView(host,{onViewChange:state=>events.push(state)}),samples=[];
        for(let i=0;i<8;i++){
          await new Promise(resolve=>setTimeout(resolve,80));
          const canvas=host.querySelector('canvas');
          samples.push({hostWidth:host.getBoundingClientRect().width,
            canvasWidth:canvas.getBoundingClientRect().width,
            canvasHeight:canvas.getBoundingClientRect().height,
            bufferWidth:canvas.width,bufferHeight:canvas.height});
        }
        const axes=()=>Object.fromEntries([...host.querySelectorAll('[data-direction-axes] line')].map(line=>
          [line.dataset.axis,['x1','y1','x2','y2'].map(key=>Number(line.getAttribute(key)))]));
        viewer.setView('top');viewer.draw();const topAxes=axes();
        viewer.setView('front');viewer.draw();const frontAxes=axes();
        host.dispatchEvent(new WheelEvent('wheel',{deltaY:-1}));
        const state=events.at(-1);viewer.dispose();
        const eventCount=events.length;host.dispatchEvent(new WheelEvent('wheel',{deltaY:-1}));
        result.textContent=JSON.stringify({dpr:devicePixelRatio,samples,topAxes,frontAxes,state,
          leakedEvents:events.length-eventCount,remainingNodes:host.children.length});
      }catch(error){result.textContent=JSON.stringify({error:String(error)});}
      </script>`);
    try {
      const run=spawnSync(browserPath,['--headless=new','--no-sandbox','--disable-gpu-sandbox',
        '--use-angle=swiftshader','--enable-unsafe-swiftshader','--force-device-scale-factor=2',
        '--allow-file-access-from-files','--no-first-run','--no-default-browser-check',
        `--user-data-dir=${join(folder,'profile')}`,'--virtual-time-budget=3000','--dump-dom',pathToFileURL(page).href],
        {encoding:'utf8',timeout:20000,windowsHide:true,maxBuffer:2e6});
      assert.equal(run.status,0,run.error?.message || run.stderr);
      const match=run.stdout.match(/<pre id="result">(.*?)<\/pre>/s);
      assert.ok(match,'browser must return measured layout');
      const measured=JSON.parse(match[1]);assert.equal(measured.error,undefined,measured.error);
      assert.equal(measured.dpr,2);assert.equal(measured.samples.length,8);
      for(const sample of measured.samples){
        assert.equal(sample.hostWidth,613,'canvas drawing buffer must not grow the grid');
        assert.equal(sample.canvasWidth,613,'CSS canvas width must follow its host');
        assert.equal(sample.canvasHeight,300,'CSS canvas height must follow its host');
        assert.equal(sample.bufferWidth,1226);assert.equal(sample.bufferHeight,600);
      }
      assert.ok(measured.topAxes.x[2]>measured.topAxes.x[0],'top view X axis points right');
      assert.ok(measured.topAxes.y[3]<measured.topAxes.y[1],'top view Y axis points up');
      assert.ok(measured.frontAxes.z[3]<measured.frontAxes.z[1],'front view Z axis points up');
      assert.equal(measured.state.custom,false,'zoom preserves the selected standard direction');assert.ok(measured.state.zoom>1);
      assert.equal(measured.leakedEvents,0);assert.equal(measured.remainingNodes,0);
    }finally{rmSync(folder,{recursive:true,force:true,maxRetries:3,retryDelay:100});}
  });

test('offline report supports fixture links, static views, lightbox keyboard navigation and task filtering',
  {skip:!browserPath,timeout:25000},()=>{
    const folder=mkdtempSync(join(tmpdir(),'report-multiview-'));
    const repository=fileURLToPath(new URL('..',import.meta.url)),page=join(folder,'index.html');
    const fixture=spawnSync(process.env.FUSION_STEP_PYTHON||process.env.PYTHON||'python',['-B','-c',`
import copy, shutil, sys
from pathlib import Path
sys.path.insert(0, 'tests')
from test_html_reporting import public_report
from src.html_reporting import render_html
folder = Path(sys.argv[1])
data = public_report()
data['cases'].extend(copy.deepcopy(data['cases'][0]) for _ in range(2))
for index, (name, task, value) in enumerate((('part A', 'generation', .4), ('part B', 'editing', .6), ('part C', 'generation', .9))):
    row = data['cases'][index]
    row.update(case=name, task_type=task, final_score=value)
    previews = {}
    for angle in ('iso', 'front', 'top', 'right'):
        previews[angle] = {}
        for kind in ('split', 'overlay'):
            url = f'display/{index}/{angle}-{kind}.svg'
            target = folder / url
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="#dbeafe"/><text x="20" y="40">{name} {angle} {kind}</text></svg>', encoding='utf-8')
            previews[angle][kind] = url
    row['display'] = {'views': {'scoring': {'thumbnail': previews['iso']['split'], 'previews': previews,
        'difference': {'status': 'ok', 'scope': 'whole_model', 'layers': {}}}}}
(folder / 'assets').mkdir()
for name in ('report.js', 'report.css'):
    shutil.copyfile(Path('report_assets') / name, folder / 'assets' / name)
(folder / 'index.html').write_text(render_html(data), encoding='utf-8')
`,folder],{cwd:repository,encoding:'utf8',windowsHide:true,timeout:10000});
    try {
      assert.equal(fixture.status,0,fixture.error?.message||fixture.stderr);
      const probe=`<pre id="interaction-result">pending</pre><script>
      document.addEventListener('DOMContentLoaded',async()=>{
        const result=document.getElementById('interaction-result');
        const wait=()=>new Promise(resolve=>setTimeout(resolve,30));
        const check=(condition,message)=>{if(!condition)throw Error(message);};
        const key=value=>document.dispatchEvent(new KeyboardEvent('keydown',{key:value,bubbles:true}));
        const navigation=(index,label,previousDisabled,nextDisabled)=>{
          const section=document.getElementById('case-'+index);
          check(section.classList.contains('active'),'navigation_opened_wrong_case_'+index);
          check(section.querySelector('.detail-position')?.textContent.trim()===label,'wrong_detail_position_'+label);
          check(section.querySelector('[data-step="-1"]').disabled===previousDisabled,'wrong_previous_boundary_'+label);
          check(section.querySelector('[data-step="1"]').disabled===nextDisabled,'wrong_next_boundary_'+label);
          return section;
        };
        try {
          await wait();
          const first=document.getElementById('case-0');
          check(first.classList.contains('active'),'fixture_deep_link_not_opened');
          navigation(0,'1 / 3',true,false);
          first.querySelector('[data-view="top"]').click();
          const topSource=first.querySelector('.preview-host img').src;
          check(topSource.endsWith('/top-split.svg'),'offline_top_direction_not_applied');
          first.querySelector('[data-mode="difference"]').click();
          const differenceSource=first.querySelector('.preview-host img').src;
          check(differenceSource.endsWith('/top-overlay.svg'),'offline_difference_not_applied');
          first.querySelector('[data-mode="split"]').click();first.querySelector('[data-reset]').click();
          check(first.querySelector('.preview-host img').src.endsWith('/iso-split.svg'),'offline_reset_not_applied');
          const opener=first.querySelector('[data-preview-kind="split"] [data-angle="iso"] [data-lightbox]');
          opener.click();await wait();
          const lightbox=document.getElementById('image-lightbox');
          check(lightbox.open,'lightbox_not_opened');
          check(lightbox.querySelector('img').src.endsWith('/iso-split.svg'),'lightbox_wrong_start');
          key('ArrowRight');check(lightbox.querySelector('img').src.endsWith('/front-split.svg'),'lightbox_right_failed');
          key('ArrowLeft');check(lightbox.querySelector('img').src.endsWith('/iso-split.svg'),'lightbox_left_failed');
          key('Escape');await wait();
          check(!lightbox.open&&first.classList.contains('active'),'lightbox_escape_changed_case');
          check(location.hash==='#fixture=part%20A','lightbox_changed_fixture_url');
          key('j');await wait();
          check(document.getElementById('case-1').classList.contains('active'),'keyboard_next_case_failed');
          check(location.hash==='#fixture=part%20B','keyboard_next_lost_fixture_identity');
          key('k');await wait();check(first.classList.contains('active'),'keyboard_previous_case_failed');
          const directionButton=first.querySelector('[data-view="top"]');directionButton.focus();
          directionButton.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));await wait();
          check(document.getElementById('case-1').classList.contains('active'),'focused_view_button_blocked_arrow_navigation');
          key('ArrowLeft');await wait();check(first.classList.contains('active'),'arrow_previous_case_failed');
          key('Escape');await wait();check(document.getElementById('overview').classList.contains('active'),'escape_overview_failed');
          const filter=document.getElementById('task-filter');filter.value='editing';filter.dispatchEvent(new Event('change',{bubbles:true}));
          const cards=[...document.querySelectorAll('.case-card')];
          check(cards[0].hidden&&!cards[1].hidden,'task_filter_failed');
          const editingVisibleCount=cards.filter(card=>!card.hidden).length;
          filter.value='generation';filter.dispatchEvent(new Event('change',{bubbles:true}));
          check(cards.filter(card=>!card.hidden).length===2,'generation_filter_must_have_two_cases');
          cards[0].click();await wait();
          navigation(0,'1 / 2',true,false).querySelector('[data-step="1"]').click();await wait();
          navigation(2,'2 / 2',false,true);
          key('j');await wait();navigation(2,'2 / 2',false,true);
          key('k');await wait();navigation(0,'1 / 2',true,false);
          key('ArrowRight');await wait();navigation(2,'2 / 2',false,true);
          key('ArrowLeft');await wait();navigation(0,'1 / 2',true,false);
          const sort=document.getElementById('case-sort');sort.value='desc';sort.dispatchEvent(new Event('change',{bubbles:true}));
          navigation(0,'2 / 2',false,true);
          key('k');await wait();navigation(2,'1 / 2',true,false);
          const sortedVisible=[...document.querySelectorAll('.case-card')].filter(card=>!card.hidden);
          check(sortedVisible.map(card=>card.dataset.index).join(',')==='2,0','score_sort_order_not_used');
          navigation(2,'1 / 2',true,false).querySelector('[data-step="1"]').click();await wait();
          navigation(0,'2 / 2',false,true);
          location.hash='fixture=part%20B';await wait();
          navigation(1,'全部 2 / 3',false,false).querySelector('[data-step="-1"]').click();await wait();
          navigation(2,'1 / 2',true,false);
          filter.value='all';filter.dispatchEvent(new Event('change',{bubbles:true}));
          sort.value='asc';sort.dispatchEvent(new Event('change',{bubbles:true}));
          navigation(2,'3 / 3',false,true);
          key('ArrowLeft');await wait();navigation(1,'2 / 3',false,false);
          const search=document.getElementById('case-search');search.value='no matching fixture';search.dispatchEvent(new Event('input',{bubbles:true}));
          navigation(1,'全部 2 / 3',false,false);
          result.textContent=JSON.stringify({status:'passed',topSource,differenceSource,visibleCases:editingVisibleCount});
        } catch(error) {result.textContent=JSON.stringify({error:String(error)});}
      });</script>`;
      writeFileSync(page,readFileSync(page,'utf8').replace('</body>',`${probe}</body>`));
      const run=spawnSync(browserPath,['--headless=new','--no-sandbox','--allow-file-access-from-files',
        '--no-first-run','--no-default-browser-check',`--user-data-dir=${join(folder,'profile')}`,
        '--virtual-time-budget=3000','--dump-dom',`${pathToFileURL(page).href}#fixture=part%20A`],
        {encoding:'utf8',timeout:20000,windowsHide:true,maxBuffer:2e6});
      assert.equal(run.status,0,run.error?.message||run.stderr);
      const match=run.stdout.match(/<pre id="interaction-result">(.*?)<\/pre>/s);
      assert.ok(match,'offline report must remain open after interactions');
      assert.notEqual(match[1],'pending','offline browser interactions must finish');
      const outcome=JSON.parse(match[1]);assert.equal(outcome.error,undefined,outcome.error);
      assert.equal(outcome.status,'passed');assert.equal(outcome.visibleCases,1);
    } finally {rmSync(folder,{recursive:true,force:true,maxRetries:3,retryDelay:100});}
  });
