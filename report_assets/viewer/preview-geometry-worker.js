// Adapted from CADViewer; Copyright (c) 2026 earthtojake (MIT).
// License: ../licenses/CADViewer-MIT.txt
import * as THREE from '../vendor/three.module.js';
import {validateMesh} from './mesh-validation.js';

// Expensive normals/crease extraction stays off the interaction thread.
export function prepareGeometry(data) {
  validateMesh({vertices:data.positions,indices:data.indices});
  const geometry=new THREE.BufferGeometry();
  let edgeGeometry;
  try {
    geometry.setAttribute('position',new THREE.BufferAttribute(data.positions,3));
    geometry.setIndex(new THREE.BufferAttribute(data.indices,1));
    geometry.computeVertexNormals();
    edgeGeometry=new THREE.EdgesGeometry(geometry,40);
    return {positions:geometry.attributes.position.array,normals:geometry.attributes.normal.array,
      indices:geometry.index.array,edges:edgeGeometry.attributes.position.array};
  } finally {geometry.dispose();edgeGeometry?.dispose();}
}

if(typeof self!=='undefined')self.onmessage=({data})=>{
  try {
    const result=prepareGeometry(data);
    self.postMessage(result,[result.positions.buffer,result.normals.buffer,result.indices.buffer,result.edges.buffer]);
  } catch {self.postMessage({error:'mesh_prepare_failed'});}
};
