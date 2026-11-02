"""Editable native page elements for an explicit fixed, centred plot view.

This reuses the existing vector encoder; native save/plot remain the normal
separate tools. Distances and directions are declared, never inferred geodesy.
"""
from __future__ import annotations
import math
from pathlib import Path
from shapely.geometry import Point,LineString,mapping
from .core import BridgeError
from .creation import prepare_creation
from .native_plot import _view

PARTS={'page.title','page.north_arrow','page.north_label','page.scale_bar','page.scale_labels'}


def _number(value,name,positive=False):
    if type(value) not in (int,float) or not math.isfinite(value) or positive and value<=0:raise BridgeError('PAGE_NUMBER: '+name)
    return float(value)


def prepare_page_element(bridge,part,parameters,template,request_id):
    if part not in PARTS:raise BridgeError('PAGE_PART')
    if not isinstance(parameters,dict):raise BridgeError('PAGE_PARAMETERS')
    try:view=_view(parameters['view'])
    except (KeyError,BridgeError) as error:raise BridgeError('PAGE_VIEW: exact native plotting view required') from error
    bounds=view['native_bounds'];paper=view['paper_mm'];factor=min(paper[0]/(bounds[2]-bounds[0]),paper[1]/(bounds[3]-bounds[1]))
    margin=[(paper[0]-(bounds[2]-bounds[0])*factor)/2,(paper[1]-(bounds[3]-bounds[1])*factor)/2]
    frame=template.get('crs',{})
    if not isinstance(frame,dict) or frame.get('kind')!='local' or frame.get('unit')!='unknown' or type(template.get('map_scale')) not in (int,float) or template['map_scale']!=1:
        raise BridgeError('PAGE_NATIVE_FRAME: named local unknown-unit native plotting frame and map_scale=1 required')
    if 'project_environment' in parameters:
        environment=parameters['project_environment']
        if not isinstance(environment,dict) or environment.get('translation',[0,0])!=[0,0] or environment.get('scale',[1,1])!=[1,1] or environment.get('rotation_degrees',0)!=0:
            raise BridgeError('PAGE_PROJECT_ENVIRONMENT: this fixed-view helper requires identity global project transform')
    text_part=part in {'page.title','page.north_label','page.scale_labels'};suffix='.wt' if text_part else '.wl'
    if Path(template.get('name','')).suffix.lower()!=suffix:raise BridgeError('PAGE_EXTENSION: '+suffix)
    identity=parameters.get('element_id')
    if not isinstance(identity,str) or not identity or len(identity)>64:raise BridgeError('PAGE_ELEMENT_ID')
    anchor=parameters.get('anchor_mm')
    if not isinstance(anchor,list) or len(anchor)!=2:raise BridgeError('PAGE_ANCHOR')
    x,y=[_number(v,'anchor_mm') for v in anchor];rows=[]
    def xy(point):
        if any(not math.isfinite(v) for v in point) or not 0<=point[0]<=paper[0] or not 0<=point[1]<=paper[1]:raise BridgeError('PAGE_OUTSIDE_PAPER')
        return [bounds[i]+(point[i]-margin[i])/factor for i in (0,1)]
    xy((x,y))
    style=dict(template.get('style',{}));allowed={'color','layer','font_zh','font_en','font_style'} if text_part else {'color','layer','linetype'}
    if set(style)-allowed:raise BridgeError('PAGE_STYLE: physical sizes belong in font_mm or line_width_mm')
    if text_part:
        height=_number(parameters.get('font_mm',3.5),'font_mm',True)
        if height>paper[1]:raise BridgeError('PAGE_FONT_SIZE')
        style|={'height':height/factor,'width':height*.7/factor,'spacing':0}
    else:
        width=_number(parameters.get('line_width_mm',.2),'line_width_mm',True);style['width']=width/factor
    def add(points,text=None,value=None):
        geometry=Point(xy(points[0])) if text is not None else LineString([xy(p) for p in points])
        row={'type':'Feature','id':identity+':'+str(len(rows)+1),'geometry':mapping(geometry),'properties':{'element_id':identity,'page_part':part}}
        if value is not None:row['properties']['value']=value
        if text is not None:row['text']=text
        rows.append(row)
    evidence={'plot_view':view,'fit_mm_per_native_unit':factor,'paper_margin_mm':margin,'anchor_mm':[x,y],
        'page_coordinates':'bottom-left origin; existing native plot aspect fit and centring','project_environment':'identity required',
        'geodetic_accuracy':'not inferred from decoration placement'}
    if part=='page.title':
        text=parameters.get('text')
        if not isinstance(text,str) or not text.strip():raise BridgeError('PAGE_TITLE')
        add([(x,y)],text)
    elif part.startswith('page.north'):
        if parameters.get('direction_kind')!='declared':raise BridgeError('PAGE_NORTH_DIRECTION: explicit declared direction required; true north is not guessed')
        angle=math.radians(_number(parameters.get('angle_degrees'),'angle_degrees'));length=_number(parameters.get('length_mm',20),'length_mm',True)
        ux,uy=math.sin(angle),math.cos(angle);tip=(x+length*ux,y+length*uy);xy(tip)
        if text_part:add([(tip[0]+height*1.5*ux,tip[1]+height*1.5*uy)],'N')
        else:
            add([(x,y),tip]);head=length*.25
            for side in (-1,1):add([tip,(tip[0]-.7*head*ux+side*.35*head*uy,tip[1]-.7*head*uy-side*.35*head*ux)])
        evidence|={'direction_kind':'declared','angle_degrees_clockwise_from_page_up':parameters['angle_degrees'],'arrow_length_mm':length}
    else:
        distance=_number(parameters.get('distance_metres'),'distance_metres',True);metres=_number(parameters.get('metres_per_native_unit'),'metres_per_native_unit',True)
        segments=parameters.get('segments',4)
        if type(segments) is not int or not 1<=segments<=20:raise BridgeError('PAGE_SCALE_SEGMENTS')
        length=distance/metres*factor;xy((x+length,y));tick=_number(parameters.get('tick_mm',2),'tick_mm',True)
        if not text_part:add([(x,y),(x+length,y)],value=0)
        for i in range(segments+1):
            px=x+length*i/segments;value=distance*i/segments
            if text_part:
                unit='km' if distance>=1000 else 'm';label=f'{value/(1000 if unit=="km" else 1):g}'+(' '+unit if i==segments else '')
                add([(px-height*.15,y-tick-height*1.2)],label,value)
            else:add([(px,y),(px,y+tick)],value=value)
        evidence|={'declared_metres_per_native_unit':metres,'scale_distance_metres':distance,'scale_paper_length_mm':length,
            'nominal_denominator':1000*metres/factor,'distance_basis':'declared planar native-coordinate length; no geodesic or datum inference'}
    return prepare_creation(bridge,{'type':'FeatureCollection','features':rows},template|{'style':style,'qa':dict(template.get('qa',{}))|evidence},request_id)
