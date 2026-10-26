// Adapted from CADViewer; Copyright (c) 2026 earthtojake (MIT).
// License: ../licenses/CADViewer-MIT.txt
import * as THREE from '../vendor/three.module.js';
import {validateMesh, commonOrigin} from './mesh-validation.js';

export class ComparisonView {
  constructor(host) {
    this.host = host;
    this.mode = 'split';
    this.azimuth = -.72; this.elevation = .52; this.radius = 80;
    this.target = new THREE.Vector3();
    this.models = {};
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
      this.fit(); this.request();
    };
    this.resizeObserver = new ResizeObserver(resize);
    this.resizeObserver.observe(host);
    resize();
    let dragging=false,last,panning=false;
    host.title='拖动旋转 · 滚轮缩放 · Shift 拖动平移 · 双击适配';
    this.listen('dblclick',()=>{this.fit();this.request();});
    this.listen('contextmenu',event=>event.preventDefault());
    this.listen('pointerdown',event=>{dragging=true;panning=event.shiftKey||event.button===2;last=[event.clientX,event.clientY];host.setPointerCapture(event.pointerId);});
    this.listen('pointermove',event=>{
      if(!dragging)return;
      if(panning) {
        const scale=(this.camera.top-this.camera.bottom)/this.height/this.camera.zoom;
        this.target.addScaledVector(new THREE.Vector3(1,0,0).applyQuaternion(this.camera.quaternion),-(event.clientX-last[0])*scale);
        this.target.addScaledVector(new THREE.Vector3(0,1,0).applyQuaternion(this.camera.quaternion),(event.clientY-last[1])*scale);
      } else {
        this.azimuth-=(event.clientX-last[0])*.009;
        this.elevation=Math.max(-1.42,Math.min(1.42,this.elevation+(event.clientY-last[1])*.009));
      }
      last=[event.clientX,event.clientY];this.request();
    });
    for(const type of ['pointerup','pointercancel','lostpointercapture'])this.listen(type,()=>dragging=false);
    this.listen('wheel',event=>{event.preventDefault();this.camera.zoom=Math.max(.05,Math.min(100,this.camera.zoom*(event.deltaY>0?.89:1.12)));this.camera.updateProjectionMatrix();this.request();},{passive:false});
    this.setMode('split');
  }
  request() {
    if(!this.disposed && !this.frame)this.frame=requestAnimationFrame(()=>{this.frame=0;this.draw();});
  }
  fit() {
    this.camera.aspect=(this.mode==='split'?this.width/2:this.width)/this.height;
    const box=new THREE.Box3();
    for(const group of Object.values(this.models))box.union(new THREE.Box3().setFromObject(group));
    if(!box.isEmpty()) {
      this.target.copy(box.getCenter(new THREE.Vector3()));
      const size=box.getSize(new THREE.Vector3());
      this.radius=Math.max(1,size.length()*2);
      const halfHeight=Math.max(.01,size.length())*.60*Math.max(1,1/this.camera.aspect);
      this.camera.top=halfHeight;this.camera.bottom=-halfHeight;
      this.camera.left=-halfHeight*this.camera.aspect;this.camera.right=halfHeight*this.camera.aspect;
      this.camera.zoom=1;
    }
    this.camera.near=Math.max(.001,this.radius/10000);this.camera.far=Math.max(1000,this.radius*100);
    this.camera.updateProjectionMatrix();
  }
  setMode(mode) {
    if(!['split','overlay','reference','candidate'].includes(mode))throw Error('invalid_view_mode');
    this.mode=mode;this.host.dataset.mode=mode;
    this.fit();this.request();
  }
  setView(view) {
    const angles={iso:[-.72,.52],front:[-Math.PI/2,0],top:[0,Math.PI/2-.001],right:[0,0]};
    [this.azimuth,this.elevation]=angles[view]||angles.iso;this.request();
  }
  setControls(visible,opacity) {this.visible=visible;this.opacity=opacity;this.request();}
  listen(type, callback, options) {
    this.host.addEventListener(type,callback,options);
    this.listeners.push([type,callback,options]);
  }
  clear() {
    ++this.meshToken;
    for(const job of this.workers.splice(0)) job.cancel();
    for(const group of Object.values(this.models)) {
      group.traverse(child=>{
        child.geometry?.dispose();
        for(const material of [].concat(child.material || []))material.dispose();
      });
      this.scene.remove(group);
    }
    this.models={};this.host.dataset.geometryState='empty';this.request();
  }
  dispose() {
    if(this.disposed)return;
    this.clear();this.disposed=true;
    this.resizeObserver.disconnect();
    for(const [type,callback,options] of this.listeners)this.host.removeEventListener(type,callback,options);
    this.listeners=[];
    if(this.frame)cancelAnimationFrame(this.frame);
    this.frame=0;this.renderer.dispose();this.renderer.forceContextLoss();
    this.renderer.domElement.remove();
  }
  prepare(positions, indices) {
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
      worker.postMessage({positions,indices},[positions.buffer,indices.buffer]);
    });
  }
  async setMeshes(meshes) {
    this.clear();
    if(this.disposed)return {status:'disposed'};
    const token=this.meshToken;
    this.host.dataset.geometryState='preparing';
    const prepared={};
    try {
      for(const role of ['reference','candidate'])if(meshes?.[role])validateMesh(meshes[role]);
      // Subtract exactly one shared origin before Float32 conversion. This
      // preserves relative placement and dimensions at large world coordinates.
      const origin=commonOrigin(meshes);this.displayOrigin=origin;
      for(const role of ['reference','candidate']) {
        const data=meshes?.[role];if(!data)continue;
        const positions=Float32Array.from(data.vertices,(value,index)=>value-origin[index%3]);
        const indices=new Uint32Array(data.indices);
        prepared[role]=await this.prepare(positions,indices);
        if(token!==this.meshToken)return {status:'cancelled'};
      }
    } catch(error) {
      if(token!==this.meshToken)return {status:'cancelled'};
      for(const job of this.workers.splice(0))job.cancel();
      this.host.dataset.geometryState='error';this.host.title='三维预览不可用';
      return {status:'failed',error:'mesh_prepare_failed'};
    }
    if(token!==this.meshToken)return {status:'cancelled'};
    for(const [role,color] of [['reference',0x35baf2],['candidate',0xffa34c]]) {
      const data=prepared[role];if(!data?.indices?.length)continue;
      const geometry=new THREE.BufferGeometry();
      geometry.setAttribute('position',new THREE.BufferAttribute(data.positions,3));geometry.setIndex(new THREE.BufferAttribute(data.indices,1));geometry.setAttribute('normal',new THREE.BufferAttribute(data.normals,3));
      const material=new THREE.MeshStandardMaterial({color,emissive:color,emissiveIntensity:.12,roughness:.55,metalness:.08,side:THREE.DoubleSide,polygonOffset:true,polygonOffsetFactor:1,polygonOffsetUnits:1});
      material.forceSinglePass=true;
      const body=new THREE.Mesh(geometry,material);
      const edgeGeometry=new THREE.BufferGeometry();edgeGeometry.setAttribute('position',new THREE.BufferAttribute(data.edges,3));
      const edges=new THREE.LineSegments(edgeGeometry,new THREE.LineBasicMaterial({color,transparent:true,opacity:.65}));
      const group=new THREE.Group();group.add(body,edges);this.models[role]=group;this.scene.add(group);
    }
    this.host.dataset.geometryState=Object.keys(this.models).length?'ready':'unavailable';
    this.fit();this.request();
    return {status:this.host.dataset.geometryState,displayOrigin:this.displayOrigin};
  }
  display(role) {
    for(const [key,group] of Object.entries(this.models)) {
      group.visible=this.visible[key] && (role==='overlay'||role===key);
      const [body,edges]=group.children;
      body.renderOrder=key==='reference'?0:1;
      edges.renderOrder=key==='reference'?3:2;
      const transparent=role==='overlay';
      if(body.material.transparent!==transparent){body.material.transparent=transparent;body.material.needsUpdate=true;}
      body.material.opacity=transparent?this.opacity[key]:1;
      body.material.depthWrite=!transparent;
      edges.material.depthTest=!(transparent&&key==='reference');
      edges.material.color.set(key==='reference'?0x7bddff:0xffc58b);
      edges.material.opacity=transparent?.72:.38;
    }
  }
  draw() {
    const {renderer,camera,target}=this;
    const planar=this.radius*Math.cos(this.elevation);
    camera.position.set(target.x+planar*Math.cos(this.azimuth),target.y+planar*Math.sin(this.azimuth),target.z+this.radius*Math.sin(this.elevation));
    camera.lookAt(target);
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
