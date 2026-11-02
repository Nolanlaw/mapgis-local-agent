"""Verified PNT_INFO fields for native annotation/symbol update and readback."""
import math,struct

from .core import BridgeError,native_ids,read_native

# Names/opcodes match the small immutable table in the owned native worker.
# Offsets are within the 62 saved PNT_INFO bytes, not the whole WT record.
FIELDS=[('color','i',(44,44),0,255),('layer','h',(52,52),0,255),
    ('angle','f',(14,14),None,None),('height','f',(2,6),0,None),
    ('width','f',(6,10),0,None),('spacing','f',(10,-1),0,None),
    ('font_zh','h',(18,-1),0,255),('font_en','h',(20,-1),0,255),
    ('font_style','h',(22,-1),0,255),('symbol_no','h',(-1,2),0,32767),
    ('pen_width','f',(-1,18),0,None)]
INDEX={field[0]:i for i,field in enumerate(FIELDS)}


def prepare_point_styles(parameters,reader):
    updates=parameters.get('updates') if isinstance(parameters,dict) else None
    if (reader.shapeType!='POINT' or not isinstance(parameters,dict) or set(parameters)!={'updates'}
        or not isinstance(updates,list) or not 1<=len(updates)<=1000):
        raise BridgeError('POINT_STYLE_UPDATES: explicit WT record IDs and style dictionaries required')
    kinds=dict(zip(native_ids(reader),reader.point_params['point_type']));seen=set();normalized=[]
    for update in updates:
        if not isinstance(update,dict) or set(update)!={'record_id','style'}:raise BridgeError('POINT_STYLE_UPDATES')
        identity=update['record_id'];style=update['style']
        if type(identity) is not int or identity not in kinds or identity in seen:raise BridgeError('POINT_STYLE_IDS')
        kind=int(kinds[identity])
        if kind not in (0,1):raise BridgeError('POINT_STYLE_KIND: annotation0/symbol1; curves use their existing native radius and graphic tools')
        if not isinstance(style,dict) or not style:raise BridgeError('POINT_STYLE_FIELDS')
        values={}
        for name,value in style.items():
            if name not in INDEX or FIELDS[INDEX[name]][2][kind]<0:raise BridgeError('POINT_STYLE_FIELD: '+str(name))
            _,fmt,_,low,high=FIELDS[INDEX[name]]
            if type(value) not in (int,float) or not math.isfinite(value):raise BridgeError('POINT_STYLE_NUMBER: '+name)
            if low is not None and value<low or high is not None and value>high:raise BridgeError('POINT_STYLE_RANGE: '+name)
            if fmt!='f' and value!=int(value):raise BridgeError('POINT_STYLE_INTEGER: '+name)
            try:actual=struct.unpack('<'+fmt,struct.pack('<'+fmt,value if fmt=='f' else int(value)))[0]
            except (OverflowError,struct.error) as error:raise BridgeError('POINT_STYLE_RANGE: '+name) from error
            if not math.isfinite(actual) or name in {'height','width'} and actual<=0:raise BridgeError('POINT_STYLE_SIZE: '+name)
            values[name]=actual
        seen.add(identity);normalized.append({'record_id':identity,'style':dict(sorted(values.items()))})
    return {'updates':sorted(normalized,key=lambda u:u['record_id']),'point_style_protocol':1}


def native_commands(parameters):
    commands=[]
    for update in parameters['updates']:
        values=sorted(update['style'].items(),key=lambda item:INDEX[item[0]])
        commands.extend([str(update['record_id']),str(len(values))])
        commands.extend(word for name,value in values for word in (str(INDEX[name]),repr(value)))
    return ' '.join(commands)


def validate_saved_styles(source,candidate,parameters):
    before,reader,old_sections=read_native(source);after,_,new_sections=read_native(candidate)
    checked=[]
    for update in parameters['updates']:
        identity=update['record_id'];old=old_sections[0][0]+identity*93+31;new=new_sections[0][0]+identity*93+31
        expected=bytearray(before[old:old+62]);kind=expected[0]
        for name,value in update['style'].items():
            _,fmt,offsets,_,_=FIELDS[INDEX[name]];struct.pack_into('<'+fmt,expected,offsets[kind],value)
        if after[new:new+62]!=expected:raise BridgeError('POINT_STYLE_SAVED_INFO_DIFF: unrequested graphic bytes changed')
        if (identity+1)*32>new_sections[3][1]:raise BridgeError('POINT_STYLE_SAVED_BOUNDS_MISSING')
        bounds=struct.unpack_from('<4d',after,new_sections[3][0]+identity*32)
        if not all(math.isfinite(v) for v in bounds) or bounds[0]>bounds[2] or bounds[1]>bounds[3]:raise BridgeError('POINT_STYLE_SAVED_BOUNDS_INVALID')
        checked.append({'record_id':identity,'style':update['style'],'saved_native_bounds':list(bounds)})
    return {'native_point_style_bytes_verified':True,'native_point_style_updates':checked,
        'bounds_policy':'BAS60 Get/UpdatePntInfo recalculates and saves its native rectangle; font availability is a separate library concern'}
