# -*- coding: utf-8 -*-
"""SVG de packaging -> carton continuo con armadura y animacion.
Uso: python motor_caja_blender.py
Opciones: --svg troquel.svg --grosor 3 --salida caja.blend --units auto
Python estandar + Blender 5.2+. Salidas auxiliares exclusivamente en baul/.
Derivado de crear_estuche_desde_svg.py; conserva bandas de flexion y malla volumetrica.
"""
from pathlib import Path
from collections import defaultdict, Counter
import argparse, datetime, hashlib, itertools, json, math, os, re, shutil
import subprocess, sys, traceback, xml.etree.ElementTree as ET
try:
    import bpy,bmesh
    from mathutils import Vector,Matrix,Quaternion
except ImportError:
    bpy=None
ESPESOR_MM=3.
ANCHO_HENDIDO_MM=4.8
HUNDIMIENTO_INTERIOR_MM=1.05
HUNDIMIENTO_EXTERIOR_MM=.42
BANDA_FLEXION_MM=6.
DIVISIONES_FLEXION=16
FAMILIA=None
PLAN={}
ROOT=Path(os.environ.get('MOTOR_CAJA_ROOT',str(Path(__file__).resolve().parent))).resolve()
BAUL=ROOT/'baul'
def material_slots_and_double_crease(obj,exterior_depth=HUNDIMIENTO_EXTERIOR_MM):
    """Reutilizable sobre la malla del prototipo; caras por topología, no por orientación de pose."""
    me=obj.data;n=len(me.vertices)//2
    if len(me.vertices)%2:raise RuntimeError('Malla incompatible: faltan parejas exterior/interior.')
    old_depth=float(obj.get('profundidad_hendido_mm',HUNDIMIENTO_INTERIOR_MM))
    if old_depth<=0:raise RuntimeError('Profundidad interior inválida.')
    attr=me.attributes.get('cara_carton') or me.attributes.new('cara_carton','INT','FACE')
    for i in range(n):
        outside,inside=me.vertices[i],me.vertices[i+n]
        if abs(outside.co.x-inside.co.x)>1e-5 or abs(outside.co.y-inside.co.y)>1e-5:raise RuntimeError('Malla incompatible: parejas de espesor desalineadas en reposo.')
        factor=max(0.,min(1.,(ESPESOR_MM-inside.co.z)/old_depth))
        outside.co.z=exterior_depth*factor
        if inside.co.z-outside.co.z<=0:raise RuntimeError('Los hundimientos agotan el espesor del cartón.')
    me.materials.clear()
    specs=[('01_Exterior_Blanco',(1.,1.,1.,1.)),('02_Interior_Marron',(.26,.115,.04,1.)),('03_Canto_Carton',(.38,.22,.095,1.))]
    for name,color in specs:
        material=bpy.data.materials.new(name);material.use_nodes=True;material.diffuse_color=color
        nodes=material.node_tree.nodes;nodes.clear();shader=nodes.new('ShaderNodeBsdfPrincipled');out=nodes.new('ShaderNodeOutputMaterial')
        shader.inputs['Base Color'].default_value=color;shader.inputs['Roughness'].default_value=.82
        material.node_tree.links.new(shader.outputs['BSDF'],out.inputs['Surface']);me.materials.append(material)
    counts=Counter()
    for face in me.polygons:
        slot=0 if all(i<n for i in face.vertices) else 1 if all(i>=n for i in face.vertices) else 2
        face.material_index=slot;attr.data[face.index].value=slot;counts[slot]+=1
    me.update();obj['hundimiento_exterior_mm']=exterior_depth;obj['hundimiento_interior_mm']=old_depth
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type=='VIEW_3D':area.spaces.active.shading.type='SOLID';area.spaces.active.shading.color_type='MATERIAL'
    return {'materiales':[m.name for m in me.materials],'caras_por_slot':dict(counts),'espesor_minimo_plano_mm':min(me.vertices[i+n].co.z-me.vertices[i].co.z for i in range(n)),'hundimiento_exterior_max_mm':max(v.co.z for v in me.vertices[:n]),'hundimiento_interior_max_mm':max(ESPESOR_MM-v.co.z for v in me.vertices[n:])}


"""Lectura SVG y reconocimiento geométrico de la familia de estuche de 19 paneles."""
NUMBER=r'[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?'

def numbers(text):return [float(v) for v in re.findall(NUMBER,text)]

def svg_transform(text):
    out=Matrix.Identity(3)
    for name,body in re.findall(r'([a-zA-Z]+)\s*\(([^)]*)\)',text or ''):
        a=numbers(body);name=name.lower();m=Matrix.Identity(3)
        if name=='matrix':
            if len(a)!=6:raise ValueError('Transformación SVG matrix inválida')
            m=Matrix(((a[0],a[2],a[4]),(a[1],a[3],a[5]),(0,0,1)))
        elif name=='translate':m[0][2]=a[0];m[1][2]=a[1] if len(a)>1 else 0
        elif name=='scale':m[0][0]=a[0];m[1][1]=a[1] if len(a)>1 else a[0]
        elif name=='rotate':
            t=math.radians(a[0]);c,s=math.cos(t),math.sin(t);m=Matrix(((c,-s,0),(s,c,0),(0,0,1)))
            if len(a)==3:
                t1=Matrix.Identity(3);t1[0][2]=a[1];t1[1][2]=a[2];t2=t1.copy();t2[0][2]*=-1;t2[1][2]*=-1;m=t1@m@t2
        elif name in ('skewx','skewy'):m[0 if name=='skewx' else 1][1 if name=='skewx' else 0]=math.tan(math.radians(a[0]))
        else:raise ValueError('Transformación SVG no soportada: '+name)
        out=out@m
    return out

def svg_root_scale(root,units):
    factors={'mm':1.,'cm':10.,'in':25.4,'pt':25.4/72,'pc':25.4/6,'px':25.4/96}
    view=numbers(root.get('viewBox',''))
    def physical(value):
        m=re.fullmatch(r'\s*('+NUMBER+r')\s*(mm|cm|in|pt|pc|px)\s*',value or '')
        return float(m[1])*factors[m[2]] if m else None
    width=physical(root.get('width'));height=physical(root.get('height'))
    matrix=Matrix.Identity(3)
    if units=='auto' and width is not None and len(view)==4:
        x,y,w,h=view;sx=width/w;sy=height/h if height is not None else sx
        preserve=root.get('preserveAspectRatio','xMidYMid meet')
        if 'none' not in preserve:
            scale=max(sx,sy) if 'slice' in preserve else min(sx,sy);dx=(width-w*scale);dy=((height or h*scale)-h*scale)
            ax=0 if 'xMin' in preserve else 1 if 'xMax' in preserve else .5;ay=0 if 'YMin' in preserve else 1 if 'YMax' in preserve else .5
            matrix[0][0]=matrix[1][1]=scale;matrix[0][2]=-x*scale+dx*ax;matrix[1][2]=-y*scale+dy*ay
        else:matrix[0][0]=sx;matrix[1][1]=sy;matrix[0][2]=-x*sx;matrix[1][2]=-y*sy
        reason='Tamaño físico declarado en SVG y viewBox'
    else:
        key='pt' if units=='auto' else units;scale=factors[key];matrix[0][0]=matrix[1][1]=scale
        reason='Unidades elegidas: '+key+(' (perfil Illustrator por defecto, sin tamaño físico declarado)' if units=='auto' else '')
    return matrix,reason

def svg_paths(d,step):
    tokens=re.findall('[a-zA-Z]|'+NUMBER,d);i=0;cmd=None;pos=0j;start=0j;control=None;previous=None;out=[];points=[]
    count={'M':2,'L':2,'H':1,'V':1,'C':6,'S':4,'Q':4,'T':2,'A':7,'Z':0}
    def add(z):points.append([float(z.real),float(z.imag)])
    while i<len(tokens):
        if tokens[i].isalpha():cmd=tokens[i];i+=1
        if cmd is None or cmd.upper() not in count:raise ValueError('Comando de path SVG no reconocido')
        op=cmd.upper();relative=cmd.islower()
        if op=='Z':
            add(start);pos=start;out.append(points);points=[];cmd=None;control=None;previous='Z';continue
        n=count[op]
        if i+n>len(tokens):raise ValueError('Path SVG incompleto')
        a=list(map(float,tokens[i:i+n]));i+=n
        def p(x,y):return complex(x,y)+(pos if relative else 0j)
        if op=='M':
            if points:out.append(points);points=[]
            pos=p(*a);start=pos;add(pos);cmd='l' if relative else 'L';control=None
        elif op=='L':pos=p(*a);add(pos);control=None
        elif op=='H':pos=complex(a[0]+(pos.real if relative else 0),pos.imag);add(pos);control=None
        elif op=='V':pos=complex(pos.real,a[0]+(pos.imag if relative else 0));add(pos);control=None
        elif op in ('C','S','Q','T'):
            if op=='C':c1,c2,end=p(*a[:2]),p(*a[2:4]),p(*a[4:6]);last=c2
            elif op=='S':c1=2*pos-control if previous in ('C','S') and control is not None else pos;c2,end=p(*a[:2]),p(*a[2:4]);last=c2
            else:
                qc=p(*a[:2]) if op=='Q' else 2*pos-control if previous in ('Q','T') and control is not None else pos
                end=p(*a[2:4]) if op=='Q' else p(*a);c1=pos+2/3*(qc-pos);c2=end+2/3*(qc-end);last=qc
            length=abs(c1-pos)+abs(c2-c1)+abs(end-c2);steps=max(8,math.ceil(length/step))
            for j in range(1,steps+1):
                t=j/steps;add((1-t)**3*pos+3*(1-t)**2*t*c1+3*(1-t)*t*t*c2+t**3*end)
            pos=end;control=last
        elif op=='A':
            rx,ry,deg,large,sweep,x,y=a;end=p(x,y);rx,ry=abs(rx),abs(ry)
            if min(rx,ry)<1e-10 or abs(end-pos)<1e-10:pos=end;add(pos);control=None;previous=op;continue
            phi=math.radians(deg);cp,sp=math.cos(phi),math.sin(phi);delta=(pos-end)/2;xp=cp*delta.real+sp*delta.imag;yp=-sp*delta.real+cp*delta.imag
            lam=xp*xp/(rx*rx)+yp*yp/(ry*ry)
            if lam>1:rx*=math.sqrt(lam);ry*=math.sqrt(lam)
            numerator=max(0,rx*rx*ry*ry-rx*rx*yp*yp-ry*ry*xp*xp);denominator=rx*rx*yp*yp+ry*ry*xp*xp
            k=(-1 if bool(large)==bool(sweep) else 1)*math.sqrt(numerator/denominator) if denominator else 0
            cxp,cyp=k*rx*yp/ry,-k*ry*xp/rx;mid=(pos+end)/2;center=complex(cp*cxp-sp*cyp+mid.real,sp*cxp+cp*cyp+mid.imag)
            u=complex((xp-cxp)/rx,(yp-cyp)/ry);v=complex((-xp-cxp)/rx,(-yp-cyp)/ry);a0=math.atan2(u.imag,u.real);da=math.atan2((u.conjugate()*v).imag,(u.conjugate()*v).real)
            if not sweep and da>0:da-=2*math.pi
            if sweep and da<0:da+=2*math.pi
            steps=max(8,math.ceil(abs(da)*max(rx,ry)/step))
            for j in range(1,steps+1):
                t=a0+da*j/steps;add(center+complex(cp*rx*math.cos(t)-sp*ry*math.sin(t),sp*rx*math.cos(t)+cp*ry*math.sin(t)))
            pos=end;control=None
        previous=op
    if points:out.append(points)
    return [p for p in out if len(p)>1]

def read_svg(path,units):
    root=ET.parse(path).getroot();root_matrix,reason=svg_root_scale(root,units);records=[];css={}
    for el in root.iter():
        if el.tag.split('}')[-1]=='style':
            for selector,body in re.findall(r'([^{}]+)\{([^{}]+)\}',el.text or ''):
                for name in selector.split(','):css.setdefault(name.strip(),{}).update({k.strip():v.strip() for k,v in re.findall(r'([\w-]+)\s*:\s*([^;]+)',body)})
    def visit(el,matrix,style):
        tag=el.tag.split('}')[-1]
        if 'dimension' in el.get('id','').lower():return
        if tag in ('defs','metadata','title','desc','style','text','image','clipPath','mask'):return
        style=style.copy()
        for name in el.get('class','').split():style.update(css.get('.'+name,{}))
        for key in ('display','visibility','stroke','fill'):
            if el.get(key) is not None:style[key]=el.get(key)
        style.update({k.strip():v.strip() for k,v in re.findall(r'([\w-]+)\s*:\s*([^;]+)',el.get('style',''))})
        if style.get('display')=='none' or style.get('visibility')=='hidden':return
        if tag=='use':raise ValueError('El SVG contiene instancias <use>. Expándelas a trazados antes de generar el estuche.')
        matrix=matrix@svg_transform(el.get('transform',''))
        step=.7/max(Vector((matrix[0][0],matrix[1][0])).length,Vector((matrix[0][1],matrix[1][1])).length,1e-9)
        paths=[]
        if tag=='line':paths=[[[float(el.get('x1',0)),float(el.get('y1',0))],[float(el.get('x2',0)),float(el.get('y2',0))]]]
        elif tag in ('polyline','polygon'):
            a=numbers(el.get('points',''));p=list(map(list,zip(a[::2],a[1::2])))
            if tag=='polygon' and p and p[0]!=p[-1]:p.append(p[0])
            paths=[p]
        elif tag=='path':paths=svg_paths(el.get('d',''),step)
        elif tag=='rect':
            x,y,w,h=[float(el.get(k,0)) for k in ('x','y','width','height')]
            if float(el.get('rx',0)) or float(el.get('ry',0)):raise ValueError('Convierte los rectángulos redondeados a path antes de usar este SVG.')
            paths=[[[x,y],[x+w,y],[x+w,y+h],[x,y+h],[x,y]]]
        elif tag in ('circle','ellipse'):
            x,y=float(el.get('cx',0)),float(el.get('cy',0));rx=float(el.get('r',el.get('rx',0)));ry=float(el.get('r',el.get('ry',0)));n=max(64,math.ceil(2*math.pi*max(rx,ry)/step))
            paths=[[[x+rx*math.cos(2*math.pi*j/n),y+ry*math.sin(2*math.pi*j/n)] for j in range(n+1)]]
        for p in paths:
            if len(p)<2:continue
            if style.get('stroke')=='none' and style.get('fill','none')=='none':continue
            pts=[]
            for x,y in p:
                q=matrix@Vector((x,y,1));pts.append([q.x,q.y])
            records.append({'id':f'E{len(records)+1:04d}','id_original':el.get('id',''),'tipo':tag,'estilo':style.copy(),'puntos_svg':pts})
        for child in el:visit(child,matrix,style)
    visit(root,root_matrix,{'fill':'none'})
    if not records:raise ValueError('No hay trazados de troquel en el SVG.')
    return records,reason

def topology(records):
    from mathutils.geometry import delaunay_2d_cdt
    vertices=[];edges=[];sources=[];segments=[]
    for rec in records:
        start=len(vertices);vertices.extend(Vector(p) for p in rec['puntos_svg'])
        for j in range(len(rec['puntos_svg'])-1):
            a,b=vertices[start+j],vertices[start+j+1]
            if (a-b).length>1e-7:edges.append((start+j,start+j+1));sources.append(rec['id']);segments.append((a,b,rec['id']))
    # Weld duplicated SVG endpoints before CDT; Illustrator rounds each path separately.
    grid=defaultdict(list);canonical=[];mapping=[];tol=.02
    for vtx in vertices:
        cell=(math.floor(vtx.x/tol),math.floor(vtx.y/tol));found=None
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                for idx in grid.get((cell[0]+dx,cell[1]+dy),[]):
                    if (canonical[idx]-vtx).length<tol:found=idx;break
        if found is None:
            found=len(canonical);canonical.append(vtx);grid[cell].append(found)
        mapping.append(found)
    es=[];ss=[]
    for (a,b),source in zip(edges,sources):
        a,b=mapping[a],mapping[b]
        if a!=b:es.append((a,b));ss.append(source)
    vertices,edges,sources=canonical,es,ss
    segments=[(vertices[a],vertices[b],name) for (a,b),name in zip(edges,sources)]
    repairs=[]
    def cross(a,b):return a.x*b.y-a.y*b.x
    def segment_distance(p,a,b):
        d=b-a;t=max(0,min(1,(p-a).dot(d)/d.length_squared));return (p-a-d*t).length
    for a,b,name in segments:
        if (b-a).length<20 or min(abs(a.x-b.x),abs(a.y-b.y))>.001:continue
        for end,other in ((a,b),(b,a)):
            direction=(end-other).normalized();best=None;connected=False
            for c,d,other_name in segments:
                if (c==a and d==b) or (c==b and d==a):continue
                if segment_distance(end,c,d)<.03:connected=True;break
                span=d-c;den=cross(direction,span)
                if abs(den)<1e-9:continue
                t=cross(c-end,span)/den;u=cross(c-end,direction)/den
                if 0<t<=.75 and -1e-6<=u<=1+1e-6 and (best is None or t<best):best=t
            if best is not None and not connected:
                i=len(vertices);vertices.extend([end.copy(),end+direction*best]);edges.append((i,i+1));sources.append(name);repairs.append({'elemento':name,'longitud_mm':best,'tipo':'extension de particion de malla; corte original sin modificar'})
    v,e,f,ov,oe,of=delaunay_2d_cdt(vertices,edges,[],0,.008,True)
    provenance={tuple(sorted(pair)):[sources[i] for i in oe[k]] for k,pair in enumerate(e)}
    ef=defaultdict(list)
    for k,face in enumerate(f):
        for a,b in zip(face,face[1:]+face[:1]):ef[tuple(sorted((a,b)))].append(k)
    graph=defaultdict(list)
    for pair,fs in ef.items():
        if len(fs)==2 and not provenance.get(pair):graph[fs[0]].append(fs[1]);graph[fs[1]].append(fs[0])
    seen=set();regions=[];face_region={}
    for i in range(len(f)):
        if i in seen:continue
        pending=[i];seen.add(i);ids=[]
        while pending:
            k=pending.pop();ids.append(k)
            for nxt in graph[k]:
                if nxt not in seen:seen.add(nxt);pending.append(nxt)
        area=0;center=Vector((0,0));pts=[];border=set();outer=False;rid=len(regions)
        for k in ids:
            face_region[k]=rid;p=[v[j] for j in f[k]];ar=abs(cross(p[1]-p[0],p[2]-p[0]))/2
            area+=ar;center+=sum(p,Vector((0,0)))/3*ar;pts.extend(p)
            for a,b in zip(f[k],f[k][1:]+f[k][:1]):
                pair=tuple(sorted((a,b)));border.update(provenance.get(pair,[]));outer|=len(ef[pair])==1
        if area<=0:raise ValueError('Región degenerada del SVG.')
        regions.append({'id':rid,'triangulos':ids,'area_svg':area,'centro':list(center/area),'bbox':[min(p.x for p in pts),min(p.y for p in pts),max(p.x for p in pts),max(p.y for p in pts)],'bordes':sorted(border),'borde_convexo':outer})
    shared=defaultdict(list)
    for pair,fs in ef.items():
        if len(fs)==2 and face_region[fs[0]]!=face_region[fs[1]]:shared[tuple(sorted((face_region[fs[0]],face_region[fs[1]])))].append(pair)
    return {'vertices':[list(p) for p in v],'aristas':[list(p) for p in e],'caras':[list(p) for p in f],'origen_aristas':[provenance.get(tuple(sorted(p)),[]) for p in e],'regiones':regions,'reparaciones_particion':repairs},shared

def recognize_panels(top,shared):
    regs={r['id']:r for r in top['regiones']};verts=top['vertices'];adj=defaultdict(set)
    for a,b in shared:adj[a].add(b);adj[b].add(a)
    def size(i):r=regs[i]['bbox'];return r[2]-r[0],r[3]-r[1]
    def joint(a,b,axis):
        groups=defaultdict(list);fixed=0 if axis=='x' else 1;along=1-fixed
        for p,q in shared.get(tuple(sorted((a,b))),[]):
            p,q=verts[p],verts[q]
            if abs(p[fixed]-q[fixed])>.002:continue
            pos=(p[fixed]+q[fixed])/2;groups[round(pos/.015)].append((pos,min(p[along],q[along]),max(p[along],q[along])))
        if not groups:return None
        segs=max(groups.values(),key=lambda g:sum(b-a for _,a,b in g));length=sum(b-a for _,a,b in segs)
        if length<.01:return None
        spans=[]
        for _,a,b in sorted(segs,key=lambda s:s[1]):
            if spans and a<=spans[-1][1]+.015:spans[-1][1]=max(b,spans[-1][1])
            else:spans.append([a,b])
        return {'axis':axis,'pos':sum(p*(b-a) for p,a,b in segs)/length,'min':min(a for _,a,b in segs),'max':max(b for _,a,b in segs),'segments':spans,'length':length}
    holes=[]
    for i,r in regs.items():
        w,h=size(i)
        if min(w,h)>1e-5 and 2<max(w,h)/min(w,h)<15 and r['area_svg']/(w*h)>.998 and not r['borde_convexo']:holes.append(i)
    candidates=[i for i in regs if len(set(holes)&adj[i])>=4]
    if len(candidates)!=1:raise ValueError('No se identifica inequívocamente la base con cuatro ranuras. Se admite la misma familia de estuche de 19 paneles; comprueba unidades, trazados y orientación del SVG.')
    base=candidates[0];bw,bh=size(base);bx,by=regs[base]['centro'];roles={'Base':base};walls={}
    for label,axis,sign in [('izq','x',-1),('der','x',1),('sup','y',-1),('inf','y',1)]:
        found=[]
        for nxt in adj[base]:
            j=joint(base,nxt,axis)
            if j is None or j['length']<.4*(bh if axis=='x' else bw):continue
            if (regs[nxt]['centro'][0 if axis=='x' else 1]-(bx if axis=='x' else by))*sign<=0:continue
            found.append((j['length'],nxt))
        if not found:raise ValueError('No se reconoce una pared de la base: '+label)
        walls[label]=max(found)[1]
    if len(set(walls.values()))!=4:raise ValueError('Las paredes detectadas no forman cuatro paneles distintos.')
    rear=[]
    for label in ('sup','inf'):
        wall=walls[label];sign=-1 if label=='sup' else 1
        for nxt in adj[wall]-{base}:
            j=joint(wall,nxt,'y')
            if j and j['length']>.6*bw and regs[nxt]['area_svg']>.5*regs[base]['area_svg'] and (regs[nxt]['centro'][1]-regs[wall]['centro'][1])*sign>0:rear.append((wall,nxt,label))
    if len(rear)!=1:raise ValueError('No se reconoce inequívocamente la pared trasera y su tapa.')
    back,lid,back_label=rear[0];front=walls['inf' if back_label=='sup' else 'sup'];roles.update(Frente=front,Trasera=back,Lateral_Izq=walls['izq'],Lateral_Der=walls['der'],Tapa=lid)
    def side_neighbor(panel,axis,sign,excluded,min_length):
        found=[];coord=0 if axis=='x' else 1;center=regs[panel]['centro'][coord]
        for nxt in adj[panel]-set(excluded):
            j=joint(panel,nxt,axis)
            if j and j['length']>=min_length and (regs[nxt]['centro'][coord]-center)*sign>0:found.append((j['length'],nxt))
        if not found:raise ValueError('Falta una solapa o una unión lateral del panel '+str(panel))
        return max(found)[1]
    for name,panel in [('Frente',front),('Trasera',back)]:
        for side,sign in [('Izq',-1),('Der',1)]:roles[f'Oreja_{name}_{side}']=side_neighbor(panel,'x',sign,{base,lid},size(panel)[1]*.65)
    for side,sign in [('Izq',-1),('Der',1)]:
        wall=roles['Lateral_'+side];bridge=side_neighbor(wall,'x',sign,{base},bh*.6)
        if size(bridge)[0]>.3*size(wall)[0]:raise ValueError('No se identifica el puente estrecho de la pared doble.')
        inner=side_neighbor(bridge,'x',sign,{wall},bh*.6);roles['Puente_'+side]=bridge;roles['Retorno_'+side]=inner
        roles['Ala_Tapa_'+side]=side_neighbor(lid,'x',sign,{back},size(lid)[1]*.65)
    backsign=1 if regs[back]['centro'][1]>by else -1
    lip=side_neighbor(lid,'y',backsign,{back},size(lid)[0]*.65);roles['Frontal_Tapa']=lip
    for side,sign in [('Izq',-1),('Der',1)]:roles['Oreja_Frontal_Tapa_'+side]=side_neighbor(lip,'x',sign,{lid},size(lip)[1]*.65)
    if len(roles)!=19 or len(set(roles.values()))!=19:raise ValueError('La topología no coincide con los 19 paneles esperados. No se inventará una caja diferente.')
    unexplained=[r['id'] for r in regs.values() if r['id'] not in roles.values() and not r['borde_convexo'] and r['area_svg']>.15*regs[base]['area_svg']]
    if unexplained:raise ValueError('Hay regiones interiores grandes sin identificar: '+str(unexplained))
    parents={'Frente':'Base','Trasera':'Base','Lateral_Izq':'Base','Lateral_Der':'Base','Tapa':'Trasera','Frontal_Tapa':'Tapa'}
    for side in ('Izq','Der'):
        parents['Oreja_Frente_'+side]='Frente';parents['Oreja_Trasera_'+side]='Trasera';parents['Puente_'+side]='Lateral_'+side;parents['Retorno_'+side]='Puente_'+side;parents['Ala_Tapa_'+side]='Tapa';parents['Oreja_Frontal_Tapa_'+side]='Frontal_Tapa'
    hinges=[]
    for child,parent in parents.items():
        axis='y' if child in ('Frente','Trasera','Tapa','Frontal_Tapa') else 'x';j=joint(roles[parent],roles[child],axis)
        if j is None:raise ValueError('Falta el eje recto de doblez de '+child)
        coord=0 if axis=='x' else 1;side=1 if regs[roles[child]]['centro'][coord]>j['pos'] else -1
        if child in ('Frente','Trasera'):start,end=1,35
        elif child.startswith('Oreja_Frente_') or child.startswith('Oreja_Trasera_'):start,end=38,65
        elif child.startswith('Ala_Tapa_'):start,end=68,95
        elif child.startswith('Lateral_'):start,end=98,135
        elif child.startswith('Puente_'):start,end=138,170
        elif child.startswith('Retorno_'):start,end=173,210
        elif child=='Tapa':start,end=213,275
        elif child.startswith('Oreja_Frontal_Tapa_'):start,end=278,305
        else:start,end=308,365
        hinges.append(dict(j,child=child,parent=parent,side=side,angle=-90*side,start=start,end=end))
    return roles,hinges


def hsegments(h):
    return h.get("segments", [[h["min"], h["max"]]])

def point(x,y,z=0): return Vector(((x-CX)*S,(CY-y)*S,z))



def make_volume(points,owners,faces,boundaries):
    n=len(points); verts=[]; allfaces=[]; face_panels=[]
    for side in (0,1):
        for p in points:
            x=p.x/S+CX;y=CY-p.y/S
            # Only true adjacent score segments can compress this vertex.
            hs=[h for h in H if IDS[h['parent']] in owners[len(verts)%n] or IDS[h['child']] in owners[len(verts)%n]]
            d=min([distance(h,x,y) for h in hs],default=R+1)
            depth=C['crease_depth']*.5*(1+math.cos(math.pi*d/R)) if d<R else 0
            verts.append((p.x,p.y,0 if side==0 else TH-depth))
    for indices,k in faces:
        allfaces.append(list(reversed(indices)));face_panels.append(k)
        allfaces.append([i+n for i in indices]);face_panels.append(k)
    for a,b,k in boundaries:
        allfaces.append([a,b,b+n,a+n]);face_panels.append(k)
    me=bpy.data.meshes.new('Carton_3mm_Hendidos_Reales');me.from_pydata(verts,[],allfaces);me.update()
    attr=me.attributes.new('panel_svg','INT','FACE')
    for d,k in zip(attr.data,face_panels):d.value=k
    obj=bpy.data.objects.new('Estuche_Carton_3mm',me);bpy.context.scene.collection.objects.link(obj)
    # Recalculate a closed volume, including all original internal cut walls.
    bm=bmesh.new();bm.from_mesh(me);bmesh.ops.recalc_face_normals(bm,faces=list(bm.faces));bm.to_mesh(me);bm.free();me.update()
    obj.color=(.46,.31,.16,1)
    obj['espesor_nominal_mm']=TH;obj['ancho_hendido_mm']=2*R;obj['profundidad_hendido_mm']=C['crease_depth']
    obj['origen_svg']='';obj['escala_mm_por_unidad_svg']=S
    obj['paneles']=json.dumps(P,ensure_ascii=False)
    return obj

def fullstate(name):
    state=[]
    while name!='Base':state.append((index[name],STEPS));name=bychild[name]['parent']
    return tuple(sorted(state))





"""Ejecución de la herramienta independiente."""
ROLE_PRIORITY={name:i for i,name in enumerate(['Retorno_Izq','Oreja_Frontal_Tapa_Izq','Ala_Tapa_Izq','Base','Frontal_Tapa','Oreja_Frente_Izq','Tapa','Trasera','Lateral_Izq','Oreja_Trasera_Izq','Ala_Tapa_Der','Oreja_Frontal_Tapa_Der','Oreja_Frente_Der','Oreja_Trasera_Der','Retorno_Der','Lateral_Der','Frente','Puente_Der','Puente_Izq'])}

def bend_estuche(h,f,angle):
    if not f or abs(angle)<1e-9:return Matrix.Identity(4)
    mid=(h['min']+h['max'])/2; pivot=point(h['pos'],mid,TH/2) if h['axis']=='x' else point(mid,h['pos'],TH/2)
    u=Vector((h['side'],0,0)) if h['axis']=='x' else Vector((0,-h['side'],0));axis=Vector((0,1,0)) if h['axis']=='x' else Vector((1,0,0))
    child=h['child'];a=0.;length=2*BR
    if child.startswith('Oreja_') and not child.startswith('Oreja_Frontal_Tapa'):
        length=TH*.55*math.pi/2;a=2*BR-length
    elif child in ('Lateral_Izq','Lateral_Der'):
        length=TH*1.6/3*math.pi/2;a=TH*2.9/3
    elif child in ('Ala_Tapa_Izq','Ala_Tapa_Der'):
        length=TH*.55*math.pi/2;a=0
    s=2*BR*f;before=min(s,a);arc=max(0,min(length,s-a));after=max(0,s-a-length)
    phi=angle*arc/length;rad=length/abs(angle)
    q=-BR+before+rad*math.sin(abs(phi))+after*math.cos(abs(angle))
    z=rad*(1-math.cos(abs(phi)))+after*math.sin(abs(angle))
    dst=pivot+u*q+Vector((0,0,z));src=pivot+u*(s-BR)
    rot=Matrix.Rotation(phi,4,axis);rot.translation=dst-rot.to_3x3()@src+u*(h.get('inward_clearance_mm',0)*f*min(1,abs(angle)/(math.pi/2)));return rot



def unique_path(path):
    path=Path(path).resolve();candidate=path;n=2
    while candidate.exists():candidate=path.with_name(f'{path.stem}_{n:02d}{path.suffix}');n+=1
    candidate.parent.mkdir(parents=True,exist_ok=True)
    return candidate


def dimension_scale(root):
    """Recover millimetres from labeled dimension groups, including transforms."""
    values=[]
    def visit(group,parent):
        matrix=parent@svg_transform(group.get('transform',''))
        if 'dimension' in group.get('id','').lower():
            texts=[''.join(e.itertext()).strip() for e in group.iter() if e.tag.split('}')[-1]=='text']
            lines=[e for e in group if e.tag.split('}')[-1]=='line']
            if len(texts)==1 and len(lines)==2:
                m=re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*(?:mm)?\s*',texts[0])
                if m:
                    points=[]
                    for line in lines:
                        world=matrix@svg_transform(line.get('transform',''))
                        points.append([world@Vector((float(line.get(x,0)),float(line.get(y,0)),1)) for x,y in [('x1','y1'),('x2','y2')]])
                    a,b=points;direction=a[1]-a[0];other=b[1]-b[0]
                    if min(direction.length,other.length)>1e-8:
                        direction.normalize();other.normalize()
                        if abs(direction.dot(other))>.999:
                            delta=b[0]-a[0];span=(delta-direction*delta.dot(direction)).length
                            if span>1e-8:values.append(float(m[1].replace(',','.'))/span)
        for child in group:visit(child,matrix)
    visit(root,Matrix.Identity(3))
    if not values:return None
    scale=sorted(values)[len(values)//2]
    if any(abs(v/scale-1)>.005 for v in values):raise ValueError('Las cotas del SVG tienen escalas incompatibles. Exporta a escala uniforme.')
    return scale


_root_scale=svg_root_scale
def svg_root_scale(root,units):
    if units!='auto':return _root_scale(root,units)
    declared=bool(re.search(r'(mm|cm|in|pt|pc|px)\s*$',root.get('width','')))
    scale=dimension_scale(root)
    if declared:
        matrix,reason=_root_scale(root,units)
        if scale and abs(matrix[0][0]/scale-1)>.005:raise ValueError('El tamaño físico y las cotas se contradicen. Revisa el SVG o fuerza --units.')
        return matrix,reason
    matrix=Matrix.Identity(3);matrix[0][0]=matrix[1][1]=scale or 1.
    return matrix,('Escala recuperada de cotas en mm: %.9f'%scale if scale else 'Sin tamaño físico ni cotas: 1 unidad SVG = 1 mm')


def is_score(record):
    stroke=record['estilo'].get('stroke','').lower().replace(' ','')
    names={'red':'#ff0000','magenta':'#ff00ff','fuchsia':'#ff00ff'}
    stroke=names.get(stroke,stroke)
    if re.fullmatch(r'#[0-9a-f]{3}',stroke):stroke='#'+''.join(c*2 for c in stroke[1:])
    if re.fullmatch(r'#[0-9a-f]{6}',stroke):rgb=[int(stroke[i:i+2],16)/255 for i in (1,3,5)]
    elif stroke.startswith('rgb('):
        raw=stroke[4:-1].split(',')
        if len(raw)!=3:return False
        rgb=[float(v[:-1])/100 if v.endswith('%') else float(v)/255 for v in raw]
    else:return False
    r,g,b=rgb
    return r>.5 and g<r*.55 and (b<r*.45 or b>r*.45)


_read_svg=read_svg
def read_svg(path,units):
    records,reason=_read_svg(path,units)
    scores=[r for r in records if is_score(r)]
    refs=scores or records
    grids=[[],[]]
    for rec in refs:
        for a,b in zip(rec['puntos_svg'],rec['puntos_svg'][1:]):
            if math.dist(a,b)<10:continue
            for axis in (0,1):
                if abs(a[axis]-b[axis])<.03:grids[axis].append((a[axis]+b[axis])/2)
    for axis in (0,1):
        clusters=[]
        for value in sorted(grids[axis]):
            if clusters and value-clusters[-1][0]<.03:clusters[-1].append(value)
            else:clusters.append([value])
        grids[axis]=[sum(c)/len(c) for c in clusters]
    for rec in records:
        for p in rec['puntos_svg']:
            for axis in (0,1):
                if grids[axis]:
                    v=min(grids[axis],key=lambda v:abs(p[axis]-v))
                    if abs(p[axis]-v)<.03:p[axis]=v
    return records,reason


def joint_between(top,shared,a,b):
    pairs=shared.get(tuple(sorted((a,b))),[])
    if not pairs:return None
    vs=top['vertices'];longest=max(pairs,key=lambda pair:math.dist(vs[pair[0]],vs[pair[1]]))
    p,q=map(lambda i:Vector(vs[i]),longest);t=(q-p).normalized()
    allpts=[Vector(vs[i]) for pair in pairs for i in pair]
    if any(abs((v-p).cross(t))>.03 for v in allpts):return None
    vals=sorted((min((Vector(vs[i])-p).dot(t) for i in pair),max((Vector(vs[i])-p).dot(t) for i in pair)) for pair in pairs)
    spans=[]
    for lo,hi in vals:
        if spans and lo<=spans[-1][1]+.03:spans[-1][1]=max(hi,spans[-1][1])
        else:spans.append([lo,hi])
    if abs(t.x)<.0001:
        spans=sorted([sorted([(p+t*lo).y,(p+t*hi).y]) for lo,hi in spans]);axis='x';pos=p.x
    elif abs(t.y)<.0001:
        spans=sorted([sorted([(p+t*lo).x,(p+t*hi).x]) for lo,hi in spans]);axis='y';pos=p.y
    else:
        return dict(axis='libre',origin=list(p),tangent=list(t),segments=spans,min=spans[0][0],max=spans[-1][1],pos=0,length=sum(hi-lo for lo,hi in spans))
    return dict(axis=axis,pos=pos,segments=spans,min=spans[0][0],max=spans[-1][1],length=sum(hi-lo for lo,hi in spans))


def scale_topology(top,factor):
    top=json.loads(json.dumps(top))
    top['vertices']=[[v*factor for v in p] for p in top['vertices']]
    for region in top['regiones']:
        region['centro']=[v*factor for v in region['centro']];region['bbox']=[v*factor for v in region['bbox']];region['area_svg']*=factor*factor
    for repair in top['reparaciones_particion']:repair['longitud_mm']*=factor
    return top


_topology=topology
def topology(records):
    pts=[p for r in records for p in r['puntos_svg']];span=max(max(p[j] for p in pts)-min(p[j] for p in pts) for j in (0,1))
    if not any(is_score(r) for r in records) and (span>1500 or span<100):
        factor=span/850;normalized=[dict(r,puntos_svg=[[v/factor for v in p] for p in r['puntos_svg']]) for r in records]
        top,shared=_topology(normalized);return scale_topology(top,factor),shared
    return _topology(records)


def score_component(top,shared,records):
    scoreids={r['id'] for r in records if is_score(r)}
    if not scoreids:return None,{}
    provenance={tuple(sorted(e)):set(names) for e,names in zip(top['aristas'],top['origen_aristas'])}
    graph=defaultdict(set)
    for (a,b),pairs in shared.items():
        if any(provenance.get(tuple(sorted(pair)),set())&scoreids for pair in pairs):graph[a].add(b);graph[b].add(a)
    if not graph:raise ValueError('Hay líneas de hendido de color, pero no delimitan paneles. Revisa los extremos abiertos del SVG.')
    seed=max(graph,key=lambda i:top['regiones'][i]['area_svg']);pending=[seed];seen={seed}
    while pending:
        for nxt in graph[pending.pop()]:
            if nxt not in seen:seen.add(nxt);pending.append(nxt)
    if len(seen)!=len(graph):raise ValueError('El SVG contiene varios desarrollos desconectados. Exporta una caja por SVG.')
    return seen,graph


def recognize_b1(top,shared,records):
    regs={r['id']:r for r in top['regiones']};seen,graph=score_component(top,shared,records)
    # Geometric fallback: four rectangular body panels, eight flaps, glue tab.
    if seen is None:
        candidates=[]
        for r in regs.values():
            x0,y0,x1,y1=r['bbox'];w,h=x1-x0,y1-y0
            if w*h and r['area_svg']/(w*h)>.995:candidates.append(r['id'])
        groups=[]
        for i in candidates:
            box=regs[i]['bbox'];h=box[3]-box[1]
            row=sorted([j for j in candidates if abs((regs[j]['bbox'][1]+regs[j]['bbox'][3])-(box[1]+box[3]))<.1 and abs((regs[j]['bbox'][3]-regs[j]['bbox'][1])-h)<=h*.05],key=lambda j:regs[j]['centro'][0])
            if len(row)==4 and row not in groups:groups.append(row)
        if len(groups)!=1:raise ValueError('No se reconocen las cuatro caras de una caja americana.')
        body=groups[0];graph=defaultdict(set)
        for a,b in shared:
            j=joint_between(top,shared,a,b)
            if j and ((a in body) or (b in body)):
                other=b if a in body else a
                # Reject narrow scrap regions alongside slots.
                if regs[other]['area_svg']>min(regs[i]['area_svg'] for i in body)*.025:graph[a].add(b);graph[b].add(a)
        seen=set(graph)
    body=[]
    for i in seen:
        box=regs[i]['bbox'];w,h=box[2]-box[0],box[3]-box[1]
        if w*h and regs[i]['area_svg']/(w*h)>.995:
            row=[j for j in seen if abs((regs[j]['bbox'][1]+regs[j]['bbox'][3])-(box[1]+box[3]))<.1 and abs((regs[j]['bbox'][3]-regs[j]['bbox'][1])-h)<=h*.05 and regs[j]['area_svg']/max(1e-9,(regs[j]['bbox'][2]-regs[j]['bbox'][0])*(regs[j]['bbox'][3]-regs[j]['bbox'][1]))>.995]
            if len(row)==4:body=sorted(row,key=lambda j:regs[j]['centro'][0]);break
    if len(body)!=4:raise ValueError('No es una caja americana de cuatro caras en fila.')
    widths=[regs[i]['bbox'][2]-regs[i]['bbox'][0] for i in body]
    if any(abs(widths[i]/widths[i+2]-1)>.03 for i in (0,1)):raise ValueError('Las caras opuestas del cuerpo difieren más del 3 %.')
    roles={'Base':body[0]};roles.update({f'Cara_{i+1}':rid for i,rid in enumerate(body[1:],1)})
    names=['Base','Cara_2','Cara_3','Cara_4'];hinges=[]
    def add(child,parent,rid,start,end,**extra):
        roles[child]=rid;j=joint_between(top,shared,roles[parent],rid)
        if not j or j['axis']=='libre':raise ValueError('Gira el troquel B1 para alinear sus caras con X/Y.')
        coord=0 if j['axis']=='x' else 1;side=1 if regs[rid]['centro'][coord]>j['pos'] else -1
        correction=[0.,0.,0.];bend_offset=0.;angle=-90*side
        if 'layer' in extra:
            u=(side,0,0) if j['axis']=='x' else (0,-side,0)
            angle=-90*side
            if extra['layer']==0:bend_offset=-(ESPESOR_MM+.25)
        elif child=='Encolado':bend_offset=-.5*ESPESOR_MM
        if child=='Cara_4':correction=[.7*ESPESOR_MM,0.,0.]
        h=dict(j,child=child,parent=parent,side=side,angle=angle,start=start,end=end,correction=correction,bend_offset=bend_offset,**extra)
        if 'layer' in extra:h['keys']=[[1,0],[42,side*25],[start,side*25],[end,angle]]
        hinges.append(h)
    for i in range(1,4):add(names[i],names[i-1],body[i],50+(i-1)*36,82+(i-1)*36)
    small={0,2} if widths[0]<widths[1] else {1,3};large=[i for i in range(4) if i not in small]
    for i,rid in enumerate(body):
        flaps=[]
        for nxt in graph[rid]-set(body):
            j=joint_between(top,shared,rid,nxt)
            if j and j['axis']=='y':flaps.append(nxt)
            elif j and j['axis']=='x' and i==0:add('Encolado',names[i],nxt,2,40)
        if len(flaps)!=2:raise ValueError('Cada cara de la caja B1 debe tener una solapa superior y otra inferior.')
        for nxt in flaps:
            upper=regs[nxt]['centro'][1]<regs[rid]['centro'][1];prefix='Superior' if upper else 'Inferior'
            offset=350 if upper else 210
            stage=0 if i in small else 1+large.index(i)
            add(f'{prefix}_{i+1}',names[i],nxt,offset+stage*40,offset+stage*40+34,layer=stage)
    if set(roles.values())!=seen:raise ValueError('Hay paneles adicionales: este troquel no corresponde a una B1 simple.')
    min_w=min(widths);by_child={h['child']:h for h in hinges}
    for prefix in ('Superior','Inferior'):
        h_small=[by_child[f'{prefix}_{i+1}'] for i in small];h_large=[by_child[f'{prefix}_{i+1}'] for i in large]
        decalaje=abs(h_large[0]['pos']-h_small[0]['pos'])
        len_a,len_b=[regs[roles[h['child']]]['bbox'][3]-regs[roles[h['child']]]['bbox'][1] for h in h_large]
        overlap=(len_a+len_b)>min_w+.5
        if decalaje>.1 or overlap:
            for h in h_small:h['bend_offset']=0.
            h_large[0]['bend_offset']=max(0.,(ESPESOR_MM+.25)-decalaje)
            h_large[1]['bend_offset']=max(0.,(2 if overlap else 1)*(ESPESOR_MM+.25)-decalaje)
    return roles,hinges


def generic_panels(top,shared,records,plan):
    seen,graph=score_component(top,shared,records)
    if seen is None:raise ValueError('Familia desconocida sin hendidos diferenciados. Marca cortes y hendidos en el SVG.')
    regs={r['id']:r for r in top['regiones']};base=max(seen,key=lambda i:regs[i]['area_svg'])
    if plan.get('base') is not None:
        base=int(plan['base'])
        if base not in seen:raise ValueError('El panel base indicado no pertenece al troquel.')
    roles={'Base':base};queue=[base];parents={base:None};hinges=[]
    while queue:
        rid=queue.pop(0);parent=next(k for k,v in roles.items() if v==rid)
        for nxt in sorted(graph[rid]):
            if nxt==parents[rid]:continue
            if nxt in parents:raise ValueError('La red de hendidos tiene ciclos. Se necesita una estrategia específica para esta estructura.')
            child=f'Panel_{nxt}';roles[child]=nxt;parents[nxt]=rid;queue.append(nxt)
            j=joint_between(top,shared,rid,nxt)
            if not j:raise ValueError('Hay un hendido curvo o varios ejes entre dos paneles: requiere un modelo específico.')
            if j['axis']=='libre':
                p=Vector(j['origin']);t=Vector(j['tangent']);d=Vector(regs[nxt]['centro'])-p
                side=1 if t.cross(d)>0 else -1;angle=-90*side
            else:
                coord=0 if j['axis']=='x' else 1;side=1 if regs[nxt]['centro'][coord]>j['pos'] else -1;angle=-90*side
            hinges.append(dict(j,child=child,parent=parent,side=side,angle=angle,start=2+len(hinges)*40,end=36+len(hinges)*40))
    return roles,hinges


_recognize_estuche=recognize_panels
def recognize_panels(top,shared):
    pts=top['vertices'];span=max(max(p[j] for p in pts)-min(p[j] for p in pts) for j in (0,1));factor=span/850
    roles,hinges=_recognize_estuche(scale_topology(top,1/factor),shared)
    for h in hinges:
        for key in ('pos','min','max','length'):h[key]*=factor
        h['segments']=[[v*factor for v in segment] for segment in h['segments']]
    for h in hinges:
        child=h['child']
        if child.startswith(('Oreja_Frente_','Oreja_Trasera_')):h['start'],h['end']=1,30
        elif child in ('Frente','Trasera'):h['start'],h['end']=33,68
        elif child.startswith('Ala_Tapa_'):h['start'],h['end']=215,245
        elif child.startswith('Oreja_Frontal_Tapa_'):h['start'],h['end']=215,245
        elif child=='Tapa':h['start'],h['end']=250,320
        elif child=='Frontal_Tapa':h['start'],h['end']=330,365
        if child.startswith('Retorno_'):h['inward_clearance_mm']=ESPESOR_MM*.5
        if child.startswith('Ala_Tapa_'):h['inward_clearance_mm']=-ESPESOR_MM*.7
        if child=='Frontal_Tapa':h['inward_clearance_mm']=ESPESOR_MM*.5
    return roles,hinges


def configure_globals(top,roles,hinges):
    global T,C,S,TH,R,H,P,IDS,CX,CY,BR,STEPS,bychild,order,index,mainstates,ROLE_PRIORITY
    T=top;H=hinges;IDS=roles.copy();P={v:k for k,v in roles.items()};S=1.;TH=ESPESOR_MM;R=ANCHO_HENDIDO_MM/2;BR=BANDA_FLEXION_MM/2;STEPS=DIVISIONES_FLEXION
    C={'thickness':TH,'crease_radius':R,'crease_depth':HUNDIMIENTO_INTERIOR_MM,'frame_end':int(max(h['end'] for h in H))+20,'substeps':2}
    base=next(r for r in top['regiones'] if r['id']==roles['Base']);CX,CY=base['centro']
    bychild={h['child']:h for h in H};order=[];remaining=H.copy();done={'Base'}
    while remaining:
        ready=[h for h in remaining if h['parent'] in done]
        if not ready:raise ValueError('Jerarquía circular o incompleta.')
        for h in ready:order.append(h);done.add(h['child']);remaining.remove(h)
    index={h['child']:i for i,h in enumerate(order)};mainstates={fullstate(name):name for name in P.values()}
    ROLE_PRIORITY={name:i for i,name in enumerate(P.values())}


def hinge_geometry(h):
    mid=(h['min']+h['max'])/2
    if h['axis']=='x':return point(h['pos'],mid,TH/2),Vector((h['side'],0,0)),Vector((0,1,0))
    if h['axis']=='y':return point(mid,h['pos'],TH/2),Vector((0,-h['side'],0)),Vector((1,0,0))
    t=Vector(h['tangent']);p=Vector(h['origin'])+t*mid
    return point(*p,TH/2),Vector((-t.y*h['side'],-t.x*h['side'],0)),Vector((t.x,-t.y,0))


def band_radius(h):return BR+abs(h.get('bend_offset',0.))


def relief_radius(thickness,family,top,roles):
    if family!='b1':return thickness*(.65 if family=='estuche' else .25)
    base=next(r for r in top['regiones'] if r['id']==roles['Base']);b=base['bbox'];minimum=min(1.95,min(b[2]-b[0],b[3]-b[1])*.01)
    return max(thickness*.65,minimum)


def cut_clearance(thickness,family,hinges,plan=None):
    if plan and 'holgura_cortes_mm' in plan:return float(plan['holgura_cortes_mm'])
    if family=='b1':return 0.
    if family=='estuche':return thickness*.15
    factor=max(1,min(4,math.tan(max(abs(h['angle']) for h in hinges)*math.pi/360)))
    return thickness*.55*factor


def pose_angles(frame):
    angles=[]
    for h in order:
        keys=h.get('keys',[[1,0],[h['start'],0],[h['end'],h['angle']]])
        value=keys[-1][1]
        for (a,x),(b,y) in zip(keys,keys[1:]):
            if frame<=b:
                t=max(0,min(1,(frame-a)/max(1e-9,b-a)));t=t*t*(3-2*t);value=x+(y-x)*t;break
        angles.append(math.radians(value))
    return angles


def planar_mesh():
    kept=[];members=defaultdict(set)
    for region in T['regiones']:
        k=region['id']
        if k not in P:continue
        for fi in region['triangulos']:
            face=T['caras'][fi];kept.append((face,k))
            for i in face:members[i].add(k)
    dsu={}
    def find(a):
        dsu.setdefault(a,a)
        if dsu[a]!=a:dsu[a]=find(dsu[a])
        return dsu[a]
    def union(a,b):dsu[find(a)]=find(b)
    for h in H:
        a,b=IDS[h['parent']],IDS[h['child']]
        for i,own in members.items():
            if a in own and b in own:union((i,a),(i,b))
    bm=bmesh.new();verts={}
    for face,k in kept:
        vs=[]
        for i in face:
            key=find((i,k))
            if key not in verts:verts[key]=bm.verts.new(point(*T['vertices'][i]))
            vs.append(verts[key])
        f=bm.faces.new(vs);f.material_index=k
    # Small circular relief at cut/score junctions allows finite-thickness
    # material to close without forcing several solid corners into one point.
    relief=float(PLAN.get('alivio_mm',relief_radius(TH,FAMILIA,T,IDS)))
    centers=[]
    for h in (H if relief>0 else []):
        for lo,hi in hsegments(h):
            for t in (lo,hi):
                if h['axis']=='x':p=point(h['pos'],t)
                elif h['axis']=='y':p=point(t,h['pos'])
                else:p=point(*(Vector(h['origin'])+Vector(h['tangent'])*t))
                if all((p-c).length>relief for c in centers):centers.append(p)
    for center in centers:
        normals=[Vector((math.cos(i*math.tau/12),math.sin(i*math.tau/12),0)) for i in range(12)]
        for normal in normals:
            patch=[f for f in bm.faces if all(min(v.co[j] for v in f.verts)<=center[j]+relief*1.05 and max(v.co[j] for v in f.verts)>=center[j]-relief*1.05 for j in (0,1))]
            es={e for f in patch for e in f.edges};vs={v for f in patch for v in f.verts}
            if patch:bmesh.ops.bisect_plane(bm,geom=patch+list(es)+list(vs),dist=.001,plane_co=center+normal*relief,plane_no=normal,clear_outer=False,clear_inner=False)
        remove=[f for f in bm.faces if all((f.calc_center_median()-center).dot(n)<relief+.001 for n in normals)]
        if remove:bmesh.ops.delete(bm,geom=remove,context='FACES')
    # Clearance is applied only to free cut edges. Score positions in the
    # centers of panels remain at the dimensions specified by the drawing.
    clearance=cut_clearance(TH,FAMILIA,H,PLAN)
    reach=max(TH*12,clearance*6);cuts=[]
    for e in bm.edges:
        if len(e.link_faces)!=1:continue
        face=e.link_faces[0];a,b=[v.co.copy() for v in e.verts];d=b-a
        if d.length<1e-6:continue
        normal=Vector((-d.y,d.x,0)).normalized()
        if normal.dot(face.calc_center_median()-(a+b)/2)<0:normal.negate()
        name=P[face.material_index]
        amount=TH*3 if name=='Encolado' else clearance
        if FAMILIA=='estuche':
            if name.startswith('Oreja_Frente_'):amount=TH*1.8
            elif name.startswith('Oreja_Trasera_'):amount=TH*.9
            elif name.startswith('Retorno_'):amount=TH*1.3
            elif name.startswith('Ala_Tapa_'):amount=TH*1.3
        amount=float(PLAN.get('holguras_por_panel_mm',{}).get(name,amount))
        cuts.append((a,b,normal,face.material_index,amount))
    moves=[]
    for v in bm.verts:
        own={f.material_index for f in v.link_faces};best=reach;normals=[];amounts=[]
        for a,b,normal,panel,amount in cuts:
            if panel not in own:continue
            d=b-a;t=max(0,min(1,(v.co-a).dot(d)/d.length_squared));delta=v.co-a-d*t;dist=delta.length
            if dist<best-.001:best=dist;normals=[normal];amounts=[amount]
            elif abs(dist-best)<.001:normals.append(normal);amounts.append(amount)
        if not normals:moves.append((v,v.co.copy()));continue
        normal=sum(normals,Vector((0,0,0)))
        if normal.length<1e-6:moves.append((v,v.co.copy()));continue
        amount=max(amounts)
        if amount<=1e-6:moves.append((v,v.co.copy()));continue
        normal.normalize();miter=1/max(.5,min(normal.dot(n) for n in normals));weight=max(0,1-best/(6*amount))**2
        moves.append((v,v.co+normal*(amount*miter*weight)))
    for v,co in moves:v.co=co
    for h in H:
        own={IDS[h['parent']],IDS[h['child']]};pivot,u,_=hinge_geometry(h);pivot.z=0
        for i in range(-8,9):
            faces=[f for f in bm.faces if f.material_index in own];es={e for f in faces for e in f.edges};vs={v for f in faces for v in f.verts}
            bmesh.ops.bisect_plane(bm,geom=faces+list(es)+list(vs),dist=.001,plane_co=pivot+u*(h.get('bend_offset',0)+i*BR/8),plane_no=u,clear_outer=False,clear_inner=False)
    # Remove float32 slivers without shifting the surviving boundary vertices.
    for iteration in range(3):
        for v in bm.verts:v.co.x=round(v.co.x,3);v.co.y=round(v.co.y,3)
        bmesh.ops.dissolve_degenerate(bm,dist=.002,edges=list(bm.edges))
    for v in bm.verts:v.co.x=round(v.co.x,3);v.co.y=round(v.co.y,3)
    bmesh.ops.dissolve_degenerate(bm,dist=1e-7,edges=list(bm.edges))
    bmesh.ops.triangulate(bm,faces=list(bm.faces),quad_method='BEAUTY',ngon_method='BEAUTY')
    # CDT/bisect can leave a triangle whose three rounded coordinates lie on
    # the same score line. Collapse its shortest edge before adding thickness.
    for iteration in range(100):
        bad=None
        for f in bm.faces:
            if len(f.verts)!=3:continue
            a,b,c=[v.co for v in f.verts]
            if abs(float(b.x-a.x)*float(c.y-a.y)-float(b.y-a.y)*float(c.x-a.x))<1e-10:bad=f;break
        if bad is None:break
        edge=min(bad.edges,key=lambda e:e.calc_length());bmesh.ops.collapse(bm,edges=[edge],uvs=False)
    bmesh.ops.triangulate(bm,faces=[f for f in bm.faces if len(f.verts)>3],quad_method='BEAUTY',ngon_method='BEAUTY')
    isolated=[v for v in bm.verts if not v.link_faces]
    if isolated:bmesh.ops.delete(bm,geom=isolated,context='VERTS')
    bm.verts.index_update();bm.faces.index_update();bm.normal_update()
    for f in bm.faces:
        if f.normal.z<0:f.normal_flip()
    points=[v.co.copy() for v in bm.verts];owners=[{f.material_index for f in v.link_faces} for v in bm.verts]
    faces=[([v.index for v in f.verts],f.material_index) for f in bm.faces]
    boundaries=[(e.verts[0].index,e.verts[1].index,e.link_faces[0].material_index) for e in bm.edges if len(e.link_faces)==1]
    if any(len(e.link_faces)>2 for e in bm.edges):raise RuntimeError('Superficie no manifold.')
    bm.free();return points,owners,faces,boundaries


def states_for_vertex(p,owners):
    x,y=p.x+CX,CY-p.y;owner=min((P[k] for k in owners),key=lambda name:ROLE_PRIORITY[name])
    fractions={i:v/STEPS for i,v in dict(fullstate(owner)).items()};active={'Base'}|{order[i]['child'] for i in fractions}
    for j,h in enumerate(order):
        if IDS[h['parent']] not in owners and IDS[h['child']] not in owners:continue
        pivot,u,_=hinge_geometry(h);q=(p-pivot).dot(u)
        if h['axis']=='libre':t=(Vector((x,y))-Vector(h['origin'])).dot(Vector(h['tangent']))
        else:t=y if h['axis']=='x' else x
        along=min(max(lo-t,0,t-hi) for lo,hi in hsegments(h))
        radius=band_radius(h)
        if abs(q)<=radius and along<BR:
            f=max(0,min(1,.5+.5*q/radius));fade=1-along/BR;fade=fade*fade*(3-2*fade)
            default=fractions.get(j,0);fractions[j]=default+(f-default)*fade;active.add(h['child'])
    # One exact neutral-curve transform per vertex pair. Interpolating several
    # helper matrices can bulge cut corners and make neighboring solids cross.
    state=tuple(sorted((j,STEPS if f>=1-1e-6 else round(f*STEPS,6)) for j,f in fractions.items() if f>1e-6))
    return {state:1.}


def distance(h,x,y):
    if h['axis']=='libre':
        t=Vector(h['tangent']);d=Vector((x,y))-Vector(h['origin']);q=abs(t.cross(d));along=d.dot(t)
    else:q=abs((x if h['axis']=='x' else y)-h['pos']);along=y if h['axis']=='x' else x
    if h.get('bend_offset'):
        signed=(t.cross(d) if h['axis']=='libre' else (x if h['axis']=='x' else y)-h['pos'])*h['side']
        q=abs(signed-h['bend_offset'])
    return min(math.hypot(q,max(lo-along,0,along-hi)) for lo,hi in hsegments(h))


def bend_matrix(h,f,angle):
    """Continuous neutral layer; all rotations originate at nominal thickness / 2.

    The integral of rotating tangents gives the flexion band. Compression is
    already modeled in the volume, rather than produced by intersecting slabs.
    """
    if not f or abs(angle)<1e-10:return Matrix.Identity(4)
    if FAMILIA=='estuche':return bend_estuche(h,f,angle)
    pivot,u,axis=hinge_geometry(h);radius=band_radius(h);length=min(2*BR,TH*.5*math.pi/2) if FAMILIA=='b1' else 2*BR
    s=2*radius*f;before=radius+h.get('bend_offset',0)-length/2
    flat=min(s,before);arc=max(0,min(length,s-before));after=max(0,s-before-length);phi=angle*arc/length
    start=pivot-u*radius
    dst=start+u*(flat+length*math.sin(phi)/angle+after*math.cos(angle))+axis.cross(u)*(length*(1-math.cos(phi))/angle+after*math.sin(angle))
    correction=Vector(h.get('correction',(0,0,0)))
    progress=min(1,abs(angle)/max(1e-9,abs(math.radians(h['angle']))))
    dst+=correction*f*progress
    src=start+u*s;rot=Matrix.Rotation(phi,4,axis);rot.translation=dst-rot.to_3x3()@src
    return rot


def make_native_rig(obj,points,owners):
    vertex_states=[states_for_vertex(p,o) for p,o in zip(points,owners)];states=set(mainstates)
    for weights in vertex_states:states.update(weights)
    states=sorted(states,key=lambda st:(len(st),st));names={st:mainstates.get(st,f'Flexion_{k:04d}') for k,st in enumerate(states)}
    arm=bpy.data.armatures.new('Esqueleto_Carton');rig=bpy.data.objects.new('Rig_Plegado',arm);bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active=rig;rig.select_set(True);bpy.ops.object.mode_set(mode='EDIT')
    for st in states:
        bone=arm.edit_bones.new(names[st])
        if not st:origin=Vector((0,0,TH/2));axis=Vector((0,1,0))
        else:
            j,n=st[-1];origin,u,axis=hinge_geometry(order[j])
            if st not in mainstates:origin+=u*(band_radius(order[j])*(2*n/STEPS-1))
        bone.head=origin;bone.tail=origin+axis*(35 if st in mainstates else 8);bone.roll=0
    parentstate={st:next((st[:k] for k in range(len(st)-1,-1,-1) if st[:k] in mainstates),()) for st in states if st}
    for st in states:
        if st:arm.edit_bones[names[st]].parent=arm.edit_bones[names[parentstate[st]]]
    bpy.ops.object.mode_set(mode='OBJECT');controls=arm.collections.new(f'Paneles ({len(P)})');helpers=arm.collections.new('Flexion auxiliar');helpers.is_visible=False
    for st in states:
        (controls if st in mainstates else helpers).assign(arm.bones[names[st]]);rig.pose.bones[names[st]].rotation_mode='QUATERNION'
    groups={st:obj.vertex_groups.new(name=names[st]) for st in states};n=len(points)
    for i,weights in enumerate(vertex_states):
        for st,w in weights.items():groups[st].add([i,i+n],w,'REPLACE')
    mod=obj.modifiers.new('Flexion_Carton','ARMATURE');mod.object=rig;mod.use_deform_preserve_volume=True;obj.parent=rig
    rig['plano_medio_nominal_mm']=TH/2;rig['ancho_banda_flexion_mm']=2*BR;rig['divisiones_flexion']=STEPS
    scene=bpy.context.scene;scene.frame_start=1;scene.frame_end=C['frame_end'];scene.render.fps=30
    frames=sorted(set(range(1,C['frame_end']+1,4))|{1,C['frame_end']}|{int(h['start']) for h in H}|{int(h['end']) for h in H}|{int(k[0]) for h in H for k in h.get('keys',[])})
    rest={st:arm.bones[names[st]].matrix_local.copy() for st in states};invrest={st:m.inverted() for st,m in rest.items()}
    tracks={st:[[],[],[],[],[],[],[]] for st in states};lastquat={}
    for frame in frames:
        angles=pose_angles(frame);cache={};poses={}
        for st in states:
            transform=Matrix.Identity(4)
            for j,k in st:
                if (j,k) not in cache:cache[j,k]=bend_matrix(order[j],k/STEPS,angles[j])
                transform=transform@cache[j,k]
            poses[st]=transform@rest[st]
        for st in states:
            if st:
                parent=parentstate[st];basis=invrest[st]@rest[parent]@poses[parent].inverted()@poses[st]
            else:basis=invrest[st]@poses[st]
            loc,quat,_=basis.decompose()
            if st in lastquat and lastquat[st].dot(quat)<0:quat.negate()
            lastquat[st]=quat.copy()
            for track,value in zip(tracks[st],list(loc)+list(quat)):track.extend((frame,value))
    # Bulk F-curves avoid repeated dependency-graph evaluation for every key.
    rig.animation_data_create();action=bpy.data.actions.new('Montaje_Packaging');slot=action.slots.new(id_type='OBJECT',name=rig.name)
    rig.animation_data.action=action;rig.animation_data.action_slot=slot;layer=action.layers.new('Plegado');strip=layer.strips.new(type='KEYFRAME');bag=strip.channelbags.new(slot)
    for st in states:
        name=names[st]
        for k,values in enumerate(tracks[st]):
            prop='location' if k<3 else 'rotation_quaternion';idx=k if k<3 else k-3
            path=f'pose.bones["{name}"].{prop}';fc=bag.fcurves.new(data_path=path,index=idx)
            fc.keyframe_points.add(len(frames));fc.keyframe_points.foreach_set('co',values)
            for kp in fc.keyframe_points:kp.interpolation='LINEAR'
            fc.update()
    print('RIG:',len(P),'paneles,',len(states),'huesos,',len(frames),'poses.',flush=True)
    return rig


def audit_mesh(obj,rig):
    bm=bmesh.new();bm.from_mesh(obj.data);n=len(obj.data.vertices)//2
    report={'blender':bpy.app.version_string,'vertices':len(bm.verts),'caras':len(bm.faces),'aristas_no_manifold':sum(not e.is_manifold for e in bm.edges),'caras_degeneradas':sum(f.calc_area()<1e-9 for f in bm.faces),'volumen_plano_mm3':bm.calc_volume(signed=True),'huesos':len(rig.data.bones),'huesos_principales':len(P),'plano_medio_mm':TH/2,'error_altura_huesos_reposo_mm':max(abs(b.head_local.z-TH/2) for b in rig.data.bones),'vertices_sin_peso':sum(not v.groups for v in obj.data.vertices),'error_suma_pesos':max(abs(sum(g.weight for g in v.groups)-1) for v in obj.data.vertices),'materiales':[m.name for m in obj.data.materials],'caras_por_slot':dict(Counter(p.material_index for p in obj.data.polygons)),'hendido_interior_mm':max(TH-v.co.z for v in obj.data.vertices[n:]),'hendido_exterior_mm':max(v.co.z for v in obj.data.vertices[:n]),'espesor_minimo_plano_mm':min(obj.data.vertices[i+n].co.z-obj.data.vertices[i].co.z for i in range(n))}
    bm.free()
    if any(report[k] for k in ('aristas_no_manifold','caras_degeneradas','vertices_sin_peso')):raise RuntimeError('La malla no supera la comprobación topológica: '+str(report))
    if report['error_altura_huesos_reposo_mm']>1e-5 or report['error_suma_pesos']>1e-5:raise RuntimeError('Error en ejes o pesos de la armadura.')
    if report['espesor_minimo_plano_mm']<=0:raise RuntimeError('El hendido agota el espesor del cartón.')
    if len(report['materiales'])!=3 or any(report['caras_por_slot'].get(i,0)==0 for i in range(3)):raise RuntimeError('Falta uno de los tres materiales.')
    return report


def write_json(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')


def rotated_records(records,angle):
    c,s=math.cos(angle),math.sin(angle)
    return [dict(r,puntos_svg=[[c*x-s*y,s*x+c*y] for x,y in r['puntos_svg']]) for r in records]


def normalize_orientation(records):
    refs=[r for r in records if is_score(r)] or records
    segments=[(a,b) for r in refs for a,b in zip(r['puntos_svg'],r['puntos_svg'][1:])]
    a,b=max(segments,key=lambda ab:math.dist(*ab));angle=math.atan2(b[1]-a[1],b[0]-a[0]);angle=(angle+math.pi/4)%(math.pi/2)-math.pi/4
    return (rotated_records(records,-angle),-angle) if abs(angle)>1e-6 else (records,0.)


def diagnostics(records,hinges,path,top=None,roles=None):
    pts=[p for r in records for p in r['puntos_svg']];x0=min(p[0] for p in pts);y0=min(p[1] for p in pts);w=max(p[0] for p in pts)-x0;h=max(p[1] for p in pts)-y0
    lines=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" viewBox="{x0} {y0} {w} {h}"><g fill="none" stroke-width=".3">']
    for rec in records:
        for a,b in zip(rec['puntos_svg'],rec['puntos_svg'][1:]):
            fold=any(max(distance(dict(h,bend_offset=0),*a),distance(dict(h,bend_offset=0),*b))<.04 for h in hinges);color='#e6007e' if fold else '#111111'
            lines.append(f'<path stroke="{color}" d="M{a[0]},{a[1]} L{b[0]},{b[1]}"/>')
    lines.append('</g>')
    if top and roles:
        from xml.sax.saxutils import escape
        labels={v:k for k,v in roles.items()}
        for region in top['regiones']:
            if region['id'] in labels:
                x,y=region['centro'];label=escape(f"{labels[region['id']]} ({region['id']})")
                lines.append(f'<text x="{x}" y="{y}" text-anchor="middle" fill="#2255aa" font-size="{max(w,h)*.009}">{label}</text>')
    lines.append('</svg>');Path(path).write_text('\n'.join(lines),encoding='utf8')


def annotate_scene(obj,rig,report):
    scene=bpy.context.scene;scene.render.engine='CYCLES';scene.unit_settings.system='METRIC';scene.unit_settings.length_unit='MILLIMETERS';scene.unit_settings.scale_length=.001
    scene.timeline_markers.clear();scene.timeline_markers.new('Desarrollo plano',frame=1)
    for h in H:scene.timeline_markers.new(h['child'],frame=h['start'])
    scene.timeline_markers.new('Packaging formado',frame=max(h['end'] for h in H))
    scene['familia']=report['familia'];scene['svg_exterior']=True
    scene['estado_colisiones']=json.dumps(report.get('colisiones',{}),ensure_ascii=False)
    for name,text in [('LEEME',f"Cartón {TH:g} mm. Exterior blanco; interior marrón; canto independiente.\nHuesos en plano medio nominal {TH/2:g} mm.\nFrame 1 plano; frame {C['frame_end']} formado.\nInforme y configuración en baul.\n"),('MOTOR_CAJA.py',Path(__file__).read_text(encoding='utf8'))]:
        block=bpy.data.texts.get(name) or bpy.data.texts.new(name);block.clear();block.write(text)
    bpy.ops.object.select_all(action='DESELECT');obj.select_set(True);bpy.context.view_layer.objects.active=obj;rig.show_in_front=False
    if report['familia']=='b1':rig.rotation_euler.x=math.pi/2
    scene.frame_set(C['frame_end']);bpy.context.view_layer.update()
    deps=bpy.context.evaluated_depsgraph_get();evaluated=obj.evaluated_get(deps);mesh=evaluated.to_mesh();coords=[evaluated.matrix_world@v.co for v in mesh.vertices]
    lo=Vector(tuple(min(v[j] for v in coords) for j in range(3)));hi=Vector(tuple(max(v[j] for v in coords) for j in range(3)));evaluated.to_mesh_clear()
    rig.location.z-=lo.z;hi.z-=lo.z;lo.z=0
    bpy.context.view_layer.update();center=(hi+lo)/2;size=max(hi-lo)
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type=='VIEW_3D':
                space=area.spaces.active;space.shading.type='SOLID';space.shading.color_type='MATERIAL';space.clip_start=.1;space.clip_end=max(10000,size*10)
                space.region_3d.view_distance=size*2;space.region_3d.view_location=center;space.region_3d.view_rotation=Quaternion((.88,.32,.14,.31)).normalized()
    report['bbox_formado_mm']=[list(lo),list(hi)]


def arguments():
    argv=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else sys.argv[1:]
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--svg',type=Path);ap.add_argument('--grosor',type=float);ap.add_argument('--salida','--output',type=Path)
    ap.add_argument('--units',choices=['auto','mm','pt','px'],default='auto');ap.add_argument('--blender',type=Path)
    ap.add_argument('--familia',choices=['auto','b1','estuche','generico'],default='auto');ap.add_argument('--plan',type=Path)
    ap.add_argument('--solo-analizar',action='store_true');ap.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def blender_program(explicit):
    if explicit:
        if not explicit.is_file():raise FileNotFoundError(explicit)
        return explicit
    found=shutil.which('blender')
    if found:return Path(found)
    base=Path(os.environ.get('PROGRAMFILES',r'C:\Program Files'))/'Blender Foundation';paths=list(base.glob('Blender */blender.exe'))
    if paths:return max(paths,key=lambda p:tuple(map(int,re.findall(r'\d+',p.parent.name))))
    raise RuntimeError('No se encuentra Blender. Indica --blender "ruta/blender.exe".')


def worker(args):
    global ESPESOR_MM,ANCHO_HENDIDO_MM,HUNDIMIENTO_INTERIOR_MM,HUNDIMIENTO_EXTERIOR_MM,BANDA_FLEXION_MM,R,FAMILIA,PLAN
    if bpy.app.version<(5,2,0):raise RuntimeError('Necesitas Blender 5.2 o posterior.')
    ESPESOR_MM=args.grosor;ANCHO_HENDIDO_MM=1.6*ESPESOR_MM;BANDA_FLEXION_MM=2*ESPESOR_MM
    HUNDIMIENTO_INTERIOR_MM=.35*ESPESOR_MM;HUNDIMIENTO_EXTERIOR_MM=.14*ESPESOR_MM
    records,reason=read_svg(args.svg,args.units);records,rotation=normalize_orientation(records);top,shared=topology(records);plan=json.loads(args.plan.read_text(encoding='utf8')) if args.plan else {}
    PLAN=plan
    for key in ('holgura_cortes_mm','alivio_mm'):
        if key in plan and (not math.isfinite(float(plan[key])) or not 0<=float(plan[key])<=args.grosor*10):raise ValueError('Tolerancia fuera de rango: '+key)
    if any(not math.isfinite(float(v)) or not 0<=float(v)<=args.grosor*10 for v in plan.get('holguras_por_panel_mm',{}).values()):raise ValueError('Holgura de panel fuera de rango.')
    errors={};roles=hinges=None;family=None
    original_records,original_top,original_shared=records,top,shared
    for name in ('b1','estuche','generico'):
        if args.familia not in ('auto',name):continue
        for turn in (range(4) if name!='generico' else range(1)):
            trial_records=rotated_records(original_records,turn*math.pi/2) if turn else original_records
            trial_top,trial_shared=topology(trial_records) if turn else (original_top,original_shared)
            try:
                if name=='b1':roles,hinges=recognize_b1(trial_top,trial_shared,trial_records)
                elif name=='estuche':roles,hinges=recognize_panels(trial_top,trial_shared)
                else:roles,hinges=generic_panels(trial_top,trial_shared,trial_records,plan)
                family=name;records,top,shared=trial_records,trial_top,trial_shared;rotation+=turn*math.pi/2;break
            except ValueError as exc:errors.setdefault(name,str(exc))
        if roles is not None:break
    if roles is None:raise ValueError('No se puede reconstruir este troquel. '+json.dumps(errors,ensure_ascii=False))
    FAMILIA=family
    base=next(r for r in top['regiones'] if r['id']==roles['Base']);box=base['bbox']
    if args.grosor*4>=min(box[2]-box[0],box[3]-box[1]):raise ValueError('El grosor es demasiado grande respecto a las dimensiones del panel principal.')
    for h in hinges:
        override=plan.get('pliegues',{}).get(h['child'],{})
        for key in ('angle','start','end','correction'):
            if key in override:h[key]=override[key]
        if any(key in override for key in ('angle','start','end')):h.pop('keys',None)
        if not all(math.isfinite(float(h[k])) for k in ('angle','start','end')) or abs(h['angle'])>180 or h['start']<1 or h['end']<=h['start']:raise ValueError('Plan de plegado inválido: '+h['child'])
    analysis={'svg':str(args.svg),'sha256_svg':hashlib.sha256(args.svg.read_bytes()).hexdigest(),'unidades':reason,'grosor_mm':args.grosor,'familia':family,'paneles':roles,'regiones':[{k:r[k] for k in ('id','area_svg','centro','bbox')} for r in top['regiones'] if r['id'] in roles.values()],'articulaciones':hinges,'reparaciones_particion':top['reparaciones_particion']}
    analysis['sha256_generador']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    analysis['rotacion_lectura_grados']=math.degrees(rotation)
    analysis['alivio_extremos_hendido_mm']=float(plan.get('alivio_mm',relief_radius(args.grosor,family,top,roles)))
    analysis['holgura_bordes_corte_mm']=cut_clearance(args.grosor,family,hinges,plan)
    analysis['holguras_por_panel_mm']=plan.get('holguras_por_panel_mm',{})
    if family=='b1':analysis['holgura_bordes_encolado_mm']=args.grosor*3
    if family=='estuche':analysis['holguras_paneles_interiores_mm']={'orejas_delanteras':args.grosor*1.8,'orejas_traseras':args.grosor*.9,'retornos':args.grosor*1.3,'alas_tapa':args.grosor*1.3,'separacion_retornos':args.grosor*.5,'separacion_alas':args.grosor*.7,'separacion_frontal':args.grosor*.5}
    diagnostics(records,hinges,args.run_dir/'cortes_hendidos.svg',top,roles);write_json(args.run_dir/'analisis.json',analysis)
    print('Familia:',family,';',len(roles),'paneles.',reason,flush=True)
    if args.solo_analizar:return
    if family=='generico' and not plan.get('confirmado'):
        write_json(args.run_dir/'necesita_plan.json',analysis);return
    for o in list(bpy.data.objects):bpy.data.objects.remove(o,do_unlink=True)
    configure_globals(top,roles,hinges)
    R=BR;points,owners,faces,boundaries=planar_mesh();R=ANCHO_HENDIDO_MM/2
    obj=make_volume(points,owners,faces,boundaries);obj.name='Packaging_Carton';obj.data.name='Carton_Hendidos_Reales'
    material_slots_and_double_crease(obj,exterior_depth=HUNDIMIENTO_EXTERIOR_MM)
    rig=make_native_rig(obj,points,owners);report=audit_mesh(obj,rig);report.update(analysis)
    report['colisiones']=audit_animation(obj,args.run_dir)
    annotate_scene(obj,rig,report)
    report['svg_original_intacto']=hashlib.sha256(args.svg.read_bytes()).hexdigest()==report['sha256_svg']
    write_json(args.run_dir/'informe.json',report)
    bpy.context.preferences.filepaths.save_version=0
    if hasattr(bpy.context.preferences.filepaths,'file_preview_type'):
        try:bpy.context.preferences.filepaths.file_preview_type='NONE'
        except (ValueError,TypeError):pass
    output=args.salida
    if report['colisiones']['pares_detectados']:
        output=args.run_dir/'REVISAR_colisiones.blend'
        print('El montaje necesita ajuste. Se conserva el candidato y el informe en',args.run_dir,flush=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output),compress=True)
    if output!=args.salida:raise RuntimeError('Se detectan intersecciones durante el plegado. No se entrega como terminado. Consulta baul.')
    print('RESULTADO_BLEND='+str(output),flush=True)


def triangle_crosses(a,b,eps=1e-6):
    """Interior segment/triangle intersections; shared edges and contact are allowed."""
    na=(a[1]-a[0]).cross(a[2]-a[0]);nb=(b[1]-b[0]).cross(b[2]-b[0])
    if min(na.length,nb.length)<1e-8:return False
    na.normalize();nb.normalize()
    if abs(na.dot(nb))>.9999 and max(abs((p-a[0]).dot(na)) for p in b)<.02:return False
    def hit(p,q,tri):
        v0,v1,v2=tri;d=q-p;e1=v1-v0;e2=v2-v0;normal=e1.cross(e2)
        if normal.length<1e-8:return False
        normal.normalize();da=(p-v0).dot(normal);db=(q-v0).dot(normal)
        if da*db>=0 or min(abs(da),abs(db))<.002:return False
        h=d.cross(e2);det=e1.dot(h)
        if abs(det)<eps:return False
        inv=1/det;s=p-v0;u=inv*s.dot(h)
        if u<=eps or u>=1-eps:return False
        z=s.cross(e1);v=inv*d.dot(z)
        if v<=eps or u+v>=1-eps:return False
        t=inv*e2.dot(z)
        return p+d*t if eps<t<1-eps else None
    for tri,other in ((a,b),(b,a)):
        for i in range(3):
            point=hit(tri[i],tri[(i+1)%3],other)
            if point is not None and point is not False:return point
    return None


def audit_animation(obj,folder):
    from mathutils.bvhtree import BVHTree
    scene=bpy.context.scene;scene.frame_set(1);bpy.context.view_layer.update()
    rest=[v.co.copy() for v in obj.data.vertices];adj={tuple(sorted((IDS[h['parent']],IDS[h['child']]))):h for h in H}
    steps=C.get('substeps',1)
    report={'metodo':'BVH y cruces de superficies entre paneles no vecinos; las bandas compartidas forman un único sólido. Se permiten contactos y se discrimina contacto numérico mediante una sonda interior de 0.04 mm. Muestreo de la animación; no certifica colisión continua ni contención completa sin cruce de superficies.','subdivisiones_por_frame':steps,'fotogramas':[],'pares_detectados':[]}
    aggregate={}
    for tick in range((C['frame_end']-1)*steps+1):
        frame=1+tick/steps
        scene.frame_set(int(frame),subframe=frame-int(frame));bpy.context.view_layer.update();dg=bpy.context.evaluated_depsgraph_get();ev=obj.evaluated_get(dg);me=ev.to_mesh();me.calc_loop_triangles()
        attr=me.attributes['panel_svg'];coords=[v.co.copy() for v in me.vertices];panels=defaultdict(list)
        for tri in me.loop_triangles:panels[attr.data[tri.polygon_index].value].append(tuple(tri.vertices))
        data={}
        for k,tris in panels.items():
            ids=sorted({i for tri in tris for i in tri});mapping={v:i for i,v in enumerate(ids)};vs=[coords[i] for i in ids]
            bbox=[tuple(min(v[j] for v in vs) for j in range(3)),tuple(max(v[j] for v in vs) for j in range(3))]
            data[k]=(BVHTree.FromPolygons(vs,[[mapping[i] for i in tri] for tri in tris],all_triangles=True,epsilon=0),tris,bbox)
        found=[]
        for a,b in itertools.combinations(sorted(data),2):
            if (a,b) in adj:continue
            ta,fa,ba=data[a];tb,fb,bb=data[b]
            if any(ba[1][j]<bb[0][j]-1e-5 or bb[1][j]<ba[0][j]-1e-5 for j in range(3)):continue
            witness=None
            for ia,ib in ta.overlap(tb):
                va,vb=fa[ia],fb[ib]
                if set(va)&set(vb):continue
                ca,cb=[coords[i] for i in va],[coords[i] for i in vb]
                crossing=triangle_crosses(ca,cb)
                if crossing is not None and crossing is not False:
                    if max(abs((coords[i]-coords[j]).length-(rest[i]-rest[j]).length) for i in va for j in vb)<.02:continue
                    na=(ca[1]-ca[0]).cross(ca[2]-ca[0]).normalized();nb=(cb[1]-cb[0]).cross(cb[2]-cb[0]).normalized();direction=na+nb
                    if direction.length<1e-5:continue
                    # Distinguish a crossing solid from numerical contact at a cut.
                    probe=crossing-direction.normalized()*.04
                    qa=ta.find_nearest(probe);qb=tb.find_nearest(probe)
                    if qa[0] is not None and qb[0] is not None and (probe-qa[0]).dot(qa[1])<-.005 and (probe-qb[0]).dot(qb[1])<-.005:
                        witness=[ia,ib];break
            if witness:
                key=(a,b);found.append([P[a],P[b]])
                if key not in aggregate:aggregate[key]={'paneles':[P[a],P[b]],'primer_frame':frame,'ultimo_frame':frame,'fotogramas':0,'testigo_triangulos':witness}
                aggregate[key]['ultimo_frame']=frame;aggregate[key]['fotogramas']+=1
        ev.to_mesh_clear()
        if found:report['fotogramas'].append({'frame':frame,'pares':found})
        if frame%50==0:print('Colisiones: comprobado frame',frame,'/',C['frame_end'],flush=True)
    report['pares_detectados']=list(aggregate.values());report['fotogramas_comprobados']=C['frame_end']
    report['muestras_comprobadas']=(C['frame_end']-1)*steps+1
    write_json(folder/'colisiones.json',report);return report


def ask_number(prompt,default):
    while True:
        try:
            raw=input(prompt+f' [{default:g}]: ').strip().replace(',','.');v=float(raw) if raw else default
            if math.isfinite(v):return v
        except ValueError:pass
        print('Introduce un número válido.')


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf8',errors='replace')
    if hasattr(sys.stdin,'reconfigure'):sys.stdin.reconfigure(encoding='utf8')
    args=arguments();BAUL.mkdir(exist_ok=True)
    if not args.svg:args.svg=Path(input('Ruta del archivo SVG (puedes arrastrarlo aquí): ').strip().strip('& ').strip('\"\''))
    args.svg=args.svg.expanduser().resolve()
    if not args.svg.is_file() or args.svg.suffix.lower()!='.svg':raise ValueError('Introduce un archivo .svg existente.')
    if args.grosor is None:args.grosor=ask_number('Grosor del cartón en mm',3.)
    if not math.isfinite(args.grosor) or args.grosor<=0:raise ValueError('El grosor debe ser positivo.')
    if not args.salida:args.salida=ROOT/(args.svg.stem+'_formada.blend')
    args.salida=args.salida.expanduser().resolve()
    if args.salida.suffix.lower()!='.blend':raise ValueError('La salida debe terminar en .blend.')
    args.salida=unique_path(args.salida)
    if bpy is not None:
        args.run_dir=Path(os.environ.get('MOTOR_CAJA_RUN_DIR',str(BAUL/'blender_directo')));args.run_dir.mkdir(parents=True,exist_ok=True);worker(args);return
    run=unique_path(BAUL/(datetime.datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+args.svg.stem));run.mkdir(exist_ok=True)
    program=blender_program(args.blender)
    snapshot=run/'motor_ejecutado.py';snapshot.write_bytes(Path(__file__).read_bytes())
    cmd=[str(program),'--background','--factory-startup','--python-exit-code','1','--python',str(snapshot),'--','--worker','--svg',str(args.svg),'--grosor',str(args.grosor),'--salida',str(args.salida),'--units',args.units,'--familia',args.familia]
    if args.plan:cmd+=['--plan',str(args.plan.resolve())]
    if args.solo_analizar:cmd+=['--solo-analizar']
    env=os.environ.copy();env['MOTOR_CAJA_RUN_DIR']=str(run);env['MOTOR_CAJA_ROOT']=str(ROOT);env['PYTHONDONTWRITEBYTECODE']='1'
    while True:
        with (run/'ejecucion.log').open('a',encoding='utf8') as log:
            proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf8',errors='replace',env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            for line in proc.stdout:print(line.rstrip(),flush=True);log.write(line);log.flush()
            code=proc.wait()
        if code:
            report_file=run/'informe.json'
            if report_file.exists() and sys.stdin.isatty():
                report=json.loads(report_file.read_text(encoding='utf8'));collisions=report.get('colisiones',{}).get('pares_detectados',[])
                if collisions:
                    print('\nHay intersecciones que requieren ajustar el montaje. Candidato:',run/'REVISAR_colisiones.blend')
                    for collision in collisions:print(' / '.join(collision['paneles']),': frames',collision['primer_frame'],'a',collision['ultimo_frame'])
                    try:answer=input('¿Ajustar ángulos y tiempos en terminal? (s/N): ').strip().lower()
                    except EOFError:answer='n'
                    if answer=='s':write_json(run/'necesita_plan.json',report);code=0
            if code:raise RuntimeError(f'Blender no terminó correctamente. Consulta {run / "ejecucion.log"}')
        request=run/'necesita_plan.json'
        if not request.exists():break
        data=json.loads(request.read_text(encoding='utf8'));print('\nTroquel nuevo. Define sus ángulos y orden de montaje. Ángulos con signo; conserva el signo sugerido para plegar hacia el interior.\nDiagnóstico:',run/'cortes_hendidos.svg')
        plan={'confirmado':True,'base':data['paneles']['Base'],'pliegues':{}}
        plan['holgura_cortes_mm']=ask_number('Holgura en bordes libres (mm)',data.get('holgura_bordes_corte_mm',args.grosor*.55))
        plan['alivio_mm']=ask_number('Radio de alivio en extremos de hendidos (mm)',data.get('alivio_extremos_hendido_mm',args.grosor*.25))
        if data['familia']=='generico':
            base=int(ask_number('ID del panel base (ver etiquetas del SVG)',data['paneles']['Base']))
            if base!=data['paneles']['Base']:
                file=run/'plan_base.json';write_json(file,{'base':base,'confirmado':False});request.unlink()
                if '--plan' in cmd:i=cmd.index('--plan');del cmd[i:i+2]
                cmd+=['--plan',str(file)];continue
        for h in data['articulaciones']:
            print(h['parent'],'->',h['child'])
            angle=ask_number('Ángulo final (grados)',h['angle']);start=ask_number('Fotograma inicial',h['start']);end=ask_number('Fotograma final',max(start+34,h['end']))
            plan['pliegues'][h['child']]={'angle':angle,'start':start,'end':end}
        file=run/'plan.json';write_json(file,plan);request.unlink()
        if '--plan' in cmd:i=cmd.index('--plan');del cmd[i:i+2]
        cmd+=['--plan',str(file)]
    if args.solo_analizar:print('Análisis guardado en',run)
    elif args.salida.is_file():print('\nPackaging formado:',args.salida,'\nAuxiliares:',run)
    else:raise RuntimeError('No se ha generado el .blend esperado.')


if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt) as exc:
        print('\nERROR:',str(exc),flush=True)
        if bpy is not None:traceback.print_exc()
        sys.exit(1)

