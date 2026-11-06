# -*- coding: utf-8 -*-
"""
CREAR ESTUCHE DESDE SVG — archivo independiente, Blender 5.2+

Uso sencillo: ejecutar este .py con Python. Pide un SVG y guarda un .blend en
la subcarpeta salidas junto a este script. Busca Blender instalado automáticamente.
También puede ejecutarse desde el editor de texto de Blender (Run Script):
abre el selector SVG y genera en un proceso aparte, sin borrar la escena actual.

Uso por consola:
  python crear_estuche_desde_svg.py --svg "troquel.svg" --units pt --output "resultado.blend"
  blender --background --factory-startup --python-exit-code 1 --python crear_estuche_desde_svg.py -- --worker --svg "troquel.svg" --units pt --output "resultado.blend"

Familia admitida: estuche de 19 paneles con tapa, paredes laterales dobles,
cuatro ranuras de encaje y orejas interiores; frontal grande de tapa exterior.
Reconoce geometría, no nombres, IDs ni orden de trazados. Conserva dimensiones
y cortes del SVG. Se detiene si no reconoce la estructura; no es universal.
Sin tamaño físico declarado, el selector permite elegir pt/mm/px. El perfil
automático por consola usa puntos de Illustrator como el original Castelus.

Cartón 3 mm. Hendido 4.8 mm: interior 1.05 mm, exterior 0.42 mm.
Tres materiales: exterior blanco, interior marrón y canto cartón marrón.
Sin renders ni instalaciones pip. Solo Python estándar + Blender instalado.
LIMITACIÓN HEREDADA: el montaje conserva cruces conocidos del prototipo.
No certifica una animación libre de intersecciones.
"""
try:
    import bpy, bmesh
    from mathutils import Vector, Matrix, Quaternion
except ImportError:
    bpy=None


"""Parte de desarrollo de la herramienta; se integra en el unico script entregable."""
import argparse, datetime, hashlib, itertools, json, math, os, re, shutil, subprocess, sys, traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict, Counter

ESPESOR_MM=3.0
ANCHO_HENDIDO_MM=4.8
HUNDIMIENTO_INTERIOR_MM=1.05
HUNDIMIENTO_EXTERIOR_MM=0.42
BANDA_FLEXION_MM=6.0
DIVISIONES_FLEXION=16
LIMITACION_ANIMACION='Prototipo de montaje: persisten intersecciones en esquinas y solapas; no certificado libre de colisiones.'

def parse_arguments():
    argv=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else sys.argv[1:]
    ap=argparse.ArgumentParser(description='SVG de estuche de 19 paneles a Blender, con cartón 3 mm y hendido por ambas caras.')
    ap.add_argument('--svg',type=Path)
    ap.add_argument('--output',type=Path,help='Archivo .blend de salida. Si existe se añade un número; nunca se sobrescribe.')
    ap.add_argument('--units',choices=['auto','pt','mm','px'],default='auto',help='Para SVG sin tamaño físico: auto usa puntos de Illustrator. 1 px = 25.4/96 mm.')
    ap.add_argument('--blender',type=Path,help='Ruta opcional a blender.exe al ejecutar con Python normal.')
    ap.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    ap.add_argument('--analyze-only',action='store_true')
    ap.add_argument('--update-current',action='store_true',help=argparse.SUPPRESS)
    return ap.parse_args(argv)

def unique_path(path):
    path=Path(path).resolve();candidate=path;n=2
    while candidate.exists():candidate=path.with_name(f'{path.stem}_{n:02d}{path.suffix}');n+=1
    candidate.parent.mkdir(parents=True,exist_ok=True)
    return candidate

def blender_program(explicit=None):
    if explicit and explicit.is_file():return explicit
    found=shutil.which('blender')
    if found:return Path(found)
    base=Path(os.environ.get('ProgramFiles',r'C:\Program Files'))/'Blender Foundation'
    candidates=list(base.glob('Blender */blender.exe')) if base.exists() else []
    def version(path):return tuple(int(x) for x in re.findall(r'\d+',path.parent.name))
    if candidates:return max(candidates,key=version)
    raise RuntimeError('No se encuentra Blender. Instala Blender 5.2 o indica --blender con la ruta de blender.exe.')

def launch_with_python(args):
    import tkinter as tk
    from tkinter import filedialog,messagebox,ttk
    root=None
    if args.svg is None:
        root=tk.Tk();root.withdraw()
        selected=filedialog.askopenfilename(parent=root,title='Selecciona el troquel SVG del estuche',filetypes=[('Troquel SVG','*.svg')])
        if not selected:root.destroy();return
        args.svg=Path(selected)
        xml=ET.parse(args.svg).getroot()
        if not re.search(r'(mm|cm|in|pt)\s*$',xml.get('width','')):
            root.deiconify();root.title('Unidades del SVG');root.resizable(False,False)
            ttk.Label(root,text='El SVG no declara tamaño físico. Selecciona sus unidades:',padding=15).pack()
            choice=tk.StringVar(value='pt')
            for label,value in [('Puntos de Illustrator (como el SVG Castelus)','pt'),('Milímetros: 1 unidad del SVG = 1 mm','mm'),('Píxeles SVG a 96 ppp','px')]:ttk.Radiobutton(root,text=label,variable=choice,value=value).pack(anchor='w',padx=20,pady=4)
            accepted=[]
            def accept():accepted.append(True);root.quit()
            ttk.Button(root,text='Crear archivo Blender',command=accept).pack(pady=15)
            root.protocol('WM_DELETE_WINDOW',root.quit);root.mainloop()
            if not accepted:root.destroy();return
            args.units=choice.get();root.withdraw()
    args.svg=args.svg.expanduser().resolve()
    if not args.svg.is_file():raise FileNotFoundError(args.svg)
    if args.output is None:
        folder=Path(__file__).resolve().parent/'salidas'
        args.output=folder/f'{args.svg.stem}_estuche.blend'
    args.output=unique_path(args.output)
    cmd=[str(blender_program(args.blender)),'--background','--factory-startup','--python-exit-code','1','--python',str(Path(__file__).resolve()),'--','--worker','--svg',str(args.svg),'--output',str(args.output),'--units',args.units]
    if args.analyze_only:cmd.append('--analyze-only')
    log=args.output.with_suffix('.log')
    if root:
        for child in root.winfo_children():child.destroy()
        root.deiconify();root.title('Creando estuche en Blender');ttk.Label(root,text='Procesando el SVG. Puede tardar aproximadamente un minuto.',padding=15).pack();root.update()
        stream=log.open('w',encoding='utf-8');proc=subprocess.Popen(cmd,stdout=stream,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        def poll():
            code=proc.poll()
            if code is None:root.after(300,poll);return
            stream.close();root.withdraw()
            if code==0:messagebox.showinfo('Estuche creado',f'Archivo guardado:\n{args.output}\n\nTres materiales y hendido por ambas caras.\n{LIMITACION_ANIMACION}',parent=root)
            else:messagebox.showerror('No se ha podido crear el estuche',f'Consulta el registro:\n{log}\n\nEl SVG debe pertenecer a la misma familia de 19 paneles.',parent=root)
            root.destroy()
        root.after(300,poll);root.mainloop()
    else:
        result=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',errors='replace')
        log.write_text(result.stdout+'\n'+result.stderr,encoding='utf-8');print(result.stdout)
        if result.returncode:raise RuntimeError(f'Blender terminó con error. Ver {log}')
        print('Archivo creado:',args.output)

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
                for name in selector.split(','):css[name.strip()]={k.strip():v.strip() for k,v in re.findall(r'([\w-]+)\s*:\s*([^;]+)',body)}
    def visit(el,matrix,style):
        tag=el.tag.split('}')[-1]
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
                if segment_distance(end,c,d)<.008:connected=True;break
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

def distance(h,x,y):
    q=x if h['axis']=='x' else y; t=y if h['axis']=='x' else x
    return min(math.hypot(q-h['pos'],max(a-t,0,t-b))*S for a,b in hsegments(h))

def planar_mesh():
    kept=[]; members=defaultdict(set)
    for region in T['regiones']:
        k=region['id']
        if k not in P: continue
        for fi in region['triangulos']:
            face=T['caras'][fi];kept.append((face,k))
            for i in face: members[i].add(k)
    dsu={}
    def find(a):
        dsu.setdefault(a,a)
        if dsu[a]!=a: dsu[a]=find(dsu[a])
        return dsu[a]
    def union(a,b): dsu[find(a)]=find(b)
    for h in H:
        a,b=IDS[h['parent']],IDS[h['child']]
        for i,owners in members.items():
            if a in owners and b in owners: union((i,a),(i,b))
    bm=bmesh.new(); verts={}
    for face,k in kept:
        vs=[]
        for i in face:
            key=find((i,k))
            if key not in verts: verts[key]=bm.verts.new(point(*T['vertices'][i]))
            vs.append(verts[key])
        try: f=bm.faces.new(vs);f.material_index=k
        except ValueError: raise RuntimeError('Cara duplicada inesperada')
    # Bands are subdivided only on the two panels belonging to that hinge.
    for h in H:
        owners={IDS[h['parent']],IDS[h['child']]}
        for offset in [i*R/8 for i in range(-8,9)]:
            pos=h['pos']*S+offset
            plane_co=Vector((pos-CX*S,0,0)) if h['axis']=='x' else Vector((0,CY*S-pos,0))
            normal=Vector((1,0,0)) if h['axis']=='x' else Vector((0,1,0))
            faces=[f for f in bm.faces if f.material_index in owners]
            es={e for f in faces for e in f.edges};vs={v for f in faces for v in f.verts}
            bmesh.ops.bisect_plane(bm,geom=faces+list(es)+list(vs),dist=1e-4,plane_co=plane_co,plane_no=normal,clear_outer=False,clear_inner=False)
    bmesh.ops.dissolve_degenerate(bm,dist=.0002,edges=list(bm.edges))
    bmesh.ops.triangulate(bm,faces=list(bm.faces),quad_method='BEAUTY',ngon_method='BEAUTY')
    bm.verts.index_update();bm.faces.index_update();bm.normal_update()
    for f in bm.faces:
        if f.normal.z<0:f.normal_flip()
    points=[v.co.copy() for v in bm.verts]
    owners=[{f.material_index for f in v.link_faces} for v in bm.verts]
    faces=[([v.index for v in f.verts],f.material_index) for f in bm.faces]
    boundaries=[(e.verts[0].index,e.verts[1].index,e.link_faces[0].material_index) for e in bm.edges if len(e.link_faces)==1]
    if any(len(e.link_faces)>2 for e in bm.edges): raise RuntimeError('Superficie plana no manifold')
    bm.free();return points,owners,faces,boundaries

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

def states_for_vertex(p,owners):
    x=p.x/S+CX;y=CY-p.y/S;owner=min((P[k] for k in owners),key=lambda name:ROLE_PRIORITY[name])
    defaults=dict(fullstate(owner));fractions={i:v/STEPS for i,v in defaults.items()}
    active={'Base'} | {order[i]['child'] for i in fractions}
    for j,h in enumerate(order):
        if h['parent'] not in active and h['child'] not in active:continue
        q=(x-h['pos'])*h['side']*S if h['axis']=='x' else (y-h['pos'])*h['side']*S
        tang=y if h['axis']=='x' else x
        along=min(max(a-tang,0,tang-b)*S for a,b in hsegments(h))
        if abs(q)<=BR and along<BR:
            t=max(0,min(1,.5+.5*q/BR));fade=1-along/BR;fade=fade*fade*(3-2*fade)
            default=fractions.get(j,0);fractions[j]=default+(t-default)*fade
            active.add(h['child'])
    fractional=[(j,f) for j,f in fractions.items() if 1e-6<f<1-1e-6]
    if len(fractional)>3:raise RuntimeError('Cruce de mas de tres bandas: requiere inspeccion')
    fixed={j:STEPS for j,f in fractions.items() if f>=1-1e-6}
    options=[]
    for j,f in fractional:
        raw=f*STEPS;lo=int(math.floor(raw));hi=min(STEPS,lo+1);w=raw-lo
        options.append([(j,lo,1-w),(j,hi,w)])
    out=defaultdict(float)
    for combo in itertools.product(*options):
        state=fixed.copy();weight=1
        for j,n,w in combo:state[j]=n;weight*=w
        if weight>1e-7:out[tuple(sorted((j,n) for j,n in state.items() if n))]+=weight
    return dict(out)

def pose_angles(frame):
    out=[]
    for h in order:
        t=max(0,min(1,(frame-h['start'])/(h['end']-h['start'])));t=t*t*(3-2*t)
        out.append(math.radians(h['angle'])*t)
    return out

def make_native_rig(obj,points,owners):
    vertex_states=[states_for_vertex(p,o) for p,o in zip(points,owners)]
    states=set(mainstates)
    for weights in vertex_states:states.update(weights)
    states=sorted(states,key=lambda st:(len(st),st))
    names={st:mainstates.get(st,f'Flexion_{k:04d}') for k,st in enumerate(states)}
    arm=bpy.data.armatures.new('Esqueleto_Estuche_Flexion');rig=bpy.data.objects.new('Rig_Plegado_Estuche',arm);bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active=rig;rig.select_set(True);bpy.ops.object.mode_set(mode='EDIT')
    for st in states:
        bone=arm.edit_bones.new(names[st])
        if not st:origin=Vector((0,0,1.5));axis=Vector((0,1,0))
        else:
            j,n=st[-1];h=order[j];mid=(h['min']+h['max'])/2
            origin=point(h['pos'],mid,1.5) if h['axis']=='x' else point(mid,h['pos'],1.5)
            axis=Vector((0,1,0)) if h['axis']=='x' else Vector((1,0,0))
            # Main bones lie on score axes. Helpers lie within the bending band.
            if st not in mainstates:
                u=Vector((h['side'],0,0)) if h['axis']=='x' else Vector((0,-h['side'],0));origin+=u*(BR*(2*n/STEPS-1))
        bone.head=origin;bone.tail=origin+axis*(35 if st in mainstates else 8);bone.roll=0
    for st in states:
        name=names[st]
        if name=='Base':continue
        parent=bychild[name]['parent'] if name in bychild else 'Base'
        arm.edit_bones[name].parent=arm.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    controls=arm.collections.new('Paneles — 19 huesos');helpers=arm.collections.new('Flexion fisica — auxiliares')
    helpers.is_visible=False
    for st in states:
        bone=arm.bones[names[st]];(controls if st in mainstates else helpers).assign(bone)
        rig.pose.bones[names[st]].rotation_mode='QUATERNION'
    groups={st:obj.vertex_groups.new(name=names[st]) for st in states}
    count=len(points)
    for i,weights in enumerate(vertex_states):
        for st,w in weights.items():groups[st].add([i,i+count],w,'REPLACE')
    mod=obj.modifiers.new('Plegado_de_bandas_fisicas','ARMATURE');mod.object=rig;mod.use_deform_preserve_volume=True;obj.parent=rig;rig.show_in_front=True
    rig['plano_medio_nominal_mm']=1.5;rig['ancho_banda_flexion_mm']=2*BR;rig['divisiones_flexion']=STEPS
    print('RIG',len(states),'huesos, 19 paneles',flush=True)
    scene=bpy.context.scene;scene.frame_start=1;scene.frame_end=C['frame_end'];scene.render.fps=30
    rest={st:arm.bones[names[st]].matrix_local.copy() for st in states}; invrest={st:m.inverted() for st,m in rest.items()}
    parentstate={st:fullstate(bychild[names[st]]['parent']) if names[st] in bychild else () for st in states if st}
    lastquat={}
    for frame in sorted(set(range(1,C['frame_end']+1,4)) | {1,C['frame_end']} | {h['start'] for h in H} | {h['end'] for h in H}):
        angles=pose_angles(frame);cache={};poses={}
        for st in states:
            transform=Matrix.Identity(4)
            for j,n in st:
                key=(j,n)
                if key not in cache:cache[key]=bend_matrix(order[j],n/STEPS,angles[j])
                transform=transform@cache[key]
            poses[st]=transform@rest[st]
        for st in states:
            pb=rig.pose.bones[names[st]];target=poses[st]
            if st:
                parent=parentstate[st];basis=invrest[st]@rest[parent]@poses[parent].inverted()@target
            else:basis=invrest[st]@target
            loc,quat,scale=basis.decompose()
            if st in lastquat and lastquat[st].dot(quat)<0:quat.negate()
            lastquat[st]=quat.copy();pb.location=loc;pb.rotation_quaternion=quat;pb.scale=scale
            pb.keyframe_insert(data_path='location',frame=frame,group=pb.name);pb.keyframe_insert(data_path='rotation_quaternion',frame=frame,group=pb.name)
        if frame%50==0:print('POSE',frame,flush=True)
    action=rig.animation_data.action;action.name='Montaje_Estuche_Flexion_Fisica'
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for fc in bag.fcurves:
                    for kp in fc.keyframe_points:kp.interpolation='LINEAR'
    for frame,name in [(1,'01 Frente y trasera'),(38,'02 Orejas interiores'),(68,'03 Laterales'),(108,'04 Puentes'),(143,'05 Retornos'),(183,'06 Preparar tapa'),(218,'07 Orejas frontal'),(248,'08 Cierre tapa'),(310,'09 Cerrado')]:scene.timeline_markers.new(name,frame=frame)
    return rig

def bend_matrix(h,f,angle):
    if not f or abs(angle)<1e-9:return Matrix.Identity(4)
    mid=(h['min']+h['max'])/2; pivot=point(h['pos'],mid,1.5) if h['axis']=='x' else point(mid,h['pos'],1.5)
    u=Vector((h['side'],0,0)) if h['axis']=='x' else Vector((0,-h['side'],0));axis=Vector((0,1,0)) if h['axis']=='x' else Vector((1,0,0))
    child=h['child'];a=0.;length=6.
    if child.startswith('Oreja_') and not child.startswith('Oreja_Frontal_Tapa'):
        length=1.5*math.pi/2;a=6-length
    elif child in ('Lateral_Izq','Lateral_Der'):
        length=1.6*math.pi/2;a=2.9
    elif child in ('Ala_Tapa_Izq','Ala_Tapa_Der'):
        length=1.5*math.pi/2;a=0
    s=6*f;before=min(s,a);arc=max(0,min(length,s-a));after=max(0,s-a-length)
    phi=angle*arc/length;rad=length/abs(angle)
    q=-3+before+rad*math.sin(abs(phi))+after*math.cos(abs(angle))
    z=rad*(1-math.cos(abs(phi)))+after*math.sin(abs(angle))
    dst=pivot+u*q+Vector((0,0,z));src=pivot+u*(s-3)
    rot=Matrix.Rotation(phi,4,axis);rot.translation=dst-rot.to_3x3()@src;return rot

"""Ejecución de la herramienta independiente."""
ROLE_PRIORITY={name:i for i,name in enumerate(['Retorno_Izq','Oreja_Frontal_Tapa_Izq','Ala_Tapa_Izq','Base','Frontal_Tapa','Oreja_Frente_Izq','Tapa','Trasera','Lateral_Izq','Oreja_Trasera_Izq','Ala_Tapa_Der','Oreja_Frontal_Tapa_Der','Oreja_Frente_Der','Oreja_Trasera_Der','Retorno_Der','Lateral_Der','Frente','Puente_Der','Puente_Izq'])}

def diagnostic_svg(records,hinges,output):
    from xml.sax.saxutils import escape
    points=[p for r in records for p in r['puntos_svg']];x0=min(p[0] for p in points);y0=min(p[1] for p in points);w=max(p[0] for p in points)-x0;h=max(p[1] for p in points)-y0
    lines=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" viewBox="{x0} {y0} {w} {h}">', '<title>Cortes rojos; hendidos azules. Coordenadas físicas en milímetros.</title>','<g fill="none" stroke-width="0.25">'];classification=[]
    for rec in records:
        classes=Counter()
        for a,b in zip(rec['puntos_svg'],rec['puntos_svg'][1:]):
            fold=False
            for joint in hinges:
                fixed=0 if joint['axis']=='x' else 1;tangent=1-fixed
                if max(abs(a[fixed]-joint['pos']),abs(b[fixed]-joint['pos']))>.015:continue
                mid=(a[tangent]+b[tangent])/2
                if any(lo-.015<=mid<=hi+.015 for lo,hi in joint['segments']):fold=True;break
            cls='hendido' if fold else 'corte';classes[cls]+=1;color='#166cba' if fold else '#c53131'
            lines.append(f'<path d="M {a[0]:.6f} {a[1]:.6f} L {b[0]:.6f} {b[1]:.6f}" stroke="{color}"/>')
        classification.append({'id':rec['id'],'id_original':rec['id_original'],'segmentos_clasificados':dict(classes)})
    lines.extend(['</g>','</svg>']);dest=unique_path(output.with_suffix('.cortes_hendidos.svg'));dest.write_text('\n'.join(lines),encoding='utf-8')
    return classification,str(dest)

def configure_globals(top,roles,hinges):
    global T,C,S,TH,R,H,P,IDS,CX,CY,BR,STEPS,bychild,order,index,mainstates
    T=top;H=hinges;P={rid:name for name,rid in roles.items()};IDS=roles.copy();S=1.;TH=ESPESOR_MM;R=ANCHO_HENDIDO_MM/2;BR=BANDA_FLEXION_MM/2;STEPS=DIVISIONES_FLEXION
    C={'thickness':TH,'crease_radius':R,'crease_depth':HUNDIMIENTO_INTERIOR_MM,'frame_end':385}
    base=next(r for r in T['regiones'] if r['id']==roles['Base']);CX=(base['bbox'][0]+base['bbox'][2])/2
    front=next(h for h in H if h['child']=='Frente');back=next(h for h in H if h['child']=='Trasera');CY=(front['pos']+back['pos'])/2
    bychild={h['child']:h for h in H};order=[];remaining=H.copy();done={'Base'}
    while remaining:
        ready=[h for h in remaining if h['parent'] in done]
        if not ready:raise ValueError('Jerarquía de paneles circular o incompleta.')
        for h in ready:order.append(h);done.add(h['child']);remaining.remove(h)
    index={h['child']:i for i,h in enumerate(order)};mainstates={fullstate(name):name for name in P.values()}

def audit_materials_and_mesh(obj,rig):
    bm=bmesh.new();bm.from_mesh(obj.data);n=len(obj.data.vertices)//2
    report={'blender':bpy.app.version_string,'vertices':len(bm.verts),'caras':len(bm.faces),'aristas_no_manifold':sum(not e.is_manifold for e in bm.edges),'caras_degeneradas':sum(f.calc_area()<1e-8 for f in bm.faces),'volumen_plano_mm3':bm.calc_volume(signed=True),'huesos':len(rig.data.bones),'huesos_principales':19,'error_altura_huesos_reposo_mm':max(abs(b.head_local.z-1.5) for b in rig.data.bones),'vertices_sin_peso':sum(not v.groups for v in obj.data.vertices),'error_suma_pesos':max(abs(sum(g.weight for g in v.groups)-1) for v in obj.data.vertices),'materiales':[m.name for m in obj.data.materials],'caras_por_slot':dict(Counter(p.material_index for p in obj.data.polygons)),'hendido_interior_max_mm':max(3-v.co.z for v in obj.data.vertices[n:]),'hendido_exterior_max_mm':max(v.co.z for v in obj.data.vertices[:n]),'espesor_minimo_mm':min(obj.data.vertices[i+n].co.z-obj.data.vertices[i].co.z for i in range(n)),'espesor_maximo_mm':max(obj.data.vertices[i+n].co.z-obj.data.vertices[i].co.z for i in range(n)),'cycles':bpy.context.scene.render.engine=='CYCLES','sin_camaras_ni_luces':not any(o.type in ('CAMERA','LIGHT') for o in bpy.context.scene.objects),'renders_ejecutados':False,'limite_animacion':LIMITACION_ANIMACION}
    bm.free()
    if report['aristas_no_manifold'] or report['caras_degeneradas'] or report['vertices_sin_peso']:raise RuntimeError('La malla no supera la comprobación topológica. Revisa el SVG; no se guardará un resultado inválido.')
    if len(report['materiales'])!=3 or any(report['caras_por_slot'].get(i,0)==0 for i in range(3)):raise RuntimeError('La asignación de tres materiales no es válida.')
    if abs(report['hendido_exterior_max_mm']-HUNDIMIENTO_EXTERIOR_MM)>.002 or abs(report['hendido_interior_max_mm']-HUNDIMIENTO_INTERIOR_MM)>.002:raise RuntimeError('No se alcanza la profundidad prevista de los hendidos.')
    if report['espesor_minimo_mm']<ESPESOR_MM-HUNDIMIENTO_INTERIOR_MM-HUNDIMIENTO_EXTERIOR_MM-.01:raise RuntimeError('El espesor local se ha reducido por debajo del perfil previsto.')
    return report

def annotate_scene(obj,svg_path=None):
    scene=bpy.context.scene;scene.render.engine='CYCLES';scene.unit_settings.system='METRIC';scene.unit_settings.length_unit='MILLIMETERS';scene.unit_settings.scale_length=.001
    scene['estado_animacion']=LIMITACION_ANIMACION
    scene.timeline_markers.clear()
    for frame,label in [(1,'01 Frente y trasera'),(38,'02 Orejas del cuerpo'),(68,'03 Alas de tapa'),(98,'04 Laterales'),(138,'05 Puentes'),(173,'06 Retornos'),(213,'07 Bajar tapa'),(278,'08 Orejas frontales'),(308,'09 Frontal exterior'),(365,'10 Cerrado')]:scene.timeline_markers.new(label,frame=frame)
    text=bpy.data.texts.get('LEEME') or bpy.data.texts.new('LEEME');text.clear();text.write('ESTUCHE — CARTON 3 mm\n\nMateriales: 01 Exterior blanco; 02 Interior marron; 03 Canto carton.\nHendido: canal 4.8 mm, interior 1.05 mm y exterior 0.42 mm.\nEspesor minimo del perfil plano: 1.53 mm. Huesos de reposo en Z=1.5 mm.\nFrame 1 plano; frames 365-385 cerrado. Orejas interiores; frontal de tapa exterior.\n\n'+LIMITACION_ANIMACION+'\n\nGenerador independiente incluido en el bloque CREAR_ESTUCHE.py.\nSin renders, camaras, luces, bake de texturas ni exportaciones GLB.\n')
    embedded=bpy.data.texts.get('CREAR_ESTUCHE.py') or bpy.data.texts.new('CREAR_ESTUCHE.py');embedded.clear();embedded.write(Path(__file__).read_text(encoding='utf-8'))
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type=='VIEW_3D':
                space=area.spaces.active;space.shading.type='SOLID';space.shading.color_type='MATERIAL';space.clip_start=.1;space.clip_end=10000
                space.region_3d.view_distance=650;space.region_3d.view_location=(0,0,55);space.region_3d.view_rotation=Quaternion((.88,.32,.14,.31)).normalized()
    bpy.ops.object.select_all(action='DESELECT');obj.select_set(True);bpy.context.view_layer.objects.active=obj

def save_result(output,report):
    output=unique_path(output)
    bpy.context.preferences.filepaths.save_version=0
    if hasattr(bpy.context.preferences.filepaths,'file_preview_type'):
        try:bpy.context.preferences.filepaths.file_preview_type='NONE'
        except (ValueError,TypeError):pass
    bpy.context.scene.frame_set(385);bpy.context.view_layer.update()
    bpy.ops.wm.save_as_mainfile(filepath=str(output),compress=True)
    report['archivo_blend']=str(output)
    unique_path(output.with_suffix('.informe.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('RESULTADO_BLEND='+str(output),flush=True)
    return output

def worker(args):
    global obj,rig,scene,R
    if bpy.app.version<(5,2,0):raise RuntimeError('Este generador necesita Blender 5.2 o posterior.')
    if args.update_current:
        meshes=[o for o in bpy.context.scene.objects if o.type=='MESH' and 'espesor_nominal_mm' in o]
        if len(meshes)!=1:raise RuntimeError('La escena no contiene una única malla compatible del prototipo.')
        obj=meshes[0];rig=obj.parent
        if not rig or rig.type!='ARMATURE':raise RuntimeError('La malla no tiene un rig compatible.')
        material_slots_and_double_crease(obj);annotate_scene(obj);report=audit_materials_and_mesh(obj,rig);report['modo']='actualizacion del prototipo; rig y animacion existentes conservados'
        return save_result(args.output,report)
    if not args.svg:raise ValueError('Falta el archivo SVG.')
    source=args.svg.resolve();records,unit_reason=read_svg(source,args.units);top,shared=topology(records);roles,hinges=recognize_panels(top,shared)
    output=args.output or Path(__file__).resolve().parent/'salidas'/f'{source.stem}_estuche.blend'
    output=unique_path(output);analysis={'svg':str(source),'sha256_svg':hashlib.sha256(source.read_bytes()).hexdigest(),'unidades':unit_reason,'paneles':roles,'articulaciones':hinges,'reparaciones_particion':top['reparaciones_particion'],'limite_animacion':LIMITACION_ANIMACION}
    analysis['clasificacion_trazados'],analysis['diagnostico_svg']=diagnostic_svg(records,hinges,output)
    unique_path(output.with_suffix('.analisis.json')).write_text(json.dumps(analysis,ensure_ascii=False,indent=2),encoding='utf-8')
    print('ANALISIS:',len(records),'trazados,',len(top['regiones']),'regiones,',len(roles),'paneles.',flush=True)
    if args.analyze_only:return output.with_suffix('.analisis.json')
    # This runs in a new background Blender process, never in the user's live scene.
    for o in list(bpy.data.objects):bpy.data.objects.remove(o,do_unlink=True)
    configure_globals(top,roles,hinges);scene=bpy.context.scene;scene.render.engine='CYCLES';scene.unit_settings.system='METRIC';scene.unit_settings.length_unit='MILLIMETERS';scene.unit_settings.scale_length=.001
    R=BR;points,owners,faces,boundaries=planar_mesh();R=ANCHO_HENDIDO_MM/2
    obj=make_volume(points,owners,faces,boundaries);obj['origen_svg']=str(source);obj['sha256_svg']=analysis['sha256_svg']
    material_slots_and_double_crease(obj);rig=make_native_rig(obj,points,owners);annotate_scene(obj,source)
    report=audit_materials_and_mesh(obj,rig);report.update(analysis);report['modo']='generacion independiente desde SVG'
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    if before!=analysis['sha256_svg']:raise RuntimeError('El SVG de entrada cambió durante el proceso.')
    report['svg_original_intacto']=True
    return save_result(output,report)

def show_blender_file_picker():
    class CREAR_ESTUCHE_OT_svg(bpy.types.Operator):
        bl_idname='import_scene.crear_estuche_svg_independiente'
        bl_label='Crear estuche desde SVG'
        filepath:bpy.props.StringProperty(subtype='FILE_PATH')
        filter_glob:bpy.props.StringProperty(default='*.svg',options={'HIDDEN'})
        units:bpy.props.EnumProperty(name='Unidades sin tamaño físico',items=[('auto','Automático / Illustrator','Usa tamaño físico declarado; si falta, puntos de Illustrator'),('mm','Milímetros','1 unidad del dibujo es 1 mm'),('px','Píxeles a 96 ppp','Conversión estándar de píxeles SVG'),('pt','Puntos de Illustrator','25.4 / 72 mm por unidad')],default='auto')
        def invoke(self,context,event):context.window_manager.fileselect_add(self);return {'RUNNING_MODAL'}
        def execute(self,context):
            source=Path(self.filepath).resolve();script=Path(__file__).resolve()
            if not script.is_file():
                embedded=bpy.data.texts.get('CREAR_ESTUCHE.py')
                if embedded is None:raise RuntimeError('Abre el archivo crear_estuche_desde_svg.py en el editor de texto de Blender y ejecútalo de nuevo.')
                folder=Path(bpy.data.filepath).resolve().parent if bpy.data.filepath else Path.cwd();script=unique_path(folder/'crear_estuche_desde_svg_embebido.py');script.write_text(embedded.as_string(),encoding='utf-8')
            output=unique_path(script.parent/'salidas'/f'{source.stem}_estuche.blend');log=output.with_suffix('.log');stream=log.open('w',encoding='utf-8')
            cmd=[bpy.app.binary_path,'--background','--factory-startup','--python-exit-code','1','--python',str(script),'--','--worker','--svg',str(source),'--output',str(output),'--units',self.units]
            proc=subprocess.Popen(cmd,stdout=stream,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            def poll():
                code=proc.poll()
                if code is None:return .5
                stream.close()
                def draw(menu,ctx):menu.layout.label(text=str(output if code==0 else log));menu.layout.label(text='Animación: consultar informe de colisiones pendiente.' if code==0 else 'El SVG no pudo reconocerse. Consulta el registro.')
                bpy.context.window_manager.popup_menu(draw,title='Estuche guardado' if code==0 else 'Error al generar el estuche',icon='INFO' if code==0 else 'ERROR');return None
            bpy.app.timers.register(poll,first_interval=.5);self.report({'INFO'},'Creando estuche en segundo plano. Tu escena actual se conserva.');return {'FINISHED'}
    old=getattr(bpy.types,'CREAR_ESTUCHE_OT_svg',None)
    if old:bpy.utils.unregister_class(old)
    bpy.utils.register_class(CREAR_ESTUCHE_OT_svg);bpy.ops.import_scene.crear_estuche_svg_independiente('INVOKE_DEFAULT')

def main():
    try:
        if bpy is not None:
            if '--' not in sys.argv:sys.argv=[sys.argv[0]]
            args=parse_arguments()
            if args.worker or args.update_current or (bpy.app.background and args.svg):worker(args)
            else:show_blender_file_picker()
        else:launch_with_python(parse_arguments())
    except Exception as error:
        traceback.print_exc()
        if bpy is None:
            try:
                import tkinter as tk
                from tkinter import messagebox
                root=tk.Tk();root.withdraw();messagebox.showerror('Error al crear el estuche',str(error));root.destroy()
            except Exception:pass
        raise

if __name__=='__main__':main()
