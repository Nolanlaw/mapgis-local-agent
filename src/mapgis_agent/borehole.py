"""Explicit drillhole survey mathematics and native-section input preparation."""
from __future__ import annotations
import math
import numpy as np
from scipy.spatial import geometric_slerp
from .core import BridgeError
from .raster import explicit_crs,crs_metadata
from ._vendor.wellpathpy.mincurve import minimum_curvature,minimum_curvature_inner
from ._vendor.wellpathpy.geometry import direction_vector,spherical


def _number(value):return type(value) in (int,float) and math.isfinite(value)


def _frame(frame):
    if (not isinstance(frame,dict) or set(frame)!={'crs','vertical_unit','vertical_datum','azimuth_reference','inclination_reference'}
            or frame['vertical_unit']!='metre' or not isinstance(frame['vertical_datum'],str) or not frame['vertical_datum'].strip()
            or frame['azimuth_reference']!='grid_north' or frame['inclination_reference']!='vertical_down'):
        raise BridgeError('BOREHOLE_FRAME: explicit metre horizontal/vertical units, named elevation datum, grid_north bearings and vertical_down inclination required')
    try:crs=explicit_crs(frame['crs'])
    except (TypeError,AttributeError,BridgeError) as error:raise BridgeError('BOREHOLE_FRAME: explicit projected metre CRS or referenced local metre grid required') from error
    if ((crs is None and frame['crs']['unit']!='metre') or (crs is not None and (not crs.is_projected or len(crs.axis_info)!=2
            or any(not math.isclose(a.unit_conversion_factor,1.,rel_tol=1e-12) for a in crs.axis_info)))):
        raise BridgeError('BOREHOLE_FRAME: convert coordinates and units explicitly before trajectory calculation')
    return {**frame,'crs':crs_metadata(frame['crs'],crs)}


def borehole_trajectory(survey,collar,coordinate_frame,*,query_mds=None,course_length=30.):
    frame=_frame(coordinate_frame)
    if not isinstance(collar,dict) or set(collar)!={'easting','northing','elevation'} or not all(_number(v) for v in collar.values()):raise BridgeError('BOREHOLE_COLLAR: finite easting/northing/elevation in the declared frame required')
    if not isinstance(survey,list) or not 2<=len(survey)<=10000:raise BridgeError('BOREHOLE_SURVEY:2..10000 ordered stations required')
    for station in survey:
        if not isinstance(station,dict) or set(station)!={'md','inclination','azimuth'} or not all(_number(v) for v in station.values()):raise BridgeError('BOREHOLE_SURVEY: finite md/inclination/azimuth, no inferred fields')
    md=np.array([s['md'] for s in survey]);inc=np.array([s['inclination'] for s in survey]);azi=np.array([s['azimuth'] for s in survey])
    if md[0]!=0 or np.any(np.diff(md)<=0) or np.any((inc<0)|(inc>=180)) or np.any((azi<0)|(azi>=360)):
        raise BridgeError('BOREHOLE_SURVEY: measured depth starts0 and strictly increases; inclination0..180 exclusive, azimuth0..360 exclusive')
    if not _number(course_length) or course_length<=0:raise BridgeError('BOREHOLE_COURSE: positive metre course length required')
    directions=np.column_stack(direction_vector(inc,azi))
    if np.any(np.linalg.norm(directions[:-1]+directions[1:],axis=1)<=1e-6):raise BridgeError('BOREHOLE_ANTIPODAL: opposite/near-opposite station directions have no unique stable minimum-curvature arc; supply intermediate survey stations')
    if query_mds is None:query=[]
    else:
        if not isinstance(query_mds,list) or len(query_mds)>100000 or any(not _number(v) or not 0<=v<=md[-1] for v in query_mds):raise BridgeError('BOREHOLE_QUERY: at most100000 measured depths within the actual survey, no extrapolation')
        query=query_mds
    tvd,north,east,dls=minimum_curvature(md,inc,azi,course_length)
    all_md=np.unique(np.r_[md,np.asarray(query,dtype=float)]);all_inc=np.empty(len(all_md));all_azi=np.empty(len(all_md))
    source_indexes=np.searchsorted(all_md,md);all_inc[source_indexes]=inc;all_azi[source_indexes]=azi
    for i in range(len(md)-1):
        lower=source_indexes[i]+1;upper=source_indexes[i+1]
        if lower>=upper:continue
        chosen=slice(lower,upper)
        fraction=(all_md[chosen]-md[i])/(md[i+1]-md[i])
        directions_at_query=geometric_slerp(directions[i],directions[i+1],fraction)
        all_inc[chosen],all_azi[chosen]=spherical(*directions_at_query.T)
    # The interpolated arc can pass exactly through vertical-up, a valid
    # direction without a unique azimuth. The radians numerical kernel handles
    # it directly, without the original public input-domain restriction inc<180.
    qtvd,qnorth,qeast,_=minimum_curvature_inner(all_md,np.deg2rad(all_inc),np.deg2rad(all_azi))
    qtvd=np.r_[0.,qtvd];qnorth=np.r_[0.,qnorth];qeast=np.r_[0.,qeast]
    source_positions=np.c_[tvd,north,east];resampled=np.c_[qtvd,qnorth,qeast]
    station_error=float(np.max(np.abs(source_positions-resampled[source_indexes])))
    if not np.all(np.isfinite(resampled)) or station_error>max(1e-8,float(md[-1])*5e-10):raise BridgeError('BOREHOLE_NUMERIC: resampling changed original survey station positions')
    def row(depth,down,n,e):
        result={'md':float(depth),'true_vertical_depth':float(down),'easting':float(collar['easting']+e),'northing':float(collar['northing']+n),'elevation':float(collar['elevation']-down),
            'east_offset':float(e),'north_offset':float(n)}
        if not all(math.isfinite(v) for v in result.values()):raise BridgeError('BOREHOLE_NUMERIC: coordinate range overflow')
        return result
    stations=[row(m,d,n,e)|{'inclination':float(i),'azimuth':float(a),'dogleg_degrees_per_course':float(severity)} for m,d,n,e,i,a,severity in zip(md,tvd,north,east,inc,azi,dls)]
    queried=[row(m,qtvd[k],qnorth[k],qeast[k]) for m,k in zip(query,np.searchsorted(all_md,query))]
    return {'backend':'unmodified wellpathpy0.5.2 minimum-curvature numerical modules; SciPy1.16.2 geometric_slerp query directions','coordinate_frame':frame,
        'collar':dict(collar),'course_length_metres':float(course_length),'dogleg_unit':'degrees per declared metre course length','survey_samples':stations,'query_samples':queried,
        'resampled_survey_station_error_max':station_error,'coordinate_conversion_performed':False,
        'conventions':'MD measured along borehole; inclination from vertical down; grid azimuth clockwise from north; elevation positive upward',
        'scope':'Computed positions from explicit station model; minimum-curvature interpolation is not an observed in-between survey or native3D-file edit.'}


def apparent_dip(dip_direction,dip,section_azimuth):
    if (not all(_number(v) for v in (dip_direction,dip,section_azimuth)) or not 0<=dip<=90 or not 0<=dip_direction<360 or not 0<=section_azimuth<360):
        raise BridgeError('DIP_ANGLES: dip0..90, dip direction and positive section azimuth0..360 exclusive, degrees from common grid north')
    component=math.cos(math.radians(section_azimuth-dip_direction))
    if abs(component)<1e-14:component=0.
    if dip==90:
        if component==0:raise BridgeError('DIP_UNDEFINED: vertical bedding and parallel/coincident vertical section planes have no unique apparent-dip intersection')
        angle=math.copysign(90.,component)
    else:angle=math.degrees(math.atan2(math.sin(math.radians(dip))*component,math.cos(math.radians(dip))))
    return {'apparent_dip_degrees':angle,'dip_direction_degrees':dip_direction,'true_dip_degrees':dip,'section_azimuth_degrees':section_azimuth,
        'positive_sign':'down along increasing section distance','angle_reference':'all azimuths clockwise from the same grid north; no magnetic/true-north conversion',
        'geological_interpretation_inferred':False}


def validate_intervals(intervals,total_depth):
    """Keep gaps unlogged and reject ambiguous overlapping core intervals."""
    if not isinstance(intervals,list) or len(intervals)>10000:raise BridgeError('BOREHOLE_INTERVALS: ordered list of at most10000 intervals required')
    result=[];gaps=[];last=0.
    for item in intervals:
        if not isinstance(item,dict) or not {'top','base','unit'}<=set(item)<={'top','base','unit','description'}:
            raise BridgeError('BOREHOLE_INTERVALS: explicit top/base measured depth, unit and optional description required')
        top,base=item['top'],item['base']
        if not _number(top) or not _number(base) or not last<=top<base<=total_depth:raise BridgeError('BOREHOLE_INTERVALS: positive ordered nonoverlapping intervals inside actual measured depth; gaps remain unlogged')
        if not isinstance(item['unit'],str) or not item['unit'].strip() or not isinstance(item.get('description',''),str) or len(item.get('description',''))>32767:raise BridgeError('BOREHOLE_INTERVALS: nonempty unit label and string description up to32767 characters required; no workbook truncation')
        if top>last:gaps.append({'top':last,'base':float(top)})
        result.append({'top':float(top),'base':float(base),'unit':item['unit'],'description':item.get('description','')});last=base
    if last<total_depth:gaps.append({'top':float(last),'base':float(total_depth)})
    return result,gaps
