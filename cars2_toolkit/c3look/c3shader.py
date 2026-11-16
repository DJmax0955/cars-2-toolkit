"""Cars 3 (PS4) shader reflection, no Blender needed: texture slots / constants of a material's shader passes and the
self-illumination constant. Shared by the Cars 3 location importer (cars3_location) and the converter
(tools/c3collect)."""
import math, struct

def string_at(data,p,relative):
 q=p+relative
 if not 0<=q<len(data):raise ValueError('Invalid reflection string pointer')
 end=data.index(0,q);name=data[q:end].decode('ascii')
 if len(name)>256 or not all(32<=ord(c)<127 for c in name):raise ValueError('Invalid reflection string')
 return name

def program_reflection(data):
 base=data.index(b'OrbShdr')+28
 nr,_,nc,ns=struct.unpack_from('<4I',data,base)
 if nr>256 or nc>4096 or ns>128:raise ValueError('Invalid reflection counts')
 resources=[];constants=[]
 for i in range(nr):
  p=base+28+i*24;v=struct.unpack_from('<6I',data,p);name=string_at(data,p+20,v[5]);resources.append({'name':name,'register':v[0],'type':v[2],'size':v[1],'member_start':v[4],'member_count':v[3]})
 p=base+28+nr*24
 for i in range(nc):
  q=p+i*36;v=struct.unpack_from('<9I',data,q);constants.append({'name':string_at(data,q+28,v[7]),'offset':v[1],'size':v[2],'type':v[0]})
 return resources,constants

def shader_semantics(archive,key):
 data=archive.read(key+'.shd');passes=[]
 for i in range(8):
  off=280+i*88
  if off+4>len(data):break
  name=f'{struct.unpack_from("<I",data,off)[0]:08x}.fp'
  if name not in archive.namelist():continue
  try:resources,constants=program_reflection(archive.read(name))
  except (ValueError,UnicodeError,struct.error):continue
  material_constants=[];instance_constants=[]
  for r in resources:
   if r['name']=='perMaterial':material_constants=constants[r['member_start']:r['member_start']+r['member_count']]
   if r['name']=='perInstance':instance_constants=constants[r['member_start']:r['member_start']+r['member_count']]
  textures={r['name']:r['register']-5 for r in resources if (r['name'].endswith('_texture') or r['name'].endswith('Texture')) and 5<=r['register']<5+data[27]}
  # Select the material pass with normals / specular rather than depth-only.
  score=sum(8 if 'normalMap' in n else 6 if 'reflectanceTexture' in n else 4 if 'diffuse' in n else 1 for n in textures)
  passes.append({'program':name,'pass_index':i,'textures':textures,'constants':material_constants,'instance_constants':instance_constants,'score':score})
 if not passes:return {}
 best=max(passes,key=lambda p:p['score']);best['source_texture_count']=data[27];best['all_passes']=[{'program':p['program'],'textures':p['textures']} for p in passes]
 return best

def emissive_constant(record,extra,sem):
 """g_emissive_color (rgb, strength in w) of a self-illuminated material (lamp glows, lit windows: cars3_rad_lightbuilding,
 b4895e49.fp), or None. It sits in the perInstance block with g_diffuse0_color, whose rgb the record holds at offset
 extra (the 'color' read), so it is at extra + (emissive - diffuse) offset."""
 ic={c['name']:c for c in sem.get('instance_constants',[])}
 if sem.get('emissive_partial'):return None   # angle-based / edge emission (snow sparkle, rims): not a full glow
 if 'g_emissive_color' not in ic or 'g_diffuse0_color' not in ic:return None
 o=extra+ic['g_emissive_color']['offset']-ic['g_diffuse0_color']['offset']
 if o<0 or len(record)<o+16:return None
 v=list(struct.unpack_from('<4f',record,o))
 if not all(math.isfinite(x) and 0<=x<=1000 for x in v) or v[3]<=0 or max(v[:3])<=0:return None
 return v
