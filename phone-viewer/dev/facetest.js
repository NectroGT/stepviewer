const fs=require('fs'); global.THREE=require(process.argv[2]+'/three.min.js');
const html=fs.readFileSync(process.argv[2]+'/phone-viewer/index.html','utf8');
const code=html.slice(html.indexOf('function faceIndexOf'), html.indexOf('function meshStats'));
eval(code+';global.analyzeFace=analyzeFace;');
require(process.argv[2]+'/phone-viewer/occt-import-js.js');
const occt=require(process.argv[2]+'/phone-viewer/occt-import-js.js');
occt().then(m=>{
  const r=m.ReadStepFile(new Uint8Array(fs.readFileSync(process.argv[3])),{linearUnit:"millimeter",linearDeflectionType:"bounding_box_ratio",linearDeflection:0.0008,angularDeflection:0.35});
  const om=r.meshes[0];
  const g=new THREE.BufferGeometry();
  g.setAttribute("position",new THREE.Float32BufferAttribute(om.attributes.position.array,3));
  g.setAttribute("normal",new THREE.Float32BufferAttribute(om.attributes.normal.array,3));
  g.setIndex(Array.from(om.index.array));
  const mesh={geometry:g,userData:{faces:om.brep_faces,faceCache:new Map()}};
  const c={}; const radii=new Map();
  om.brep_faces.forEach((f,i)=>{const a=analyzeFace(mesh,i); c[a.type]=(c[a.type]||0)+1;
    if(a.type==="cyl"){const k=(a.radius).toFixed(3)+(a.sweep>350?" full":" arc")+(a.inner?" in":" out"); radii.set(k,(radii.get(k)||0)+1);}});
  console.log(om.brep_faces.length,"faces",c); console.log([...radii].sort().slice(0,20)); om.brep_faces.forEach((f,i)=>{const a=analyzeFace(mesh,i); if(a.type==="curved"){const s=a.box.getSize(new THREE.Vector3()); console.log("curved",i,f.last-f.first+1,"tris",s.toArray().map(v=>v.toFixed(2)).join(" "));}});
});
