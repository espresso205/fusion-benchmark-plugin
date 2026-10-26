// Numeric validation is shared by the viewer and geometry worker.
export function validateMesh(mesh) {
  const {vertices,indices}=mesh || {};
  if((!Array.isArray(vertices)&&!ArrayBuffer.isView(vertices)) ||
     (!Array.isArray(indices)&&!ArrayBuffer.isView(indices)) ||
     !vertices.length || vertices.length%3 || !indices.length || indices.length%3 ||
     vertices.length>9000000 || indices.length>3000000)throw Error('invalid_mesh');
  for(const value of vertices)if(typeof value!=='number'||!Number.isFinite(value)||Math.abs(value)>1e15)throw Error('invalid_mesh');
  const count=vertices.length/3;
  for(const index of indices)if(!Number.isInteger(index)||index<0||index>=count)throw Error('invalid_mesh');
}

export function commonOrigin(meshes) {
  const minimum=[Infinity,Infinity,Infinity],maximum=[-Infinity,-Infinity,-Infinity];
  for(const role of ['reference','candidate']) {
    const mesh=meshes?.[role];if(!mesh)continue;
    for(let i=0;i<mesh.vertices.length;i++) {
      const axis=i%3,value=mesh.vertices[i];
      minimum[axis]=Math.min(minimum[axis],value);maximum[axis]=Math.max(maximum[axis],value);
    }
  }
  return minimum.map((value,axis)=>Number.isFinite(value)?value+(maximum[axis]-value)/2:0);
}
