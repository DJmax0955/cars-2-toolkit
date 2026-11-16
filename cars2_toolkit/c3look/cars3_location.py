"""Cars 3 PS4 location importer - in-extension copy of c3imp/import_cars3_location.py (0.1.3), driven by
File > Import > Cars 3 Location. Changes: scenes decoded in memory (c3json, verified identical to offsetting's JSON on
1095 .oct), textures converted in memory (ditex) and packed into the .blend, generated normal/scalar maps packed, the
report kept as a Blender text - nothing is written to disk. The settings below are set by the operator.

Source-based rest-pose import; it does not bake lighting or edit game files.
Materials/lights approximate the source renderer. Coverage is always reported.
"""
# ========================= USER SETTINGS ================================
GAME_ROOT = r""
LOCATION = "cars3_florida500_b"  # Change to another zone name, or use BUNDLE_PATH.
BUNDLE_PATH = ""               # Optional full path to a zone's _root_.zip.
MAIN_SCENE = ""                # Optional exact OCT member; otherwise selected automatically.
OFFSETTING_EXE = r""
import os
DITEX_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ditex.py")   # cars2_toolkit: its own copy
TBODY_DIRS = []   # cars2_toolkit: loose textures/ folders (an extracted bundle) searched first for <hash>.tbody
NOESIS_EXE = r""
EXISTING_DDS_DIR = r""
MATERIAL_BINDINGS_JSON = r""
ACTIVE_REALM = ""             # Empty: choose a referenced stadium/outdoor/dusk realm automatically.
CACHE_DIR = r""
OUTPUT_BLEND = ""             # Optional absolute .blend path; empty = do not auto-save.
PACK_TEXTURES_ON_SAVE = True
IMPORT_TEXTURES = True
CLEAN_SCENE = True            # Geometry only: disable game lights/probes/shadow proxies/FX/sky/debug.
IMPORT_LIGHT_PROBES = True    # Individual switches apply when CLEAN_SCENE = False.
IMPORT_SHADOW_MESHES = True
IMPORT_EFFECT_MESHES = True
IMPORT_HELPER_MESHES = True   # Occluders, visibility/debug meshes, coarse StadGround.
IMPORT_SKY = True
MATERIAL_TEXTURE_SLOTS = {}   # Optional canonical material name -> (diffuse_slot, normal_slot).
SHADER_SEMANTICS_JSON = r""
IMPORT_EXTRA_MAPS = True      # Named reflectance/specular, gloss, emission and explicit overrides.
IMPORT_TERRAIN_LAYERS = True  # Two diffuse/normal/specular layers using the source blend mask.
DECAL_LIFT = 0.02              # Metres; vertex-alpha decals are lifted (shader displacement only) off the coplanar surface below.
NORMAL_ONLY_DECAL_PROGRAMS = {'ff683733.fp'}  # deferred decals whose gBuffer pass samples diffuse alpha only (sandtracksdecal)
VERTEX_ALPHA_INPUTS = {'vertexColor_alpha','packedVertexColor_diffuse_alpha','packedVertexColor_occlusion_alpha','packedVertexColor_emissive_alpha'}
CONNECT_DIFFUSE_ALPHA = True # Connect authored diffuse alpha, including leaf/fence/decal materials.
MATERIAL_MAP_OVERRIDES = {}   # Name -> role -> {slot, channel, invert}; see README.
UPDATE_EXISTING_MATERIALS = False # True rebuilds materials in the current imported scene.
IMPORT_LIGHTS = True
IMPORT_CURVES = True
IMPORT_COLLISION = True        # Hidden debug collection, never a render/bake caster.
IMPORT_LOCATORS = True
IMPORT_DYNAMIC_DEPENDENCIES = True
IMPORT_ALL_FILESET_VARIANTS = False  # False imports the explicit default/first variant.
IMPORT_OTHER_RACE_MODES = False     # Do not overlay battle/attack/hang/trial art by default.
KEEP_SOURCE_HIDDEN = True
Y_UP_TO_Z_UP = True
FLIP_UV_V = True              # Source/DDS top origin -> Blender bottom origin. U is unchanged.
MAX_RECURSION = 12
MAX_OBJECTS = 60000            # Raises on limit; never silently truncates the map.
LIGHT_ENERGY_SCALE = 1.0       # Source intensity units are not proven Blender watts.
# =======================================================================
import bpy
import numpy as np
import os,sys,json,struct,re,hashlib,subprocess,importlib.util,math,time,traceback,base64
from pathlib import Path, PurePosixPath
from zipfile import ZipFile
from collections import defaultdict,Counter,OrderedDict
from mathutils import Matrix,Vector

VERSION="0.1.3-ext"
from . import c3json
from .c3shader import string_at, program_reflection, shader_semantics, emissive_constant
DATA_DIR=Path(__file__).resolve().parent/'cars3_data'
if not MATERIAL_BINDINGS_JSON:MATERIAL_BINDINGS_JSON=str(DATA_DIR/'source_material_bindings.json')
if not SHADER_SEMANTICS_JSON:SHADER_SEMANTICS_JSON=str(DATA_DIR/'shader_texture_semantics.json')
IDENTITY=Matrix.Identity(4)
AXIS=Matrix(((1,0,0,0),(0,0,-1,0),(0,1,0,0),(0,0,0,1))) if Y_UP_TO_Z_UP else IDENTITY
def normpath(s):return str(s).replace('\\','/').strip('/').lower()
def matrix(values):return Matrix([values[i:i+4] for i in range(0,16,4)]).transposed() if values and len(values)==16 else IDENTITY.copy()
def safe_json(v):return json.dumps(v,ensure_ascii=False,default=str)
def mesh_stamp(me):
 """crc32 of a mesh's positions, corners and map1 UVs: Cars 3 -> Cars 2 export sends a mesh's geometry only when this changed."""
 import zlib
 co=np.empty(len(me.vertices)*3,np.float32);me.vertices.foreach_get('co',co);lv=np.empty(len(me.loops),np.int32);me.loops.foreach_get('vertex_index',lv)
 h=zlib.crc32(co.tobytes());h=zlib.crc32(lv.tobytes(),h)
 if 'map1' in me.uv_layers:
  uv=np.empty(len(me.loops)*2,np.float32);me.uv_layers['map1'].data.foreach_get('uv',uv);h=zlib.crc32(uv.tobytes(),h)
 return '%08x'%h
def blender_uv(source):
 uv=source.copy()
 if FLIP_UV_V:uv[:,1]=1-uv[:,1]
 return uv
def stream_view(data,count,offset,stride,dtype,width):
 """Reject bad counts/strides BEFORE handing arrays to Blender's native APIs."""
 item=np.dtype(dtype).itemsize
 if not all(isinstance(v,int) for v in (count,offset,stride,width)) or count<0 or offset<0 or stride<width*item:
  raise ValueError('Invalid vertex stream descriptor')
 end=offset+(count-1)*stride+width*item if count else offset
 if end>len(data):raise ValueError('Vertex stream exceeds buffer')
 return np.ndarray((count,width),dtype=dtype,buffer=data,offset=offset,strides=(stride,item))
def hidden_run(args,timeout=60):
 kw={}
 if os.name=='nt':
  si=subprocess.STARTUPINFO();si.dwFlags|=subprocess.STARTF_USESHOWWINDOW;si.wShowWindow=subprocess.SW_HIDE;kw['startupinfo']=si
 p=subprocess.run([str(a) for a in args],capture_output=True,timeout=timeout,**kw)
 if p.returncode:raise RuntimeError(p.stderr.decode(errors='replace')[-1500:] or p.stdout.decode(errors='replace')[-1500:])
 return p

class LuaReader:
 """Literal Lua 5.1 configuration evaluation, including SETLIST. No Lua execution."""
 def __init__(self,data):
  if data[:5]!=b'\x1bLuaQ':raise ValueError('Not Lua 5.1')
  self.b=data;self.p=12;self.e='<' if data[6] else '>';self.st=data[8]
 def take(self,n):
  b=self.b[self.p:self.p+n];self.p+=n
  if len(b)!=n:raise ValueError('Truncated Lua chunk')
  return b
 def u(self):return struct.unpack(self.e+'I',self.take(4))[0]
 def string(self):
  n=struct.unpack(self.e+('I' if self.st==4 else 'Q'),self.take(self.st))[0]
  return self.take(n)[:-1].decode('utf-8',errors='replace') if n else None
 def proto(self):
  self.string();self.take(12);code=[self.u() for _ in range(self.u())];k=[]
  for _ in range(self.u()):
   t=self.take(1)[0]
   if t==0:v=None
   elif t==1:v=bool(self.take(1)[0])
   elif t==3:v=struct.unpack(self.e+'d',self.take(8))[0]
   elif t==4:v=self.string()
   else:raise ValueError('Unknown Lua constant type')
   k.append(v)
  children=[self.proto() for _ in range(self.u())];self.take(self.u()*4)
  for _ in range(self.u()):self.string();self.take(8)
  for _ in range(self.u()):self.string()
  return code,k,children
 def tables(self):
  code,k,_=self.proto();r={};g={};unknown=[];pc=0
  def rk(v):return k[v&255] if v&256 else r.get(v)
  while pc<len(code):
   i=code[pc];pc+=1;op=i&63;a=(i>>6)&255;c=(i>>14)&511;b=(i>>23)&511;bx=i>>14
   if op==0:r[a]=r.get(b)
   elif op==1:r[a]=k[bx]
   elif op==2:r[a]=bool(b)
   elif op==3:
    for j in range(a,b+1):r[j]=None
   elif op==5:r[a]=g.get(k[bx])
   elif op==6:r[a]=r.get(b,{}).get(rk(c)) if isinstance(r.get(b),dict) else None
   elif op==7:g[k[bx]]=r.get(a)
   elif op==9:
    if isinstance(r.get(a),dict):r[a][rk(b)]=rk(c)
   elif op==10:r[a]={}
   elif op==34:
    if c==0:c=code[pc];pc+=1
    if not b:unknown.append((pc-1,'variable SETLIST'));continue
    for j in range(1,b+1):r[a][(c-1)*50+j]=r.get(a+j)
   elif op==30:pass
   else:unknown.append((pc-1,op))
  return g,unknown

class Source:
 def __init__(self,root,bundle,cache,report):
  # cars2_toolkit: bundle may be None (a loose extracted .oct + .mtb)
  self.root=Path(root);self.bundle=Path(bundle) if bundle else None;self.cache=cache;self.report=report;self.zips={};self.entries={};self.zip_stems=defaultdict(list);self.decoded={};self.aliases={};self._global_index=None
  if self.bundle is not None:self.openzip(self.bundle)
  # Filename index only. ZIP contents are opened lazily, not all at startup.
  for directory,_,names in os.walk(self.root/'assets'):
   for name in names:
    if name.lower().endswith('.zip'):self.zip_stems[Path(name).stem.lower()].append(Path(directory)/name)
  self.load_aliases()
 def openzip(self,path):
  path=Path(path)
  if path not in self.zips:
   z=ZipFile(path);self.zips[path]=z;self.entries[path]={normpath(n):n for n in z.namelist() if not n.endswith('/')}
  return self.zips[path]
 def ref(self,path,member=None):return (str(Path(path)),member)
 def read(self,ref):return self.openzip(ref[0]).read(ref[1]) if ref[1] is not None else Path(ref[0]).read_bytes()
 def locate(self,key,suffix='.oct',preferred=None):
  key=normpath(key)
  if key.endswith(suffix):key=key[:-len(suffix)]
  base=PurePosixPath(key).name;variants=[key+suffix,key+'/'+base+suffix]
  order=[]
  if preferred:order.append(Path(preferred[0]))
  order.extend(([self.bundle] if self.bundle is not None else [])+[p for p in self.zips if p!=self.bundle])
  for p in dict.fromkeys(order):
   if p not in self.zips and p.exists():self.openzip(p)
   entries=self.entries.get(p,{})
   for name in variants:
    if name in entries:return self.ref(p,entries[name])
   candidates=[n for n in entries if PurePosixPath(n).name==base+suffix]
   if len(candidates)==1:return self.ref(p,entries[candidates[0]])
  for v in variants:
   for path in (self.root/'assets'/v,self.root/v):
    if path.is_file():return self.ref(path)
  archives=[]
  for v in (key+'.zip',key+'/'+base+'.zip'):
   p=self.root/'assets'/v
   if p.exists():archives.append(p)
  archives.extend(self.zip_stems.get(base,[]))
  for p in dict.fromkeys(archives):
   self.openzip(p);entries=self.entries[p]
   for name in variants:
    if name in entries:return self.ref(p,entries[name])
   candidates=[n for n in entries if PurePosixPath(n).name==base+suffix]
   if len(candidates)==1:return self.ref(p,entries[candidates[0]])
  return None
 def sibling(self,ref,suffix):
  if ref[1] is None:
   p=Path(ref[0]);q=p.with_name(p.stem+suffix);return self.ref(q) if q.exists() else None
  name=str(PurePosixPath(ref[1]).with_suffix(''))+suffix;entries=self.entries[Path(ref[0])];actual=entries.get(normpath(name))
  return self.ref(ref[0],actual) if actual else None
 def decode(self,ref):
  if ref in self.decoded:return self.decoded[ref]
  scene=c3json.decode(self.read(ref));self.decoded[ref]=scene;return scene
 def load_aliases(self):
  p=self.root/'assets/expendables_data/expendables_data.zip'
  if not p.exists():return
  try:
   z=self.openzip(p);data,unknown=LuaReader(z.read('cars3.lua')).tables();items=data.get('ExpendablesPackage',{}).get('Expendables',{})
   for row in items.values():
    if isinstance(row,dict) and row.get('Name') and row.get('Model'):self.aliases[normpath(row['Name'])]=row
   self.report['actor_aliases_loaded']=len(self.aliases)
   if unknown:self.report['warnings'].append({'kind':'actor_table_partial_lua','unknown_opcodes':unknown})
  except Exception as e:self.report['warnings'].append({'kind':'actor_alias_table','message':str(e)})
 def resolve_actor(self,path,preferred=None):
  row=self.aliases.get(normpath(path));actual=row.get('Model') if row else path
  return self.locate(actual,preferred=preferred),row
 def texture(self,h):
  if EXISTING_DDS_DIR:
   p=Path(EXISTING_DDS_DIR)/(h+'.dds')
   if p.is_file():return p.read_bytes()
  ref=None
  for d in TBODY_DIRS:   # cars2_toolkit: an extracted bundle's textures/ folder
   p=Path(d)/(h+'.tbody')
   if p.is_file():ref=self.ref(p);break
  if not ref:ref=self.locate('textures/'+h,'.tbody')
  if not ref:
   # Shared texture bundles can be indexed only when a referenced ID is missing.
   if self._global_index is None:
    self._global_index={}
    for p in sorted((self.root/'bundles').glob('*/_root_.zip')):
     with ZipFile(p) as z:
      for n in z.namelist():
       if n.endswith('.tbody'):self._global_index.setdefault(PurePosixPath(n).stem.lower(),(str(p),n))
   ref=self._global_index.get(h)
  if not ref:return None
  # Loading a shared texture must not add a foreign zone to model lookup order.
  if ref[1] is None:data=Path(ref[0]).read_bytes()   # cars2_toolkit: loose file
  elif Path(ref[0]) in self.zips:data=self.read(ref)
  else:
   with ZipFile(ref[0]) as texturezip:data=texturezip.read(ref[1])
  try:
   fmt=(struct.unpack_from('<I',data,20)[0]>>20)&63
   # ditex also decodes uncompressed (10) and sRGB BC1/BC3 (38/39) - the sand tracks decal's colour is fmt 10
   if fmt in (10,35,37,38,39) and Path(DITEX_SCRIPT).exists():
    if not hasattr(self,'ditex'):
     spec=importlib.util.spec_from_file_location('cars3_importer_ditex',DITEX_SCRIPT);self.ditex=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.ditex)
    output,_=self.ditex.c3_tbody_to_dds(data);expected={35:b'DXT1',38:b'DXT1',37:b'DXT5',39:b'DXT5',10:b'\0\0\0\0'}[fmt]
    if output[84:88]!=expected:raise ValueError('GNF descriptor disagrees with DDS format')
   elif NOESIS_EXE and Path(NOESIS_EXE).exists():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
     gnf=Path(td)/(h+'.gnf');dds=Path(td)/(h+'.dds');gnf.write_bytes(data);hidden_run([NOESIS_EXE,'?cmode',gnf,dds],timeout=45)
     output=dds.read_bytes() if dds.exists() else b''
   elif not Path(DITEX_SCRIPT).exists():raise RuntimeError('ditex.py not found - set its path in the add-on preferences')
   else:raise RuntimeError('This GNF format needs Noesis (not configured)')
   if output[:4]!=b'DDS ':raise ValueError('No valid DDS output')
   return output
  except Exception as e:self.report['warnings'].append({'kind':'texture_conversion','texture':h,'message':str(e)});return None
 def close(self):
  for z in self.zips.values():z.close()

def mtb_sections(data):
 if data[:4]!=b'BNDL':raise ValueError('Unsupported MTB header')
 p=16;out={}
 for _ in range(struct.unpack_from('<I',data,12)[0]):
  tag,v,l,c=struct.unpack_from('<4sIII',data,p)
  if l<16 or p+l>len(data):raise ValueError('Invalid MTB section bounds')
  out[tag]=(p,v,l,c);p+=l
 return out
def texture_packet(record,count,total,preferred=None):
 candidates=[]
 for bindings in range(1,count+1):
  for off in range(0,len(record)-bindings*8-count+1,4):
   ids=[struct.unpack_from('<I',record,off+i*8)[0] for i in range(bindings)];flags=[struct.unpack_from('<I',record,off+i*8+4)[0] for i in range(bindings)];sam=list(record[off+bindings*8:off+bindings*8+count])
   if all(i<total or i==0xffffffff for i in ids) and all(f in (0,0xffffffff) for f in flags) and all(v in (2,3,4,6,8,10,12,14,16,18,20,28,32,34,35,64,66) for v in sam):candidates.append((off,ids,sam))
 if not candidates:return None
 if preferred is not None:
  exact=[c for c in candidates if c[0]==preferred]
  if len(exact)==1:return exact[0]
 ends={o+len(ids)*8 for o,ids,s in candidates}
 return min(candidates,key=lambda x:x[0]) if len(ends)==1 else None

# Shaders whose binding block texture_packet finds ambiguous; offsets read by hand from the MTB records (same table as
# tools/c3collect.py): realm sky gradient / high clouds 1 binding at 0x20, Florida ocean 5 bindings at 0x90.
MANUAL_PACKET_OFFSETS={'020112a1c5501a2972f852ac35179f6b':0x20,'27db04fdcb423d0b0da76321a1113d18':0x90}
def bindings_at(record,off,count,total):
 """(offset after, texture ids, flags) of the bindings at a known offset, until an entry is not a binding."""
 ids=[];flags=[]
 while len(ids)<count and off+8<=len(record):
  i,f=struct.unpack_from('<II',record,off)
  if not ((i<total or i==0xffffffff) and f in (0,0xffffffff)):break
  ids.append(i);flags.append(f);off+=8
 return (off,ids,flags) if ids else None

def packet_at(data,o,count,total):
 """(texture ids, sampler bytes) of a full texture packet (count bindings) at data[o], or None."""
 if o<0 or o+count*9>len(data):return None
 ids=[struct.unpack_from('<I',data,o+i*8)[0] for i in range(count)];flags=[struct.unpack_from('<I',data,o+i*8+4)[0] for i in range(count)];sam=list(data[o+count*8:o+count*9])
 if all(i<total or i==0xffffffff for i in ids) and all(f in (0,0xffffffff) for f in flags) and all(v in (2,3,4,6,8,10,12,14,16,18,20,28,32,34,35,64,66) for v in sam):return ids,sam
 return None
def realign_terrain(data,start,size,extra,count,total,packet,consts,span=256):
 """Terrain records are [g_diffuse0_color ...][texture packet][constant buffer], so the packet belongs at
 size - buffer - packet length. When an earlier record's size is off, the slice drifts (heartland: -80, the slice
 then 'finds' a 2-entry garbage packet). None when aligned, else (true start, packet) from the full packet nearest
 the expected spot, if a colour sits where that start puts g_diffuse0_color."""
 if not consts or not count:return None
 cbs=(max(c['offset']+c['size'] for c in consts)+15)&~15;exp=size-cbs-((count*9+15)&~15)
 if exp<0 or (packet and packet[0]==exp and len(packet[1])==count):return None
 best=None
 for o in range(start+exp-span,start+exp+span+1,16):
  got=packet_at(data,o,count,total)
  if got and (best is None or abs(o-start-exp)<abs(best[0]-start-exp)):best=(o,got)
 if not best:return None
 true=best[0]-exp
 if true<0 or true+size>len(data):return None
 if not all(math.isfinite(x) and 0<=x<=64 for x in struct.unpack_from('<3f',data,true+extra)):return None
 return true,(exp,best[1][0],best[1][1])
def terrain_constants(data,start,size,extra,packet,consts):
 """Cars 3 multi-layer terrain constants. A record is [g_diffuse0_color ...][texture packet][constant buffer]; the
 buffer's offset 0 holds the vertex shader's g_layers_texcoordScale (layer 0 / layer 1 UV scale), the pixel shader
 constants follow at their reflected offsets. Located from the texture packet in the whole .mtb (data), because a
 record slice (start, size) can drift when an earlier record's size is off; implausible values -> identity."""
 out={'layers':(1.0,1.0),'contrast':0.0,'blend_scale':1.0,'color1':(1.0,1.0,1.0)}
 if not consts or not packet:return out
 off,ids,sam=packet;cbs=(max(c['offset']+c['size'] for c in consts)+15)&~15
 true=start+off-(size-cbs-((len(ids)*8+len(sam)+15)&~15));base=true+size-cbs  # where this record really starts
 if true<0 or base+cbs>len(data):return out
 c0=struct.unpack_from('<3f',data,true+extra)
 if not all(math.isfinite(x) and 0<=x<=64 for x in c0):return out  # packet not where the layout says
 out['color0']=c0
 def get(o,n,lo,hi):
  v=struct.unpack_from('<%df'%n,data,base+o)
  return v if all(math.isfinite(x) and lo<=x<=hi for x in v) else None
 c={x['name']:x['offset'] for x in consts}
 v=get(0,2,1e-3,64)
 if v:out['layers']=v
 v='g_multi_blendMap_parameters' in c and get(c['g_multi_blendMap_parameters'],1,-8,8)
 if v:out['contrast']=v[0]
 v='g_multi_blendMap_texcoordScale' in c and get(c['g_multi_blendMap_texcoordScale'],1,1e-3,64)
 if v:out['blend_scale']=v[0]
 v='g_diffuse1_color' in c and get(c['g_diffuse1_color'],3,0,64)
 if v:out['color1']=v
 return out


class Importer:
 def __init__(self,provider,location,cache,report,create_scene=True):
  self.src=provider;self.location=location;self.cache=cache;self.r=report;self.mesh_cache={};self.material_cache={};self.images={};self.normal_ag_cache={};self.standard_normals={};self.asset_counter=0;self.object_count=0;self.completed={};self._known={};self._sizes={};self._overrides={};self.record_cache={};self.buffer_cache=OrderedDict();self.buffer_bytes=0;self.last_checkpoint=0
  self.manifest={'mesh':{},'coll':{}};self.options={k:bool(v and not CLEAN_SCENE) for k,v in {'lights':IMPORT_LIGHTS,'light_probes':IMPORT_LIGHT_PROBES,'shadow_meshes':IMPORT_SHADOW_MESHES,'effect_meshes':IMPORT_EFFECT_MESHES,'helper_meshes':IMPORT_HELPER_MESHES,'sky':IMPORT_SKY,'curves':IMPORT_CURVES,'collision':IMPORT_COLLISION,'locators':IMPORT_LOCATORS}.items()}
  self.shader_semantics={};self.scalar_images={}
  if SHADER_SEMANTICS_JSON and Path(SHADER_SEMANTICS_JSON).is_file():self.shader_semantics=json.loads(Path(SHADER_SEMANTICS_JSON).read_text(encoding='utf8'))
  if create_scene:
   self.collection=bpy.data.collections.new('Cars3_'+location);bpy.context.scene.collection.children.link(self.collection);self.collection['cars3_importer_version']=VERSION
   self.root=bpy.data.objects.new('Cars3_Source_Yup',None);self.collection.objects.link(self.root);self.root.matrix_world=AXIS;self.root.empty_display_size=4
   self.debug=self.subcollection('Debug_Collision_Volumes_Unsupported');self.debug.hide_render=True
   self.paths=self.subcollection('RacePaths_Locators');self.lights=self.subcollection('Lights_Realms')
  else:self.collection=bpy.context.scene.collection;self.root=None;self.debug=self.paths=self.lights=None
  self.report_text=bpy.data.texts.new('Cars3_Import_Report_'+location)
  self.shaderzip=None;self.primary_realm=None;p=Path(GAME_ROOT)/'assets/shaders/shaders.zip'
  if p.exists():self.shaderzip=ZipFile(p)
  if Path(MATERIAL_BINDINGS_JSON).is_file():self._known=json.loads(Path(MATERIAL_BINDINGS_JSON).read_text(encoding='utf-8'))
  # Optional learned material lengths are local support files, not Florida-only bindings.
  folder=DATA_DIR
  for name,attr in (('shader_material_record_sizes.json','_sizes'),('material_shader_size_overrides.json','_overrides')):
   p=folder/name
   if p.exists():setattr(self,attr,json.loads(p.read_text()))
 def subcollection(self,name,parent=None):
  c=bpy.data.collections.new(name);(parent or self.collection).children.link(c);return c
 def object(self,name,data,col,world,meta=None):
  if self.object_count>=MAX_OBJECTS:raise RuntimeError('MAX_OBJECTS reached; increase it. Import is incomplete.')
  obj=bpy.data.objects.new(name,data);col.objects.link(obj);obj.parent=self.root;obj.matrix_local=world
  if meta:
   for k,v in meta.items():obj['c3_'+k]=v if isinstance(v,(str,int,float,bool)) else safe_json(v)
  self.object_count+=1;return obj
 def warning(self,kind,**kwargs):self.r['warnings'].append({'kind':kind,**kwargs})
 def source_role(self,n,p=None,scene=None,ref=None):
  material=(scene or {}).get('MaterialPool',{}).get('Material#'+str((p or {}).get('MaterialReference')),{});text=' '.join(str(x).lower() for x in (n.get('Type'),n.get('NodeName'),n.get('MeshName'),(p or {}).get('MaterialName'),material.get('Name'),material.get('FileName')))
  if any(t in text for t in ('lightprobe','light_probe','reflectionprobe','reflection_probe','irradiance','probevolume','probe_volume')) or n.get('Type','').lower()=='probe':return 'light_probes'
  if re.search(r'(?:^|[_ /])shadow(?:caster|only|proxy|mesh|volume|plane|$|[_ /])',text):return 'shadow_meshes'
  if n.get('NodeName')=='StadGround' or any(t in text for t in ('occluder','occlusionmesh','visrgn','visregion','collision_mesh')):return 'helper_meshes'
  if any(t in text for t in ('lightray','light_ray','lightbeam','light_beam','blueglow','lensflare','materials__fx__','tracklights_fx')):return 'effect_meshes'
  if p is not None and (any(t in text for t in ('skydome','sky_dome','sky_night','sky_day')) or ref and ('/realms/' in '/'+normpath(ref[0]) or '/realms/' in '/'+normpath(ref[1] or '') or PurePosixPath(ref[1] or ref[0]).stem.lower().startswith('rlm_'))):return 'sky'
  return None
 def filtered(self,role,ref,node=None,primitive=None):
  if role and not self.options[role]:
   self.r['filtered_items'].append({'category':role,'asset':ref,'node':node,'primitive':primitive});return True
  return False
 def checkpoint(self,stage,force=False,**details):
  self.r['last_stage']={'stage':stage,**details};now=time.monotonic()
  if not force and now-self.last_checkpoint<1:return
  self.last_checkpoint=now
  row={'version':VERSION,'location':self.location,'status':'importing','objects':self.object_count,'mesh_templates':self.r['mesh_templates'],'mesh_instances':self.r['mesh_instances'],'elapsed_seconds':time.time()-self.r['started'],**self.r['last_stage']}
  print('CARS3_STAGE',stage,details,flush=True)
 def world(self,scene,ref):
  m=IDENTITY.copy();visited=set();nodes=scene.get('SceneTreeNodePool',{})
  while ref is not None:
   if ref in visited:raise ValueError('Scene parent cycle')
   visited.add(ref);n=nodes.get('Node#'+str(ref),{});m=matrix(n.get('LocalToParentMatrix'))@m;parents=n.get('ParentNodeReferences',[]);ref=parents[0] if parents else None
  return m
 def material_records(self,scene,ref):
  if ref in self.record_cache:return self.record_cache[ref]
  file=self.src.sibling(ref,'.mtb')
  if not file or not self.shaderzip:return {}
  try:
   data=self.src.read(file);sec=mtb_sections(data)
   if b'MATP' not in sec:return {}
   tp=sec.get(b'TEXB');hashes=[data[tp[0]+24+i*16:tp[0]+32+i*16].hex() for i in range(tp[3])] if tp else [];mp=sec[b'MATP'];p=mp[0]+16
   def strings(align):
    nonlocal p
    count=struct.unpack_from('<I',data,p)[0];p+=4;out=[]
    for _ in range(count):q=data.index(0,p);out.append(data[p:q].decode());p=q+1
    p=(p+align-1)&~(align-1);return out
   shaders=strings(4);uuids=strings(2);ids=struct.unpack_from('<'+'H'*mp[3],data,p);p+=2*mp[3];p=(p+3)&~3;p+=4*mp[3];p=(p+15)&~15;out={};cursor=p
   for i,shaderid in enumerate(ids):
    key=shaders[shaderid];shd=self.shaderzip.read(key+'.shd');counts=list(shd[24:28]);extra=432 if struct.unpack_from('<I',shd,76)[0]&0x1f00000==0x1f00000 else 0
    size=counts[0]*16+(((counts[3]+1)*8+15)&~15)+extra
    if key in self._sizes:size=self._sizes[key]['sizes'][0]
    size+=self._overrides.get(key,0);record=data[cursor:cursor+size];cursor+=size
    # This one-sampler family has a scalar whose low byte masquerades as a
    # sampler at offset 24. The actual binding follows two float4 registers.
    packet=texture_packet(record,counts[3],len(hashes),32 if key in ('bc289c1827ff364812473194108b9c8d','09275dee01c51866109e52c786443ad6') else None) if counts[3] else (0,[],[])
    if not packet and key in MANUAL_PACKET_OFFSETS:packet=bindings_at(record,MANUAL_PACKET_OFFSETS[key],counts[3],len(hashes))
    sem=self.semantics(key)
    if 'g_multi_blendMap_texture' in sem.get('textures',{}):
     fix=realign_terrain(data,cursor-size,size,extra,counts[3],len(hashes),packet,sem.get('constants',[]))
     if fix:start,packet=fix;record=data[start:start+size];cursor=start+size;self.warning('material_record_realigned',asset=str(ref),material=i)  # drifted slice: re-slice, re-sync the cursor
    if not packet:
     canonical=scene.get('MaterialPool',{}).get('Material#'+str(i),{}).get('FileName','').removesuffix('.mtl').lower()
     if canonical in self._known:
      known=self._known[canonical];out[i]={'shader':known['shader'],'textures':known['textures'],'flags':known.get('sampler_flags',[]),'color':known.get('color',[1]*3),'raw_parameters_hex':record.hex(),'verified_override':True};continue
     self.warning('unresolved_material_packet',asset=str(ref),material=i,shader=key);continue
    _,indexes,flags=packet;color=list(struct.unpack_from('<3f',record,extra)) if len(record)>=extra+12 else [1]*3
    if not all(math.isfinite(v) and 0<=v<=64 for v in color):color=[1]*3
    out[i]={'shader':key,'textures':[hashes[j] if j!=0xffffffff else None for j in indexes],'flags':flags,'color':color,'raw_parameters_hex':record.hex()}
    em=emissive_constant(record,extra,self.instance_semantics(key,sem))
    if em:out[i]['emissive']=em
    if 'g_multi_blendMap_texture' in sem.get('textures',{}):
     out[i]['terrain']=tc=terrain_constants(data,cursor-size,size,extra,packet,sem.get('constants',[]))
     if 'color0' in tc:out[i]['color']=list(tc['color0'])  # g_diffuse0_color of a drifted record slice
   if not 0<=mp[0]+mp[2]-cursor<16:self.warning('material_record_size_mismatch',asset=str(ref),remaining=mp[0]+mp[2]-cursor)
   self.record_cache[ref]=out;return out
  except Exception as e:self.warning('material_bundle',asset=str(ref),message=str(e));return {}
 def image(self,h,noncolor=False):
  if not h:return None
  key=(h,noncolor)
  if key in self.images:return self.images[key]
  data=self.src.texture(h)
  if not data:self.warning('missing_texture',texture=h);self.images[key]=None;return None
  try:
   self.checkpoint('texture_load',texture=h)
   if len(data)<128 or data[:4]!=b'DDS ':raise ValueError('Invalid DDS header')
   height,width=struct.unpack_from('<II',data,12)
   if not 0<width<=32768 or not 0<height<=32768:raise ValueError('Invalid DDS dimensions')
   image=bpy.data.images.new('C3_'+h+('_data' if noncolor else ''),8,8);image.pack(data=data,data_len=len(data));image.source='FILE';image.filepath_raw='C3_'+h+'.dds'
   if image.size[0]==0:bpy.data.images.remove(image);raise ValueError('Blender could not read the DDS')
   image.colorspace_settings.name='Non-Color' if noncolor else 'sRGB';self.images[key]=image;return image
  except Exception as e:self.warning('blender_texture_load',texture=h,message=str(e));self.images[key]=None;return None
 def normal_image(self,h):
  """Convert packed AG normals once on disk, keeping material nodes simple."""
  if not h:return None
  if h in self.standard_normals:return self.standard_normals[h]
  image=self.image(h,True)
  if not image:self.standard_normals[h]=None;return None
  pixels=np.empty(len(image.pixels),np.float32);image.pixels.foreach_get(pixels);raw=pixels.reshape(-1,4);sample=raw[::max(1,len(raw)//2048)]
  ag=len(sample)>0 and float(np.min(sample[:,0]))>.97 and .15<float(np.mean(sample[:,3]))<.85
  rgb=len(sample)>0 and float(np.mean(sample[:,2]))>.7 and all(.15<float(np.mean(sample[:,i]))<.85 for i in (0,1))
  if not ag:
   if rgb:self.standard_normals[h]=image;image['c3_normal_encoding']='RGB';return image
   self.warning('normal_slot_not_normal',texture=h,note='Texture is not recognized as an AG/RGB normal; left disconnected')
   self.standard_normals[h]=None;return None
  w,height=image.size;out=''
  x=raw[:,3]*2-1;y=raw[:,1]*2-1;z=np.sqrt(np.maximum(1-x*x-y*y,0));converted=np.empty_like(raw);converted[:,0]=raw[:,3];converted[:,1]=raw[:,1];converted[:,2]=z*.5+.5;converted[:,3]=1
  result=bpy.data.images.new('C3_'+h+'_normal_RGB',width=w,height=height,alpha=False);result.colorspace_settings.name='Non-Color';result.pixels.foreach_set(converted.ravel());result.pack()
  result['c3_source_texture']=h;result['c3_normal_encoding']='AG converted to RGB, original resolution';self.images[(h,'normal_rgb')]=result;self.standard_normals[h]=result
  if image.users==0:self.images.pop((h,True),None);bpy.data.images.remove(image)
  self.r['normal_conversions'].append({'source':h,'path':str(out),'width':w,'height':height,'encoding':'AG to RGB'})
  return result
 def semantics(self,key):
  if key not in self.shader_semantics:
   try:self.shader_semantics[key]=shader_semantics(self.shaderzip,key) if self.shaderzip else {}
   except Exception as e:self.shader_semantics[key]={};self.warning('shader_reflection',shader=key,message=str(e))
  return self.shader_semantics[key]
 def instance_semantics(self,key,sem):
  """sem with 'instance_constants' (the cached shader_texture_semantics.json predates them: read from the shader)."""
  if 'emissive_partial' in sem or not self.shaderzip:return sem
  try:
   if 'instance_constants' not in sem:sem['instance_constants']=shader_semantics(self.shaderzip,key).get('instance_constants',[])
   code=self.shaderzip.read(sem['program']) if sem.get('program') else b''
   sem['emissive_partial']=b'g_emissive_angleBasedParameters' in code or b'g_emissiveEdge' in code
  except Exception:sem.setdefault('instance_constants',[]);sem['emissive_partial']=False
  return sem
 def channel_image(self,h,channel='R',invert=False):
  key=(h,channel,bool(invert))
  if key in self.scalar_images:return self.scalar_images[key]
  image=self.image(h,True)
  if not image:return None
  if True:
   pixels=np.empty(len(image.pixels),np.float32);image.pixels.foreach_get(pixels);raw=pixels.reshape(-1,4)
   value=raw[:,:3]@np.array((.2126,.7152,.0722),np.float32) if channel in ('RGB','LUMINANCE') else raw[:,{'R':0,'G':1,'B':2,'A':3}[channel]]
   if invert:value=1-value
   rgba=np.ones_like(raw);rgba[:,:3]=np.clip(value,0,1)[:,None];result=bpy.data.images.new('scalar',width=image.size[0],height=image.size[1],alpha=False);result.colorspace_settings.name='Non-Color';result.pixels.foreach_set(rgba.ravel());result.pack()
  result.name='C3_'+h+'_'+channel+('_inverse' if invert else '');result.colorspace_settings.name='Non-Color';result['c3_source_texture']=h;result['c3_source_channel']=channel;self.scalar_images[key]=result;self.images[(h,channel,invert)]=result;return result
 def texture_roles(self,name,record):
  sem=self.semantics(record['shader']);slots=sem.get('textures',{})
  di=next((slots[n] for n in ('g_diffuse0_texture','g_diffuse_texture','g_color_texture','g_main_texture') if n in slots),None);ni=slots.get('g_normalMap0_texture')
  if not sem:
   di=1 if record['shader']=='909dfe23b96f2314dd9bd9fc6125a1d7' else 0;ni=di+1
  if name in MATERIAL_TEXTURE_SLOTS:di,ni=MATERIAL_TEXTURE_SLOTS[name]
  textures=record['textures']
  return (textures[di] if isinstance(di,int) and 0<=di<len(textures) else None,textures[ni] if isinstance(ni,int) and 0<=ni<len(textures) else None,di,ni)
 def program_inputs(self,program):
  """Vertex inputs a pixel program declares (reflection names i_<name>), e.g. vertexColor_alpha."""
  if not hasattr(self,'_program_inputs'):self._program_inputs={}
  if program not in self._program_inputs:
   try:data=self.shaderzip.read(program) if program and self.shaderzip else b''
   except KeyError:data=b''
   self._program_inputs[program]={m.decode() for m in re.findall(rb'VARIABLE\.i_([A-Za-z0-9_]+)',data)}
  return self._program_inputs[program]
 def terrain_weight(self,nodes,links,blendmap,layer1,tc):
  """Layer-1 weight socket: w = b - 2|c-.5|(b - [c>=.5]) (c = vertex blue, b = blend map blue),
  w = clamp((w-.5)/(1-parameters.x)+.5), w = min(w, layer-1 alpha)."""
  x=[-640]
  def node(kind,**kw):
   n=nodes.new(kind);n.location=(x[0],-900);x[0]+=150
   for k,v in kw.items():setattr(n,k,v)
   return n
  def op(kind,a,b=None,c=None,clamp=False,label=''):
   n=node('ShaderNodeMath',operation=kind,use_clamp=clamp,label=label)
   for i,v in enumerate((a,b,c)):
    if v is None:continue
    if isinstance(v,(int,float)):n.inputs[i].default_value=v
    else:links.new(v,n.inputs[i])
   return n.outputs[0]
  vc=node('ShaderNodeVertexColor',layer_name='SourceAuxColor0',label='Cars 3 blend weight (blue)');sv=node('ShaderNodeSeparateColor');links.new(vc.outputs['Color'],sv.inputs['Color']);c=sv.outputs['Blue']
  if blendmap:
   sb=node('ShaderNodeSeparateColor');links.new(blendmap.outputs['Color'],sb.inputs['Color']);b=sb.outputs['Blue']
  else:b=.5
  step=op('GREATER_THAN',c,.5-1e-6,label='c >= 0.5')
  ramp=op('MULTIPLY_ADD',op('MULTIPLY',op('ABSOLUTE',op('SUBTRACT',c,.5)),op('SUBTRACT',b,step)),-2,b,label='noise ramp')
  k=1/(1-tc['contrast']) if abs(1-tc['contrast'])>1e-4 else 1e4
  w=op('MULTIPLY_ADD',op('SUBTRACT',ramp,.5),k,.5,clamp=True,label='contrast (parameters.x)')
  if layer1:w=op('MINIMUM',w,layer1.outputs['Alpha'],label='min layer 1 alpha')
  return w
 def material(self,scene,mid,record,ref):
  key=(ref,mid)
  if key in self.material_cache:return self.material_cache[key]
  source=scene.get('MaterialPool',{}).get('Material#'+str(mid),{});name=source.get('FileName',source.get('Name',str(mid))).removesuffix('.mtl');lower=name.lower();mat=bpy.data.materials.new('C3_'+PurePosixPath(name).name);mat.use_nodes=True;mat['c3_source_material']=safe_json(source);mat['c3_source_record']=safe_json(record or {});mat['c3_material_status']='reflected material maps; source rendering approximated'
  nodes=mat.node_tree.nodes;nodes.clear();links=mat.node_tree.links;output=nodes.new('ShaderNodeOutputMaterial');output.location=(900,100);bsdf=nodes.new('ShaderNodeBsdfPrincipled');bsdf.location=(600,100);bsdf.inputs['Roughness'].default_value=.65;bsdf.inputs['Base Color'].default_value=(.5,.5,.5,1);mat.diffuse_color=(.5,.5,.5,1);links.new(bsdf.outputs['BSDF'],output.inputs['Surface'])
  if not record:
   mat['c3_material_status']='unresolved source texture bindings';self.warning('material_without_bindings',asset=str(ref),material=mid,name=name);self.material_cache[key]=mat;return mat
  diffuse,normal,di,ni=self.texture_roles(lower,record);sem=self.semantics(record['shader']);slots=sem.get('textures',{});textures=record['textures'];used=set();maps={};texnodes={}
  mat['c3_diffuse_slot']=di if di is not None else -1;mat['c3_normal_slot']=ni if ni is not None else -1;mat['c3_diffuse_texture']=diffuse or '';mat['c3_normal_texture']=normal or '';mat['c3_shader_reflection']=safe_json(sem)
  uvnode=nodes.new('ShaderNodeUVMap');uvnode.uv_map='map1';uvnode.location=(-900,100)
  def hashed(slot):return textures[slot] if isinstance(slot,int) and 0<=slot<len(textures) else None
  slot_uv={}  # terrain layers sample their own scaled UVs
  def tex(slot,label,data=False,isnormal=False,image=None):
   h=hashed(slot)
   if not IMPORT_TEXTURES or not h:return None
   if image is None:image=self.normal_image(h) if isnormal else self.image(h,data)
   if not image:return None
   tk=(slot,data,isnormal,image.name)
   if tk in texnodes:return texnodes[tk]
   node=nodes.new('ShaderNodeTexImage');node.image=image;node.label=label+' [slot '+str(slot)+']';node.location=(-660,-len(texnodes)*240+300);links.new(slot_uv.get(slot,uvnode.outputs['UV']),node.inputs['Vector']);texnodes[tk]=node;used.add(slot);maps[label]={'slot':slot,'texture':h,'colorspace':image.colorspace_settings.name};return node
  def blend(a,b,mask,label):
   if a is None:return b
   if b is None or mask is None:return a
   mix=nodes.new('ShaderNodeMixRGB');mix.blend_type='MIX';mix.label=label;mix.location=(-220,150-len(maps)*40);links.new(mask,mix.inputs[0]);links.new(a,mix.inputs[1]);links.new(b,mix.inputs[2]);return mix.outputs[0]
  mask=None;secondary=next((i for i in (1,2) if 'g_diffuse'+str(i)+'_texture' in slots),None);terrain=IMPORT_TERRAIN_LAYERS and 'g_multi_blendMap_texture' in slots
  if terrain:
   # Cars 3 multi-layer terrain, as its pixel shaders compute it (PS4 1ce0785b/ff40df40, Disney Infinity PC 21b059b5):
   # layer UVs = map1 * g_layers_texcoordScale.xy, blend map at layer-0 UV * texcoordScale; opaque (alpha out = 1).
   tc=record.get('terrain') or terrain_constants(b'',0,0,0,None,[]);mat['c3_terrain_constants']=safe_json(tc)
   def scaled(s,label):
    if abs(s-1)<1e-6:return uvnode.outputs['UV']
    vm=nodes.new('ShaderNodeVectorMath');vm.operation='MULTIPLY_ADD';vm.label=label;vm.location=(-860,-200-len(slot_uv)*60)
    vm.inputs[1].default_value=(s,s,0);vm.inputs[2].default_value=(0,1-s if FLIP_UV_V else 0,0)  # scale about the source UV origin (v flipped)
    links.new(uvnode.outputs['UV'],vm.inputs[0]);return vm.outputs[0]
   lx,ly=tc['layers'];uv0=scaled(lx,'Layer 0 UV');uv1=scaled(ly,'Layer 1 UV');uvb=scaled(lx*tc['blend_scale'],'Blend map UV')
   for n,uv in (('g_multi_blendMap_texture',uvb),('g_diffuse0_texture',uv0),('g_normalMap0_texture',uv0),('g_specular0_reflectanceTexture',uv0),('g_specular0_glossTexture',uv0),('g_diffuse1_texture',uv1),('g_normalMap1_texture',uv1),('g_specular1_reflectanceTexture',uv1),('g_specular1_glossTexture',uv1)):
    if n in slots:slot_uv[slots[n]]=uv
  primary=tex(di,'Base Color');color=primary.outputs['Color'] if primary else None;n0=tex(ni,'Normal',True,True);normal_color=n0.outputs['Color'] if n0 else None
  if terrain:
   def tint(socket,rgb,label):
    if socket is None or all(abs(v-1)<1e-6 for v in rgb):return socket
    vm=nodes.new('ShaderNodeVectorMath');vm.operation='MULTIPLY';vm.label=label;vm.location=(-420,260-len(maps)*40);vm.inputs[1].default_value=tuple(rgb);links.new(socket,vm.inputs[0]);return vm.outputs[0]
   color=tint(color,record.get('color',[1]*3),'g_diffuse0_color')
   layer=tex(slots.get('g_diffuse1_texture'),'Base Color layer 1');other_n=tex(slots.get('g_normalMap1_texture'),'Normal layer 1',True,True)
   color1=tint(layer.outputs['Color'] if layer else None,tc['color1'],'g_diffuse1_color')
   if 'g_diffuse0_texture' in slots and 'g_diffuse1_texture' in slots:
    mask=self.terrain_weight(nodes,links,tex(slots['g_multi_blendMap_texture'],'Terrain blend map',True),layer,tc)
    color=blend(color,color1,mask,'Terrain colors');normal_color=blend(normal_color,other_n.outputs['Color'] if other_n else None,mask,'Terrain normals')
    mat['c3_terrain_blend']='Cars 3 shader: vertex blue + blend map blue, contrast, capped by layer 1 alpha'
   elif color is None:color=color1;normal_color=other_n.outputs['Color'] if other_n else None  # layersEnabled_1: layer 1 only
  if color:links.new(color,bsdf.inputs['Base Color'])
  if normal_color:
   nm=nodes.new('ShaderNodeNormalMap');nm.uv_map='map1';nm.location=(200,-140);links.new(normal_color,nm.inputs['Color']);links.new(nm.outputs['Normal'],bsdf.inputs['Normal'])
  valpha=next((n for n in self.program_inputs(sem.get('program')) if n in VERTEX_ALPHA_INPUTS),None)
  if primary and CONNECT_DIFFUSE_ALPHA and not terrain:
   alpha=primary.outputs['Alpha']
   if valpha:
    # decals/overlays: alpha = texture alpha x vertex alpha (aux byte 3), e.g. sandtracksdecal ba2efa1a.fp
    vc=nodes.new('ShaderNodeVertexColor');vc.layer_name='SourceAuxColor0';vc.label='Cars 3 '+valpha;vc.location=(-420,-420)
    mul=nodes.new('ShaderNodeMath');mul.operation='MULTIPLY';mul.location=(-220,-420);links.new(alpha,mul.inputs[0]);links.new(vc.outputs['Alpha'],mul.inputs[1]);alpha=mul.outputs[0]
   links.new(alpha,bsdf.inputs['Alpha']);maps['Opacity']={'slot':di,'texture':diffuse,'channel':'A'+(' x vertex alpha' if valpha else '')}
   if hasattr(mat,'blend_method'):mat.blend_method='BLEND' if valpha else 'HASHED'
   if hasattr(mat,'surface_render_method'):mat.surface_render_method='BLENDED' if valpha else 'DITHERED'
   mat['c3_alpha_source']='authored diffuse alpha'+(' x vertex alpha ('+valpha+')' if valpha else '; includes leaf/fence/decal names')
   if valpha and DECAL_LIFT and not re.search(r'(?:^|/|__)fx(?:/|__)',lower):
    # Cars 3 draws these as deferred decals ON the surface below (often exactly coplanar): lift them in the shader
    # only (material displacement), so the mesh data - and the Cars 3 -> Cars 2 export - stay untouched
    disp=nodes.new('ShaderNodeDisplacement');disp.space='WORLD';disp.location=(600,-300);disp.inputs['Height'].default_value=1;disp.inputs['Midlevel'].default_value=0;disp.inputs['Scale'].default_value=DECAL_LIFT
    links.new(disp.outputs['Displacement'],output.inputs['Displacement'])
    if hasattr(mat,'displacement_method'):mat.displacement_method='DISPLACEMENT'
    mat['c3_decal_lift']=DECAL_LIFT
   if valpha and sem.get('program') in NORMAL_ONLY_DECAL_PROGRAMS and normal_color:
    # its gBuffer pass reads only the diffuse ALPHA and writes normals: the surface below keeps its colour and
    # the grooves shade it. Approximation: multiply what is behind by (N_mapped . N)^4, faded by the alpha.
    geo=nodes.new('ShaderNodeNewGeometry');geo.location=(200,-520);dot=nodes.new('ShaderNodeVectorMath');dot.operation='DOT_PRODUCT';dot.location=(380,-520)
    links.new(nm.outputs['Normal'],dot.inputs[0]);links.new(geo.outputs['Normal'],dot.inputs[1])
    pw=nodes.new('ShaderNodeMath');pw.operation='POWER';pw.use_clamp=True;pw.location=(540,-520);pw.inputs[1].default_value=4;links.new(dot.outputs['Value'],pw.inputs[0])
    clear=nodes.new('ShaderNodeBsdfTransparent');clear.location=(540,-640);shade=nodes.new('ShaderNodeBsdfTransparent');shade.location=(700,-520);links.new(pw.outputs[0],shade.inputs['Color'])
    mix=nodes.new('ShaderNodeMixShader');mix.location=(760,-300);links.new(alpha,mix.inputs['Fac']);links.new(clear.outputs[0],mix.inputs[1]);links.new(shade.outputs[0],mix.inputs[2]);links.new(mix.outputs[0],output.inputs['Surface'])
    mat['c3_decal_mode']='normal only (gBuffer pass '+sem.get('program')+' reads diffuse alpha only): multiplies the surface below'
  overrides=MATERIAL_MAP_OVERRIDES.get(lower,{})
  if IMPORT_EXTRA_MAPS:
   si=slots.get('g_specular0_reflectanceTexture',slots.get('g_reflectance_texture'))
   if si is None and terrain:si=slots.get('g_specular1_reflectanceTexture')  # layersEnabled_1
   spec=tex(si,'Specular reflectance',True);spec_color=spec.outputs['Color'] if spec else None
   if mask and secondary is not None:
    other_s=tex(slots.get('g_specular'+str(secondary)+'_reflectanceTexture'),'Specular reflectance layer '+str(secondary),True);spec_color=blend(spec_color,other_s.outputs['Color'] if other_s else None,mask,'Terrain reflectance')
   if spec_color:
    tint=bsdf.inputs.get('Specular Tint');level=bsdf.inputs.get('Specular IOR Level') or bsdf.inputs.get('Specular')
    if tint:links.new(spec_color,tint)
    if level:links.new(spec_color,level)
    mat['c3_specular_model']='source reflectance -> Principled tint/level approximation; not metallic'
   gloss=slots.get('g_specular0_glossTexture')
   if hashed(gloss):
    im=self.channel_image(hashed(gloss),'R',True);node=tex(gloss,'Roughness from inverse gloss',True,image=im)
    if node:links.new(node.outputs['Color'],bsdf.inputs['Roughness']);maps['Roughness from inverse gloss']['conversion']='1 - R; approximation to source gloss response'
   emission_slot=slots.get('g_emissive_texture')
   if emission_slot is None:   # often only the material's separate emissive pass samples it (cars3_win_tko_master: e0797ae9.fp, slot 5)
    emission_slot=next((p_['textures']['g_emissive_texture'] for p_ in sem.get('all_passes',[]) if 'g_emissive_texture' in p_.get('textures',{})),None)
   emission=tex(emission_slot,'Emission')
   target=bsdf.inputs.get('Emission Color') or bsdf.inputs.get('Emission');em=record.get('emissive')
   if emission:
    links.new(emission.outputs['Color'],target);bsdf.inputs['Emission Strength'].default_value=1
   if em:
    # self-illuminated (g_emissive_color: rgb + strength): the emissive map, else the diffuse texture itself, glows -
    # lit windows / lamp glows / headlights have no glow texture of their own
    src=emission.outputs['Color'] if emission else color
    mul=nodes.new('ShaderNodeMix');mul.data_type='RGBA';mul.blend_type='MULTIPLY';mul.label='g_emissive_color';mul.location=(200,-760)
    mul.inputs[0].default_value=1.0;mul.inputs[7].default_value=(*em[:3],1.0)
    if src is not None:links.new(src,mul.inputs[6])
    else:mul.inputs[6].default_value=(1.0,1.0,1.0,1.0)
    links.new(mul.outputs[2],target);bsdf.inputs['Emission Strength'].default_value=em[3]
    mat['c3_emissive']='g_emissive_color rgb %.3f %.3f %.3f x strength %.3f on the %s'%(*em,'emissive map' if emission else 'diffuse texture')
   for role,entry in overrides.items():
    slot=entry['slot'];h=hashed(slot)
    if not h:continue
    image=self.channel_image(h,entry.get('channel','R'),entry.get('invert',False));node=tex(slot,'Override '+role,True,image=image)
    target={'roughness':'Roughness','metallic':'Metallic','alpha':'Alpha'}.get(role)
    if target and node:links.new(node.outputs['Color'],bsdf.inputs[target])
    elif role=='height' and node:
     bump=nodes.new('ShaderNodeBump');bump.inputs['Strength'].default_value=entry.get('strength',.1);bump.inputs['Distance'].default_value=entry.get('distance',.05);links.new(node.outputs['Color'],bump.inputs['Height'])
     if normal_color:links.new(nm.outputs['Normal'],bump.inputs['Normal'])
     links.new(bump.outputs['Normal'],bsdf.inputs['Normal'])
   connected_slots=set(used)
   for slot,h in enumerate(textures):
    if not h or slot in used:continue
    names=[n for n,i in slots.items() if i==slot];node=tex(slot,' / '.join(names) if names else 'Unclassified source map',True)
    if node:node.location=(-1120,-slot*240);node['c3_unconnected_reason']='Source map preserved; its role is not assigned to a Blender input'
  mat['c3_material_maps']=safe_json(maps);mat['c3_unassigned_channels']='Packed AG normal B is preserved in the source texture; not assumed to be height/roughness.'
  self.r['material_maps'].append({'asset':ref,'material':mid,'name':name,'shader':record['shader'],'program':sem.get('program'),'maps':maps,'unmapped_slots':[i for i,h in enumerate(textures) if h and i not in (connected_slots if IMPORT_EXTRA_MAPS else used)]})
  self.material_cache[key]=mat;return mat
 def buffer(self,ref,index,kind):
  suffix='_'+str(index)+'.'+kind;r=self.src.sibling(ref,suffix)
  if not r:raise FileNotFoundError(str(ref)+' / '+suffix)
  if r in self.buffer_cache:self.buffer_cache.move_to_end(r);return self.buffer_cache[r]
  data=self.src.read(r)
  # Bound decompressed-buffer cache to 64 MiB; meshes already hold their data.
  if len(data)<=64*1024*1024:
   while self.buffer_cache and self.buffer_bytes+len(data)>64*1024*1024:
    _,old=self.buffer_cache.popitem(last=False);self.buffer_bytes-=len(old)
   self.buffer_cache[r]=data;self.buffer_bytes+=len(data)
  return data
 def primitive(self,scene,n,p,ref,pk,records):
  key=(ref,tuple(p['Vdata']),tuple(p['Idata']),tuple(p['UnitBase']),tuple(p['UnitScale']),p.get('MaterialReference'))
  if key in self.mesh_cache:return self.mesh_cache[key]
  vd=p['Vdata'];count=vd[1];bid,off,stride=vd[2:5];vb=self.buffer(ref,bid,'vbuf');bounds=off+count*stride
  if bounds>len(vb):raise ValueError('Vertex stream exceeds buffer')
  if stride not in (8,12,16,24,28,32):raise ValueError('Unsupported position stride '+str(stride))
  q=stream_view(vb,count,off,stride,'<i2',3).astype(np.float32);pos=np.array(p['UnitBase'],np.float32)+q*np.array(p['UnitScale'],np.float32)/32767
  uv=stream_view(vb,count,off+8,stride,'<f2',2).astype(np.float32) if stride in (12,16,28,32) else np.zeros((count,2),np.float32)
  if not np.isfinite(pos).all() or not np.isfinite(uv).all():raise ValueError('Non-finite position/UV')
  idata=p['Idata'];ib=self.buffer(ref,idata[0],'ibuf');descriptor=scene.get('IndexBufferPool',{}).get('IndexBuffer#'+str(idata[0]),{});width=descriptor.get('Width')
  if width not in (1,2,4):raise ValueError('Missing/unsupported source index Width: '+str(width))
  ix=stream_view(ib,idata[3],idata[1],width,{1:'u1',2:'<u2',4:'<u4'}[width],1).ravel()
  if len(ix) and int(ix.max())>=count:raise ValueError('Index exceeds source vertex count')
  ix=ix.astype(np.int32)
  if len(ix)%3 or len(ix) and int(ix.max())>=count:raise ValueError('Invalid triangle index range')
  me=bpy.data.meshes.new('C3_'+n.get('NodeName','mesh')+'_'+pk);me.vertices.add(count);me.vertices.foreach_set('co',pos.ravel());me.loops.add(len(ix));me.loops.foreach_set('vertex_index',ix);me.polygons.add(len(ix)//3);me.polygons.foreach_set('loop_start',np.arange(0,len(ix),3,dtype=np.int32));me.polygons.foreach_set('loop_total',np.full(len(ix)//3,3,dtype=np.int32))
  # foreach_set builds faces/loops but DOES NOT create edges or corner->edge links.
  # Blender 5.2 custom normals dereference those links (native access violation).
  me.update(calc_edges=True)
  # Source batches may contain degenerate/duplicate triangles. Validate before
  # custom normals: malformed face fans can crash Blender instead of raising.
  if me.validate(clean_customdata=False):
   self.warning('mesh_topology_repaired',asset=str(ref),node=n.get('NodeName'),primitive=pk,source_indices=len(ix),remaining_loops=len(me.loops))
   me.update(calc_edges=True);ix=np.empty(len(me.loops),np.int32);me.loops.foreach_get('vertex_index',ix)
  layer=me.uv_layers.new(name='map1');layer.data.foreach_set('uv',blender_uv(uv)[ix].ravel())
  attr=me.attributes.new(name='SourceUV0',type='FLOAT2',domain='POINT');attr.data.foreach_set('vector',uv.ravel())
  colors=np.ones((count,4),np.float32);normals=None
  # First 4 bytes of the normal stream are S10_10_10_2 in known static/pivot layouts.
  if len(vd)>=8 and vd[7] in (4,8):
   nb=self.buffer(ref,vd[5],'vbuf');words=stream_view(nb,count,vd[6],vd[7],'<u4',1).ravel();normals=np.column_stack([((words>>(10*k))&1023).astype(np.int32) for k in range(3)]);normals=np.where(normals>=512,normals-1024,normals).astype(np.float32)/511;length=np.linalg.norm(normals,axis=1);valid=length>.2
   if np.all(valid):normals/=length[:,None]
   else:normals=None;self.warning('invalid_packed_normals',asset=str(ref),node=n.get('NodeName'),primitive=pk)
  if len(vd)>=11 and vd[10] in (4,8,12,16):
   cb=self.buffer(ref,vd[8],'vbuf');aux=stream_view(cb,count,vd[9],vd[10],'u1',vd[10]).copy();colors=aux[:,-4:].astype(np.float32)/255
   c=me.color_attributes.new(name='SourceAuxColor0',type='BYTE_COLOR',domain='POINT');c.data.foreach_set('color',aux[:,:4].astype(np.float32).ravel()/255)
   if vd[10]>=12:
    uv2=aux[:,4:8].copy().view('<f2').reshape(count,2).astype(np.float32)
    if np.isfinite(uv2).all():
     layer=me.uv_layers.new(name='map2');layer.data.foreach_set('uv',blender_uv(uv2)[ix].ravel());attr=me.attributes.new(name='SourceUV1',type='FLOAT2',domain='POINT');attr.data.foreach_set('vector',uv2.ravel())
  me.uv_layers.active_index=0;me.uv_layers['map1'].active_render=True;me['c3_uv_v_flipped']=FLIP_UV_V
  colorattr=me.color_attributes.new(name='SourceLightingRGBA',type='FLOAT_COLOR',domain='POINT');colorattr.data.foreach_set('color',colors.ravel())
  if normals is not None:
   attr=me.attributes.new(name='SourceNormal',type='FLOAT_VECTOR',domain='POINT');attr.data.foreach_set('vector',normals.ravel())
   me.polygons.foreach_set('use_smooth',np.ones(len(me.polygons),dtype=np.bool_))
   if len(ix):
    self.checkpoint('custom_normals',force=True,asset=ref,node=n.get('NodeName'),primitive=pk)
    try:me.normals_split_custom_set(normals[ix].tolist())
    except Exception as e:self.warning('custom_normals_api',asset=str(ref),node=n.get('NodeName'),message=str(e))
  if stride in (24,28,32):
   pivot=np.ndarray((count,4),dtype='<f4',buffer=vb,offset=off+stride-16,strides=(stride,4)).copy();attr=me.attributes.new(name='SourcePivot',type='FLOAT_VECTOR',domain='POINT');attr.data.foreach_set('vector',pivot[:,:3].ravel());attr=me.attributes.new(name='SourcePivotW',type='FLOAT',domain='POINT');attr.data.foreach_set('value',pivot[:,3]);me['c3_pose']='rest; shader pivot/wind animation not evaluated'
  if stride in (16,32):
   constraint=np.ndarray((count,4),dtype='u1',buffer=vb,offset=off+12,strides=(stride,1)).astype(np.float32)/255;c=me.color_attributes.new(name='SourceWindConstraint',type='FLOAT_COLOR',domain='POINT');c.data.foreach_set('color',constraint.ravel())
  me['c3_vdata']=safe_json(vd);me['c3_vformatCRC']=p.get('vformatCRC',0);me['c3_source_primitive']=safe_json(p);me['c3_index_width']=width;me['c3_stamp']=mesh_stamp(me)
  try:mat=self.material(scene,p['MaterialReference'],records.get(p['MaterialReference']),ref)
  except Exception as e:
   self.warning('material_node_error',asset=str(ref),material=p['MaterialReference'],message=str(e));mat=bpy.data.materials.new('UNRESOLVED_MATERIAL');mat.diffuse_color=(.8,.15,.8,1);self.material_cache[(ref,p['MaterialReference'])]=mat
  me.materials.append(mat);self.mesh_cache[key]=me;self.r['mesh_templates']+=1;self.r['vertex_format_counts'][str(stride)]+=1;self.r['index_width_counts'][str(width)]+=1
  return me
 def collision(self,scene,ref,col,external):
  sr=self.src.sibling(ref,'.btm');dr=self.src.sibling(ref,'.btd')
  if not sr or not dr:return
  try:
   btm=self.src.read(sr);btd=self.src.read(dr);assert struct.unpack_from('<I',btm)[0]==0xc007d00d;count=struct.unpack_from('<I',btm,4)[0];meshes={}
   shape_for={a['CollisionInstanceRef']:a['CollisionShapeRef'] for a in scene.get('AssociationPool',{}).values() if a.get('Type')=='CollisionShapeToCollisionInstance'}
   for key,inst in scene.get('CollisionInstancePool',{}).items():
    iid=int(key.split('#')[1]);sid=shape_for.get(iid)
    if sid is None or sid>=count:continue
    if sid not in meshes:
     start,tag=struct.unpack_from('<II',btm,8+sid*8);vo,io,nv,nt=struct.unpack_from('<QQII',btd,start);pos=np.frombuffer(btd,dtype='<f4',count=nv*3,offset=start+vo).reshape(-1,3);ix=np.frombuffer(btd,dtype='<u2',count=nt*3,offset=start+io).reshape(-1,3)
     me=bpy.data.meshes.new('Collision_'+str(sid));me.from_pydata(pos.tolist(),[],ix.tolist());meshes[sid]=me
    obj=self.object('Collision_'+str(iid),meshes[sid],col,external@matrix(inst.get('Transform')),{'collision_instance':iid,'collision_shape':sid,'asset':ref});gk=json.dumps([str(ref[0]),str(ref[1]),iid]);self.manifest['coll'][gk]=self.manifest['coll'].get(gk,0)+1;obj.display_type='WIRE';obj.hide_render=True;obj.hide_set(True);self.r['collision_instances']+=1
  except Exception as e:self.warning('collision_format',asset=str(ref),message=str(e))
 def curves(self,scene,external,col):
  for curve in scene.get('CurvesPool',{}).values():
   off=0
   for name,count,flags in zip(curve['Names'],curve['NumPoints'],curve['Flags']):
    points=np.array(curve['AllPoints'][off*3:(off+count)*3],np.float32).reshape(-1,3);off+=count;data=bpy.data.curves.new('C3_'+name,'CURVE');data.dimensions='3D';data.bevel_depth=0;spline=data.splines.new('POLY');spline.points.add(count-1);spline.points.foreach_set('co',np.column_stack((points,np.ones(count))).astype(np.float32).ravel());obj=self.object(name,data,col,external,{'curve_flags':flags,'curve_display':'raw source control polygon; not an evaluated NURBS path'});obj.hide_render=True;self.r['curves']+=1
 def asset(self,ref,external=IDENTITY,stack=(),label=None):
  if len(stack)>MAX_RECURSION:raise RuntimeError('Dependency recursion limit reached')
  if ref in stack:self.warning('dependency_cycle',asset=str(ref));return
  self.checkpoint('asset',asset=ref,label=label)
  scene=self.src.decode(ref);self.asset_counter+=1;name=label or PurePosixPath(ref[1] or ref[0]).stem;col=self.subcollection(name);self.r['assets'].append({'ref':ref,'label':name});records=self.material_records(scene,ref);self.completed[ref]=scene
  realm=PurePosixPath(ref[1] or ref[0]).stem if 'rlm_' in PurePosixPath(ref[1] or ref[0]).stem.lower() else None
  if realm and self.primary_realm is None:self.primary_realm=ACTIVE_REALM or realm
  if realm and realm!=self.primary_realm:col.hide_render=True;col.hide_viewport=True;col['c3_inactive_realm_variant']=True
  if self.asset_counter%10==0:print('CARS3_IMPORT',self.asset_counter,'assets,',self.object_count,'objects',flush=True)
  for nk,n in scene.get('SceneTreeNodePool',{}).items():
   nid=int(nk.split('#')[1]);owner=external@self.world(scene,nid);name_n=n.get('NodeName',nk)
   for pk,p in n.get('Primitives',{}).items():
    role=self.source_role(n,p,scene,ref)
    if self.filtered(role,ref,nk,pk):continue
    meta={'asset':ref,'node':nk,'primitive':pk,'material':p.get('MaterialReference'),'render_caps':p.get('RenderCaps',0),'vformatCRC':p.get('vformatCRC',0)}
    try:
     me=self.primitive(scene,n,p,ref,pk,records);instances=n.get('InstanceArray',[])
     if 'Instancedata' in p:
      first=p.get('InstanceArrayOffset',0);mats=[matrix(instances[16*i:16*(i+1)]) for i in range(first,first+p['NumInstances'])]
      if any(len(instances[16*i:16*(i+1)])!=16 for i in range(first,first+p['NumInstances'])):raise ValueError('Invalid instance matrix slice')
     else:mats=[IDENTITY]
     gk=json.dumps([str(ref[0]),str(ref[1]),nk,pk]);self.manifest['mesh'][gk]=self.manifest['mesh'].get(gk,0)+len(mats)  # what this import created (Cars 3 -> Cars 2 export: absent = deleted)
     for j,im in enumerate(mats):
      obj=self.object(name_n+'_'+pk+'_'+str(j),me,col,owner@im,meta);self.r['mesh_instances']+=1
      if role:obj['c3_role']=role
      lower=p.get('MaterialName','').lower()
      if 'shadowcaster' in lower:
       obj['c3_role']='shadow proxy';obj.visible_camera=False;obj.visible_diffuse=False;obj.visible_glossy=False
      # realm sky nodes ship Visible 0 (the game shows the active realm's at runtime): keep those shown; inactive
      # realm variants are hidden by their collection above
      if (KEEP_SOURCE_HIDDEN and not n.get('Visible',1) and role!='sky') or name_n=='StadGround':obj.hide_render=True;obj.hide_set(True);obj['c3_hidden_reason']='source visibility/coarse debug ground'
      if any(t in lower for t in ('lightray','blueglow','water','watter')):obj.visible_shadow=False
    except Exception as e:
     self.r['unsupported_geometry'].append({**meta,'name':name_n,'message':str(e)});self.object('UNRESOLVED_'+name_n,None,self.debug,owner,meta)
   if self.options['lights'] and n.get('Type')=='Light':
    data=n.get('LightData',[]);kind=n.get('LightType','Point');typ={'Directional':'SUN','Point':'POINT','Spot':'SPOT','Ambient':'AREA'}.get(kind,'POINT');light=bpy.data.lights.new(name_n,typ);light.color=tuple(max(0,min(1,c)) for c in (data[:3] if len(data)>=3 else [1,1,1]));light.energy=(data[3] if len(data)>3 else 1)*LIGHT_ENERGY_SCALE
    if typ=='SPOT':light.spot_size=math.radians(60)
    obj=self.object(name_n,light,col if realm else self.lights,owner,{'source_light':n,'lighting_units':'approximate, source units not proven watts'});self.r['lights']+=1
   elif not n.get('Primitives') and n.get('Type') not in ('Scene','Light') and (self.options['locators'] or self.source_role(n)=='light_probes' and self.options['light_probes']):
    if self.filtered(self.source_role(n),ref,nk):continue
    obj=self.object(name_n,None,self.paths,owner,{'source_node':n});obj.empty_display_size=1
   elif self.source_role(n)=='light_probes':self.filtered('light_probes',ref,nk)
  if self.options['curves']:self.curves(scene,external,self.paths)
  if self.options['collision']:self.collision(scene,ref,self.debug,external)
  associations={a['PlacementNodeRef']:a['SceneTreeNodeRef'] for a in scene.get('AssociationPool',{}).values() if a.get('Type')=='PlacementNode'}
  layouts=scene.get('LayoutPool',{})
  for layout in layouts.values():
   for ik,instance in layout.get('InstanceNodePool',{}).items():
    if not instance.get('FilePath'):
     if not CLEAN_SCENE:self.object('INLINE_LAYOUT_'+instance.get('Name',ik),None,self.debug,external,{'source_layout':instance})
     continue
    path=instance['FilePath'];pose=external@self.world(scene,associations.get(instance.get('PlacementNodeRef'),0));model,row=self.src.resolve_actor(path,ref);meta={'layout_instance':ik,'logical_path':path,'actor_definition':row or {}}
    if not IMPORT_OTHER_RACE_MODES and re.search(r'(?:_attack_|_battle_|_hang_|_trial_)',path):
     if not CLEAN_SCENE:self.object('MODE_DISABLED_'+instance.get('Name',path),None,self.debug,pose,meta)
     self.r['disabled_mode_instances'].append(meta);continue
    if model:
     self.asset(model,pose,stack+(ref,),instance.get('Name',path));self.r['resolved_layout_instances']+=1
    else:
     if not CLEAN_SCENE:self.object('UNRESOLVED_'+instance.get('Name',path),None,self.debug,pose,meta)
     self.r['unresolved_dependencies'].append({**meta,'owner':ref,'non_visual_audio_hint':'_snd' in path.lower() or '/audio/' in path.lower()})
  # Realms and dynamic references can be encoded in MER without layout entries.
  if IMPORT_DYNAMIC_DEPENDENCIES:
   merref=self.src.sibling(ref,'.mer');mer=None
   if merref:
    try:mer=self.src.decode(merref)
    except Exception as e:self.warning('meridian_decode',asset=str(merref),message=str(e))
   references=[]
   if mer:
    def walk(v,path=''):
     if isinstance(v,dict):
      for k,x in v.items():walk(x,path+'/'+k)
     elif isinstance(v,list):
      for x in v:walk(x,path)
     elif isinstance(v,str):
      if path.endswith('/ParameterStrings') and v.startswith('base64:'):
       raw=base64.b64decode(v.split(':',1)[1]+'===')
       strings=[part.decode('utf-8',errors='replace') for part in raw.split(b'\0')]
       for i,value in enumerate(strings):
        if re.match(r'^(?:characters|choreographies|realms|env_assets|objects|exploders)/',value,re.I):
         anchor=None
         for candidate in strings[i+1:i+4]:
          if '|' not in candidate:continue
          wanted=candidate.split('|')[-1];match=next((int(k.split('#')[1]) for k,n in scene.get('SceneTreeNodePool',{}).items() if n.get('NodeName')==wanted),None)
          if match is not None:anchor=match;break
         references.append((path+'/decoded',value,anchor))
        elif value.lower().startswith('rlm_'):references.append((path+'/decoded','realms/'+value,None))
      elif re.match(r'^(?:characters|choreographies|realms|env_assets|objects|exploders)/',v,re.I):references.append((path,v,None))
      elif v.lower().startswith('rlm_'):references.append((path,'realms/'+v,None))
    walk(mer)
   if self.primary_realm is None:
    candidates=[PurePosixPath(p).name for _,p,_ in references if p.lower().startswith('realms/rlm_')]
    if candidates:self.primary_realm=ACTIVE_REALM or next((p for p in candidates if 'stadium' in p or 'dusk' in p or 'outdoor' in p),candidates[0])
   used={i['FilePath'].lower() for l in layouts.values() for i in l.get('InstanceNodePool',{}).values() if i.get('FilePath')}
   visited_refs=set()
   for origin,path,anchor in references:
    if (normpath(path),anchor) in visited_refs:continue
    visited_refs.add((normpath(path),anchor))
    if normpath(path) in used:continue
    child,_=self.src.resolve_actor(path,ref)
    if child and (anchor is not None or child not in self.completed):
     pose=external@self.world(scene,anchor) if anchor is not None else external
     if anchor is not None:self.warning('meridian_anchor_inferred',resource=path,anchor_node=anchor,note='neighboring ParameterStrings locator; verify choreography placement')
     self.asset(child,pose,stack+(ref,),'Dependency_'+PurePosixPath(path).name)
    elif not child:self.r['unresolved_dependencies'].append({'owner':ref,'path':path,'meridian_field':origin})
  # Optional .bfileset variants: source actor assets, default selected explicitly.
  fileset_refs=[]
  for p,z in self.src.zips.items():
   if str(p)!=ref[0] or ref[1] is None:continue
   directory=str(PurePosixPath(ref[1]).parent)
   for member in z.namelist():
    if member.endswith('.bfileset') and str(PurePosixPath(member).parent)==directory:fileset_refs.append((str(p),member))
  for fs in fileset_refs:
   text=self.src.read(fs).decode(errors='replace');choices=re.findall(r'File\s+"([^"]+)"\s*=\s*\{(.*?)\}',text,re.S);chosen=[c for c in choices if re.search(r'Default\s*=\s*1',c[1])] or choices[:1]
   if IMPORT_ALL_FILESET_VARIANTS:chosen=choices
   for variant,body in chosen:
    match=re.search(r'FilePath\s*=\s*"([^"]+)"',body)
    if not match:continue
    child,_=self.src.resolve_actor(match[1],ref)
    if child and child not in self.completed:self.asset(child,external,stack+(ref,),'Fileset_'+variant)
    self.warning('fileset_reference_pose',fileset=fs,variant=variant,note='reference resource imported; choreography animation/attachment is not evaluated')
 def finish(self,error=None):
  self.r['objects']=self.object_count;self.r['unique_assets']=len(self.completed);self.r['elapsed_seconds']=time.time()-self.r['started'];self.r['vertex_format_counts']=dict(self.r['vertex_format_counts']);self.r['index_width_counts']=dict(self.r['index_width_counts']);self.r['fatal_error']=error;self.r['complete_geometry_claim']=False
  self.r['limitations']=['rest pose only; original wind/pivot/skin animation is not evaluated','materials and light units approximate source rendering','terrain multilayer blending is not fully implemented','dynamic MER references may lack attachment/animation transforms','source particle emitters are represented through available geometry/dependencies, not simulated','curves display original control polygons; original spline basis is not rendered']
  self.r['blender_version']=bpy.app.version_string;self.r['settings']={'flip_uv_v':FLIP_UV_V,'textures':IMPORT_TEXTURES,'clean_scene':CLEAN_SCENE,**self.options,'dynamic_dependencies':IMPORT_DYNAMIC_DEPENDENCIES,'extra_maps':IMPORT_EXTRA_MAPS,'terrain_layers':IMPORT_TERRAIN_LAYERS,'diffuse_alpha':CONNECT_DIFFUSE_ALPHA};self.r['normal_method']='validated topology with explicit edges; source normals mapped to face corners';self.r['filtered_counts']=dict(Counter(item['category'] for item in self.r['filtered_items']))
  self.collection['c3_manifest']=json.dumps(self.manifest);path=self.report_text.name;self.report_text.write(json.dumps(self.r,indent=2,ensure_ascii=False,default=str));self.collection['c3_report']=path
  if self.shaderzip:self.shaderzip.close()
  print('CARS3_IMPORT_REPORT',path,flush=True)
  print('CARS3_IMPORT_DONE',json.dumps({k:self.r[k] for k in ('objects','mesh_instances','mesh_templates','unique_assets','lights','curves','collision_instances','fatal_error')},ensure_ascii=False),flush=True)

def choose_main(src):
 if MAIN_SCENE:
  r=src.locate(MAIN_SCENE)
  if not r:raise FileNotFoundError(MAIN_SCENE)
  return r
 entries=src.entries[src.bundle];exact=[n for n in entries if PurePosixPath(n).stem==LOCATION.lower() and n.endswith('.oct')]
 if exact:return src.ref(src.bundle,entries[exact[0]])
 candidates=[]
 for n in entries:
  if n.endswith('.oct') and n.startswith('worlds/') and not any(t in n for t in ('collision','visrgn','_attack_','_battle_','_hang_','_trial_')):
   ref=src.ref(src.bundle,entries[n]);s=src.decode(ref);score=len(s.get('TrackNodePool',{}))+100*bool(s.get('LayoutPool'));candidates.append((score,n,ref))
 if not candidates:raise RuntimeError('No main world found. Set MAIN_SCENE to an exact OCT member.')
 candidates.sort(key=lambda x:(-x[0],x[1]));return candidates[0][2]
def new_report(location,bundle):
 return {'version':VERSION,'location':location,'bundle':str(bundle),'started':time.time(),'mesh_templates':0,'mesh_instances':0,'resolved_layout_instances':0,'lights':0,'curves':0,'collision_instances':0,'vertex_format_counts':Counter(),'index_width_counts':Counter(),'assets':[],'warnings':[],'unsupported_geometry':[],'unresolved_dependencies':[],'disabled_mode_instances':[],'normal_conversions':[],'filtered_items':[],'material_maps':[]}

def save_result(images):
 if not OUTPUT_BLEND:return
 if PACK_TEXTURES_ON_SAVE:
  for image in images.values():
   if image and image.source=='FILE':
    try:image.pack()
    except Exception as e:print('CARS3_IMAGE_PACK_WARNING',image.name,str(e))
 p=Path(OUTPUT_BLEND);p.parent.mkdir(parents=True,exist_ok=True);bpy.ops.wm.save_as_mainfile(filepath=str(p))

def update_materials_in_current_scene():
 objects=[o for o in bpy.context.scene.objects if o.type=='MESH' and 'c3_asset' in o and 'c3_material' in o]
 if not objects:raise RuntimeError('No imported Cars 3 meshes with source metadata in this scene')
 refs={tuple(json.loads(o['c3_asset'])) for o in objects};bundle=next((r[0] for r in refs if '/bundles/' in normpath(r[0])),next(iter(refs))[0]);cache=Path(CACHE_DIR);cache.mkdir(parents=True,exist_ok=True);report=new_report(LOCATION,bundle);report['operation']='update_materials';source=Source(GAME_ROOT,bundle,cache,report);imp=Importer(source,LOCATION,cache,report,create_scene=False);error=None;updated=set()
 try:
  for o in objects:
   ref=tuple(json.loads(o['c3_asset']));mid=o['c3_material'];scene=source.decode(ref);imp.completed[ref]=scene;records=imp.material_records(scene,ref)
   mat=imp.material(scene,mid,records.get(mid),ref)
   if o.data.name not in updated:
    if o.data.materials:o.data.materials[0]=mat
    else:o.data.materials.append(mat)
    updated.add(o.data.name)
   imp.object_count+=1;report['mesh_instances']+=1
  report['updated_mesh_datablocks']=len(updated);report['materials_rebuilt']=len(imp.material_cache);bpy.context.view_layer.update()
 except Exception:error=traceback.format_exc();raise
 finally:imp.finish(error);source.close()
 save_result(imp.images)
 return report

def run():
 root=Path(GAME_ROOT);bundle=Path(BUNDLE_PATH) if BUNDLE_PATH else root/'bundles'/('zone-'+LOCATION)/'_root_.zip'
 if not bundle.exists():raise FileNotFoundError('Bundle not found: '+str(bundle))
 report=new_report(LOCATION,bundle)
 source=Source(root,bundle,None,report);importer=Importer(source,LOCATION,None,report);error=None
 try:
  importer.checkpoint('select_main',force=True);ref=choose_main(source);report['main_scene']=ref;importer.asset(ref);report['primary_realm']=importer.primary_realm
 except Exception:
  error=traceback.format_exc();raise
 finally:importer.finish(error);source.close()
 return report
