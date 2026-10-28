// Adapted from CADViewer; Copyright (c) 2026 earthtojake (MIT).
// License: ../licenses/CADViewer-MIT.txt
import * as THREE from '../vendor/three.module.js';
import {validateMesh, commonOrigin} from './mesh-validation.js';

const directions={iso:[-Math.PI/4,Math.atan(1/Math.sqrt(2))],front:[-Math.PI/2,0],top:[0,Math.PI/2],right:[0,0]};
const layerColors={matched:0x35baf2,extra:0xef4444,missing:0xf59e0b};
const interactionHint='拖动旋转 · 滚轮缩放 · Shift 拖动平移 · 双击重置';

export class ComparisonView {
  constructor(host,{onViewChange}={}) {
    this.host = host;
    this.mode = 'split';
    this.view='iso';this.custom=false;this.onViewChange=onViewChange;
    [this.azimuth,this.elevation]=directions.iso;this.radius = 80;
    this.target = new THREE.Vector3();
    this.models = {};this.layers={};this.surfaceLayers={};this.differenceVisible={matched:true,extra:true,missing:true};
    this.visible = {reference:true, candidate:true};
    this.opacity = {reference:.20, candidate:.55};
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color('#ffffff');
    this.camera = new THREE.OrthographicCamera(-1,1,1,-1,.01,100000);
    this.camera.up.set(0,0,1);
    this.renderer = new THREE.WebGLRenderer({antialias:true});
    // The drawing buffer uses device pixels. Its intrinsic dimensions must
    // never become the CSS size measured by our ResizeObserver.
    Object.assign(this.renderer.domElement.style,{display:'block',width:'100%',height:'100%'});
    host.style.minWidth='0';
    this.renderer.setPixelRatio(Math.min(devicePixelRatio,2));
    this.renderer.outputColorSpace=THREE.SRGBColorSpace;
    this.renderer.toneMapping=THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure=1.15;
    host.prepend(this.renderer.domElement);
    this.createAxes();
    this.scene.add(new THREE.HemisphereLight(0xdceeff,0x556579,2.2));
    const light = new THREE.DirectionalLight(0xffffff,2.5);
    light.position.set(1,-2,4); this.scene.add(light);
    const fill=new THREE.DirectionalLight(0xbddcff,1.3);fill.position.set(-2,1,1);this.scene.add(fill);
    this.workers=[];this.meshToken=0;this.disposed=false;this.listeners=[];
    this.width = 1; this.height = 1; this.frame = 0;
    const resize = () => {
      const {width,height} = host.getBoundingClientRect();
      if(!width || !height)return;
      this.width=width; this.height=height;
      this.renderer.setSize(width,height,false);
      this.updateProjection(); this.request();
    };
    this.resizeObserver = new ResizeObserver(resize);
    this.resizeObserver.observe(host);
    resize();
    let dragging=false,last,panning=false;
    host.title=interactionHint;
    this.listen('dblclick',()=>this.resetView());
    this.listen('contextmenu',event=>event.preventDefault());
    this.listen('pointerdown',event=>{dragging=true;panning=event.shiftKey||event.button===2;last=[event.clientX,event.clientY];this.pointerId=event.pointerId;host.setPointerCapture(event.pointerId);});
    this.listen('pointermove',event=>{
      if(!dragging)return;
      if(panning) {
        const scale=(this.camera.top-this.camera.bottom)/this.height/this.camera.zoom;
        this.target.addScaledVector(new THREE.Vector3(1,0,0).applyQuaternion(this.camera.quaternion),-(event.clientX-last[0])*scale);
        this.target.addScaledVector(new THREE.Vector3(0,1,0).applyQuaternion(this.camera.quaternion),(event.clientY-last[1])*scale);
      } else {
        this.camera.up.set(0,0,1);
        this.azimuth-=(event.clientX-last[0])*.009;
        this.elevation=Math.max(-1.42,Math.min(1.42,this.elevation+(event.clientY-last[1])*.009));
      }
      last=[event.clientX,event.clientY];this.custom=true;this.notifyViewChange();this.request();
    });
    for(const type of ['pointerup','pointercancel','lostpointercapture'])this.listen(type,()=>{dragging=false;this.pointerId=null;});
    this.listen('wheel',event=>{event.preventDefault();this.zoomBy(event.deltaY>0?.89:1.12);},{passive:false});
    this.setMode('split');
  }
  createAxes() {
    const namespace='http://www.w3.org/2000/svg';
    this.axes=document.createElementNS(namespace,'svg');
    this.axes.setAttribute('viewBox','0 0 88 88');this.axes.dataset.directionAxes='';
    this.axes.setAttribute('role','img');this.axes.setAttribute('aria-label','方向轴：X、Y、Z');
    Object.assign(this.axes.style,{position:'absolute',right:'10px',bottom:'10px',width:'88px',height:'88px',pointerEvents:'none'});
    this.previousPosition=this.host.style.position;
    if(getComputedStyle(this.host).position==='static')this.host.style.position='relative';
    this.axisElements=[];
    for(const [name,color,direction] of [['x','#b42318',[1,0,0]],['y','#21833b',[0,1,0]],['z','#176ab8',[0,0,1]]]) {
      const line=document.createElementNS(namespace,'line'),label=document.createElementNS(namespace,'text');
      line.dataset.axis=name;line.setAttribute('stroke',color);line.setAttribute('stroke-width','2');
      label.textContent=name.toUpperCase();label.setAttribute('fill',color);label.setAttribute('font-size','12');
      label.setAttribute('font-family','sans-serif');label.setAttribute('font-weight','600');
      label.setAttribute('text-anchor','middle');label.setAttribute('dominant-baseline','central');
      this.axes.append(line,label);this.axisElements.push({line,label,direction:new THREE.Vector3(...direction)});
    }
    this.host.append(this.axes);
  }
  updateAxes() {
    if(!this.axisElements)return;
    const inverse=this.camera.quaternion.clone().invert();
    for(const {line,label,direction} of this.axisElements) {
      const projected=direction.clone().applyQuaternion(inverse);
      line.setAttribute('x1',44);line.setAttribute('y1',44);
      line.setAttribute('x2',44+projected.x*27);line.setAttribute('y2',44-projected.y*27);
      label.setAttribute('x',44+projected.x*36);label.setAttribute('y',44-projected.y*36);
      label.setAttribute('opacity',Math.abs(projected.z)>.99?'.55':'1');
    }
  }
  notifyViewChange() {
    if(this.disposed)return;
    this.host.dataset.view=this.custom?'custom':this.view;
    this.onViewChange?.({view:this.view,mode:this.mode,zoom:this.camera.zoom,custom:!!this.custom});
  }
  request() {
    if(!this.disposed && !this.frame)this.frame=requestAnimationFrame(()=>{this.frame=0;this.draw();});
  }
  fit() {
    const box=new THREE.Box3();
    for(const group of [...Object.values(this.models),...Object.values(this.layers||{}),...Object.values(this.surfaceLayers||{})])box.union(new THREE.Box3().setFromObject(group));
    if(!box.isEmpty()) {
      this.target.copy(box.getCenter(new THREE.Vector3()));
      const size=box.getSize(new THREE.Vector3());
      this.radius=Math.max(1,size.length()*2);
      this.fitSpan=Math.max(.01,size.length())*.60;
    } else {
      this.target.set(0,0,0);this.radius=80;this.fitSpan=1;
    }
    this.camera.zoom=1;this.updateProjection();
  }
  updateProjection() {
    this.camera.aspect=(this.mode==='split'?this.width/2:this.width)/this.height;
    const halfHeight=(this.fitSpan||1)*Math.max(1,1/this.camera.aspect);
    this.camera.top=halfHeight;this.camera.bottom=-halfHeight;
    this.camera.left=-halfHeight*this.camera.aspect;this.camera.right=halfHeight*this.camera.aspect;
    this.camera.near=Math.max(.001,this.radius/10000);this.camera.far=Math.max(1000,this.radius*100);
    this.camera.updateProjectionMatrix();
  }
  setMode(mode) {
    if(!['split','overlay','reference','candidate','difference','difference_raw'].includes(mode))throw Error('invalid_view_mode');
    this.mode=mode;this.host.dataset.mode=mode;
    this.updateProjection();this.notifyViewChange();this.request();
  }
  setView(view) {
    this.view=Object.hasOwn(directions,view)?view:'iso';this.custom=false;
    [this.azimuth,this.elevation]=directions[this.view];
    this.camera.up.set(0,this.view==='top'?1:0,this.view==='top'?0:1);
    this.notifyViewChange();this.request();
  }
  resetView() {this.fit();this.setView('iso');}
  setZoom(value) {this.camera.zoom=Math.max(.05,Math.min(100,Number(value)||1));this.camera.updateProjectionMatrix();this.notifyViewChange();this.request();}
  zoomBy(factor) {this.setZoom(this.camera.zoom*factor);}
  setDifferenceLayers(visible) {this.differenceVisible={matched:true,extra:true,missing:true,...visible};this.request();}
  getCameraState() {return {azimuth:this.azimuth,elevation:this.elevation,up:this.camera.up.toArray(),view:this.view,custom:!!this.custom,zoom:this.camera.zoom};}
  setCameraState(state) {
    if(!state||![state.azimuth,state.elevation].every(Number.isFinite))return;
    this.azimuth=state.azimuth;this.elevation=state.elevation;this.view=Object.hasOwn(directions,state.view)?state.view:'iso';this.custom=!!state.custom;
    if(Array.isArray(state.up)&&state.up.length===3&&state.up.every(Number.isFinite))this.camera.up.fromArray(state.up);
    this.setZoom(state.zoom||1);
  }
  setControls(visible,opacity) {this.visible=visible;this.opacity=opacity;this.request();}
  listen(type, callback, options) {
    this.host.addEventListener(type,callback,options);
    this.listeners.push([type,callback,options]);
  }
  clear() {
    ++this.meshToken;
    for(const job of this.workers.splice(0)) job.cancel();
    for(const group of [...Object.values(this.models),...Object.values(this.layers||{}),...Object.values(this.surfaceLayers||{})]) {
      group.traverse(child=>{
        child.geometry?.dispose();
        for(const material of [].concat(child.material || []))material.dispose();
      });
      this.scene.remove(group);
    }
    this.models={};this.layers={};this.surfaceLayers={};this.difference={status:'unavailable',scope:'whole_model'};
    this.host.dataset.geometryState='empty';this.host.dataset.differenceState='unavailable';this.request();
  }
  dispose() {
    if(this.disposed)return;
    this.clear();this.disposed=true;
    this.resizeObserver.disconnect();
    if(this.pointerId!=null&&this.host.hasPointerCapture(this.pointerId))this.host.releasePointerCapture(this.pointerId);
    for(const [type,callback,options] of this.listeners)this.host.removeEventListener(type,callback,options);
    this.listeners=[];
    if(this.frame)cancelAnimationFrame(this.frame);
    this.frame=0;this.renderer.dispose();this.renderer.forceContextLoss();
    this.renderer.domElement.remove();
    this.axes?.remove();this.axisElements=[];
    this.host.style.position=this.previousPosition;
    this.onViewChange=null;
  }
  prepare(positions, indices, skipEdges=false) {
    return new Promise((resolve,reject)=>{
      const worker=new Worker(new URL('./preview-geometry-worker.js',import.meta.url),{type:'module'});
      let settled=false;
      const done=(error,data)=>{
        if(settled)return;
        settled=true;clearTimeout(timer);worker.terminate();
        this.workers=this.workers.filter(item=>item!==job);
        error?reject(Error(error)):resolve(data);
      };
      const job={cancel:()=>done('mesh_cancelled')};
      const timer=setTimeout(()=>done('mesh_prepare_timeout'),30000);
      this.workers.push(job);
      worker.onmessage=({data})=>done(data.error?'mesh_prepare_failed':null,data);
      worker.onerror=()=>done('mesh_prepare_failed');
      try {worker.postMessage({positions,indices,skipEdges},[positions.buffer,indices.buffer]);}
      catch {done('mesh_prepare_failed');}
    });
  }
  async setMeshes(meshes,difference) {
    this.clear();
    if(this.disposed)return {status:'disposed'};
    const token=this.meshToken;
    this.host.dataset.geometryState='preparing';
    const prepared={},valid={},failed=[];
    const canShowDifference=['ok','completed','ready','partial','empty'].includes(difference?.status);
    const entries={reference:meshes?.reference,candidate:meshes?.candidate,
      ...(canShowDifference?Object.fromEntries(Object.keys(layerColors).map(key=>[key,difference.layers?.[key]])):{}),
      ...(canShowDifference?Object.fromEntries(Object.keys(layerColors).map(key=>['surface_'+key,difference.surface_layers?.[key]])):{})};
    for(const [role,data] of Object.entries(entries)) {
      if(data==null)continue;
      if((role in layerColors||role.startsWith('surface_'))&&Array.isArray(data.vertices)&&Array.isArray(data.indices)&&!data.vertices.length&&!data.indices.length)continue;
      try {validateMesh(data);valid[role]=data;} catch {failed.push(role);}
    }
    // Use one origin for all ordinary models and Boolean partitions before
    // Float32 conversion, retaining their relative world-space placement.
    const origin=commonOrigin(valid);this.displayOrigin=origin;
    for(const [role,data] of Object.entries(valid)) {
      try {
        const positions=Float32Array.from(data.vertices,(value,index)=>value-origin[index%3]);
        const indices=new Uint32Array(data.indices);
        prepared[role]=await this.prepare(positions,indices,role in layerColors||role.startsWith('surface_'));
        if(token!==this.meshToken)return {status:'cancelled'};
      } catch {
        if(token!==this.meshToken)return {status:'cancelled'};
        failed.push(role);
      }
    }
    if(token!==this.meshToken)return {status:'cancelled'};
    if(failed.some(role=>role.startsWith('surface_')))
      for(const role of Object.keys(prepared))if(role.startsWith('surface_'))delete prepared[role];
    for(const [role,color] of [['reference',0x35baf2],['candidate',0xffa34c],...Object.entries(layerColors),...Object.entries(layerColors).map(([key,color])=>['surface_'+key,color])]) {
      const data=prepared[role];if(!data?.indices?.length)continue;
      const geometry=new THREE.BufferGeometry();
      geometry.setAttribute('position',new THREE.BufferAttribute(data.positions,3));geometry.setIndex(new THREE.BufferAttribute(data.indices,1));geometry.setAttribute('normal',new THREE.BufferAttribute(data.normals,3));
      const material=new THREE.MeshStandardMaterial({color,emissive:color,emissiveIntensity:.12,roughness:.55,metalness:.08,side:THREE.DoubleSide,polygonOffset:true,polygonOffsetFactor:1,polygonOffsetUnits:1});
      material.forceSinglePass=true;
      const body=new THREE.Mesh(geometry,material);
      const edgeGeometry=new THREE.BufferGeometry();edgeGeometry.setAttribute('position',new THREE.BufferAttribute(data.edges,3));
      const edges=new THREE.LineSegments(edgeGeometry,new THREE.LineBasicMaterial({color,transparent:true,opacity:.65}));
      const group=new THREE.Group();group.add(body,edges);
      (role.startsWith('surface_')?this.surfaceLayers:role in layerColors?this.layers:this.models)[role.replace(/^surface_/,'')]=group;this.scene.add(group);
    }
    const layerFailure=failed.some(role=>role in layerColors),layerCount=Object.keys(this.layers).length;
    let differenceStatus=difference?.status||'unavailable';
    if(canShowDifference)differenceStatus=layerFailure?(layerCount?'partial':'unavailable'):
      difference?.status==='partial'?'partial':layerCount?'ready':'empty';
    this.difference={status:differenceStatus,scope:difference?.scope==='interface'?'interface':'whole_model',
      ...(difference?.visualization?{visualization:difference.visualization}:{}),surface_available:Object.keys(this.surfaceLayers).length>0,
      ...(difference?.reason?{reason:difference.reason}:{}),...(layerFailure?{reason:'difference_layer_prepare_failed'}:{})};
    if(difference?.surface_layers&&!this.difference.surface_available)
      this.difference.visualization={kind:'raw-volume',tolerance_mm:0,raw_available:true,description:'表面显示层不可用，展示未经容差过滤的原始体积差异。'};
    this.host.dataset.differenceState=differenceStatus;
    this.host.dataset.geometryState=Object.keys(this.models).length||layerCount?'ready':failed.length?'error':'unavailable';
    this.host.title=this.host.dataset.geometryState==='error'?'三维预览不可用':interactionHint;
    this.fit();this.notifyViewChange();this.request();
    return {status:this.host.dataset.geometryState==='error'?'failed':this.host.dataset.geometryState,
      displayOrigin:this.displayOrigin,difference:this.difference};
  }
  display(role) {
    const surface=role==='difference'&&Object.keys(this.surfaceLayers||{}).length>0;
    for(const [kind,collection] of [['raw',this.layers||{}],['surface',this.surfaceLayers||{}]])for(const [key,group] of Object.entries(collection)) {
      group.visible=(role==='difference_raw'&&kind==='raw'||role==='difference'&&kind===(surface?'surface':'raw'))&&(this.differenceVisible?.[key]!==false);
      const [body,edges]=group.children;
      if(body.material.transparent){body.material.transparent=false;body.material.needsUpdate=true;}
      body.material.opacity=1;body.material.depthWrite=true;body.material.depthTest=true;body.renderOrder=0;
      body.material.polygonOffset=key==='matched';
      body.material.polygonOffsetFactor=1;body.material.polygonOffsetUnits=1;
      edges.visible=false;
    }
    for(const [key,group] of Object.entries(this.models)) {
      group.visible=this.visible[key] && (role==='overlay'||role===key);
      const [body,edges]=group.children;
      body.renderOrder=key==='reference'?0:1;
      edges.renderOrder=key==='reference'?3:2;
      const color=key==='reference'?0x35baf2:0xffa34c;
      body.material.color.set(color);body.material.emissive.set(color);
      const transparent=role==='overlay';
      if(body.material.transparent!==transparent){body.material.transparent=transparent;body.material.needsUpdate=true;}
      body.material.opacity=transparent?this.opacity[key]:1;
      body.material.depthWrite=!transparent;
      edges.material.depthTest=true;edges.visible=true;
      edges.material.depthWrite=!transparent;
      edges.material.color.set(key==='reference'?0x7bddff:0xffc58b);
      edges.material.opacity=transparent?.72*this.opacity[key]:.38;
    }
  }
  draw() {
    const {renderer,camera,target}=this;
    const planar=this.radius*Math.cos(this.elevation);
    camera.position.set(target.x+planar*Math.cos(this.azimuth),target.y+planar*Math.sin(this.azimuth),target.z+this.radius*Math.sin(this.elevation));
    camera.lookAt(target);
    this.updateAxes();
    if(this.mode==='split') {
      const left=Math.floor(this.width/2);
      renderer.setScissorTest(true);
      for(const [role,x,width] of [['candidate',0,left],['reference',left,this.width-left]]) {
        renderer.setViewport(x,0,width,this.height);renderer.setScissor(x,0,width,this.height);
        this.display(role);renderer.render(this.scene,camera);
      }
      renderer.setScissorTest(false);
    } else {
      renderer.setViewport(0,0,this.width,this.height);this.display(this.mode);renderer.render(this.scene,camera);
    }
  }
}
