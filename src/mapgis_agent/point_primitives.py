"""Native WT circle/arc graphic fields, pinned against normal 6.7 SDK records.

WT stores their centres as points and their exact curves as graphic parameters.
The 62 saved graphic bytes equal the first62 bytes of the SDK's94-byte PNT_INFO;
the remaining32 bytes are the native conservative rectangle. Angles are degrees
counterclockwise from east. Radius/pen width are drawing millimetres.
"""
import math,struct
from .core import BridgeError

FIELDS={'radius_mm','border_color','filled','start_angle','end_angle'}
COMMON={'point_type','color','layer','pen_width','radius_mm'}


def validate_style(style,crs):
    kind=style['point_type']
    if kind not in (2,3):
        if set(style)&FIELDS:raise BridgeError('POINT_PRIMITIVE: circle/arc parameters require point_type2 or3')
        if 'pen_width' in style:
            if kind!=1:raise BridgeError('UNKNOWN_STYLE_FIELD: pen_width belongs to a symbol, circle or arc, not annotation')
            if not 0<=style['pen_width']<=3.4028234663852886e38:raise BridgeError('POINT_SYMBOL: finite nonnegative float32 pen_width required')
        return
    if type(kind) is not int:raise BridgeError('POINT_PRIMITIVE: integral point_type required')
    if crs and crs.is_geographic:raise BridgeError('POINT_PRIMITIVE: use an explicit projected CRS or local drawing frame')
    allowed=COMMON|({'border_color','filled'} if kind==2 else {'start_angle','end_angle'})
    if set(style)-allowed:raise BridgeError('POINT_PRIMITIVE: parameter is not valid for this point kind')
    if style.get('radius_mm',0)<=0:raise BridgeError('POINT_PRIMITIVE: positive radius_mm required')
    style.setdefault('color',1);style.setdefault('layer',0);style.setdefault('pen_width',.2)
    if style['pen_width']<=0 or style['pen_width']>3.4028234663852886e38:raise BridgeError('POINT_PRIMITIVE: positive float32 pen_width required')
    for key in ('color','layer'):
        if type(style[key]) is not int or not 0<=style[key]<=255:raise BridgeError('POINT_PRIMITIVE: tested colour/layer range0..255')
    if kind==2:
        style.setdefault('border_color',style['color']);style.setdefault('filled',0)
        if type(style['border_color']) is not int or not 0<=style['border_color']<=255 or type(style['filled']) is not int or style['filled'] not in (0,1):raise BridgeError('POINT_PRIMITIVE: border_color0..255 and filled0/1 required')
    elif not all(key in style for key in ('start_angle','end_angle')) or not 0<=style['start_angle']<style['end_angle']<=360:
        raise BridgeError('POINT_PRIMITIVE: explicit0<=start_angle<end_angle<=360 required')


def encode(blob,sections,styles):
    changed=bytearray(blob)
    for identity,style in enumerate(styles,1):
        kind=style.get('point_type')
        if kind not in (2,3):continue
        raw=bytearray(62);raw[0]=kind
        struct.pack_into('<d',raw,2,style['radius_mm'])
        struct.pack_into('<i',raw,44,style['color']);struct.pack_into('<h',raw,52,style['layer'])
        if kind==2:
            struct.pack_into('<ifB',raw,10,style['border_color'],style['pen_width'],style['filled'])
        else:struct.pack_into('<3f',raw,10,style['start_angle'],style['end_angle'],style['pen_width'])
        offset=sections[0][0]+identity*93+31;changed[offset:offset+62]=raw
    return bytes(changed)


def decode(blob,reader,sections):
    if reader.shapeType!='POINT' or not reader.point_params['point_type'].isin([2,3]).any():return
    from .core import native_ids
    params=reader.point_params.copy()
    for field in FIELDS:params[field]=float('nan')
    for row,identity in enumerate(native_ids(reader)):
        # Deleted physical slots leave a filtered DataFrame with its original
        # labels. Assign to that label, not to the dense iteration ordinal.
        row=params.index[row]
        pos=sections[0][0]+identity*93;kind=blob[pos+31]
        if kind not in (2,3):continue
        radius=struct.unpack_from('<d',blob,pos+33)[0]
        if not math.isfinite(radius) or radius<=0:raise BridgeError('POINT_PRIMITIVE_INVALID_RADIUS')
        for field in ('height','width','spacing','angle','font_zh','font_en','font_style'):params.loc[row,field]=float('nan')
        params.loc[row,'radius_mm']=radius;params.loc[row,'color']=struct.unpack_from('<i',blob,pos+75)[0]
        params.loc[row,'layer']=struct.unpack_from('<h',blob,pos+83)[0]
        if kind==2:
            color,width,filled=struct.unpack_from('<ifB',blob,pos+41)
            params.loc[row,'border_color']=color;params.loc[row,'pen_width']=width;params.loc[row,'filled']=filled
        else:
            start,end,width=struct.unpack_from('<3f',blob,pos+41)
            params.loc[row,'start_angle']=start;params.loc[row,'end_angle']=end;params.loc[row,'pen_width']=width
    reader.point_params=params


def style_fields(row):
    kind=row.get('point_type')
    if kind in (2,3):return COMMON|({'border_color','filled'} if kind==2 else {'start_angle','end_angle'})
    # Original IPnt_Info_Sub.penw and its native CLN->WT exporter confirm
    # symbol pen_width at WT+49. Those bytes hold font fields in annotation.
    return set(row.index)-FIELDS-({'pen_width'} if kind!=1 else set())
