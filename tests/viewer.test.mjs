import {test} from 'node:test';
import assert from 'node:assert/strict';
import {existsSync,mkdtempSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';

const moduleURL = new URL('../report_assets/viewer/comparison-view.js', import.meta.url);
const triangle = {vertices:[0,0,0,10,0,0,0,10,0], indices:[0,1,2]};

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

test('high-DPI canvas stays within a 613px grid through repeated resize observations',
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
        const viewer=new ComparisonView(host),samples=[];
        for(let i=0;i<8;i++){
          await new Promise(resolve=>setTimeout(resolve,80));
          const canvas=host.querySelector('canvas');
          samples.push({hostWidth:host.getBoundingClientRect().width,
            canvasWidth:canvas.getBoundingClientRect().width,
            canvasHeight:canvas.getBoundingClientRect().height,
            bufferWidth:canvas.width,bufferHeight:canvas.height});
        }
        result.textContent=JSON.stringify({dpr:devicePixelRatio,samples});viewer.dispose();
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
    }finally{rmSync(folder,{recursive:true,force:true,maxRetries:3,retryDelay:100});}
  });
