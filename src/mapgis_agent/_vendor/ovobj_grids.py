# -*- coding: utf-8 -*-
"""Map-sheet kernel adapted from good9527/ovobj-converter, MIT.

Pinned ef5a338f7c45d22c48c841bff8ae059cf75a1cd9; original grids.py Git blob
57ae9495173494842e28b0d7c12b44ecfa601c9f. See ovobj-grids.LICENSE and
ovobj-grids.PATCH.md. Corrected2k table, added1k/500, strict domestic
code validation and canonical-float southwest boundary ownership.
"""

import math
import re
from typing import Dict, Union

# Standard GB/T 13989-2012 scale configuration
# Latitude and longitude grid subdivisions per 1:1,000,000 map sheet
SCALES_CONFIG: Dict[str, Dict[str, Union[str, float, int, None]]] = {
    '1m':   {'letter': None, 'dlat': 4.0,        'dlon': 6.0,        'rows': 1,   'cols': 1},
    '500k': {'letter': 'B',  'dlat': 2.0,        'dlon': 3.0,        'rows': 2,   'cols': 2},
    '250k': {'letter': 'C',  'dlat': 1.0,        'dlon': 1.5,        'rows': 4,   'cols': 4},
    '100k': {'letter': 'D',  'dlat': 20.0 / 60,  'dlon': 30.0 / 60,  'rows': 12,  'cols': 12},
    '50k':  {'letter': 'E',  'dlat': 10.0 / 60,  'dlon': 15.0 / 60,  'rows': 24,  'cols': 24},
    '25k':  {'letter': 'F',  'dlat': 5.0 / 60,   'dlon': 7.5 / 60,   'rows': 48,  'cols': 48},
    '10k':  {'letter': 'G',  'dlat': 2.5 / 60,   'dlon': 3.75 / 60,  'rows': 96,  'cols': 96},
    '5k':   {'letter': 'H',  'dlat': 1.25 / 60,  'dlon': 1.875 / 60, 'rows': 192, 'cols': 192},
    '2k':   {'letter': 'I',  'dlat': 4 / 576, 'dlon': 6 / 576, 'rows': 576, 'cols': 576},
    '1k':   {'letter': 'J',  'dlat': 4 / 1152, 'dlon': 6 / 1152, 'rows': 1152, 'cols': 1152},
    '500':  {'letter': 'K',  'dlat': 4 / 2304, 'dlon': 6 / 2304, 'rows': 2304, 'cols': 2304},
}

LETTER_TO_SCALE: Dict[str, str] = {
    v['letter']: k for k, v in SCALES_CONFIG.items() if v['letter'] is not None
}

ALIAS_TO_SCALE: Dict[str, str] = {
    '1:1000000': '1m', '1000000': '1m', '1m': '1m',
    '1:500000': '500k', '500000': '500k', '500k': '500k',
    '1:250000': '250k', '250000': '250k', '250k': '250k',
    '1:100000': '100k', '100000': '100k', '100k': '100k',
    '1:50000': '50k', '50000': '50k', '50k': '50k',
    '1:25000': '25k', '25000': '25k', '25k': '25k',
    '1:10000': '10k', '10000': '10k', '10k': '10k',
    '1:5000': '5k', '5000': '5k', '5k': '5k',
    '1:2000': '2k', '2000': '2k', '2k': '2k',
    '1:1000': '1k', '1000': '1k', '1k': '1k',
    '1:500': '500', '500': '500',
}

def normalize_scale(scale: str) -> str:
    """Normalizes scale input string to internal standard key."""
    s = str(scale).strip().lower().replace(" ", "")
    if s in ALIAS_TO_SCALE:
        return ALIAS_TO_SCALE[s]
    if s in SCALES_CONFIG:
        return s
    raise ValueError(f"Unsupported map sheet scale: '{scale}'. Supported: {list(SCALES_CONFIG.keys())}")


def _edge(origin,span,index,divisions):
    # All inputs are small integers. A single division rounds the rational
    # edge once, so its returned float and its neighboring floats stay distinct.
    return (origin*divisions+span*index)/divisions


def _axis_index(value,origin,span,divisions):
    index=math.floor((value-origin)*divisions/span)
    while value<_edge(origin,span,index,divisions):index-=1
    while value>=_edge(origin,span,index+1,divisions):index+=1
    return index


def lonlat_to_sheet_code(lon,lat,scale='10k'):
    if not (math.isfinite(lon) and math.isfinite(lat) and 72<=lon<138 and 0<=lat<56):
        raise ValueError('Coordinates outside domestic unmerged envelope72..138E,0..56N')
    key=normalize_scale(scale);cfg=SCALES_CONFIG[key]
    a=math.floor(lat/4)+1;b=math.floor(lon/6)+31
    code=f'{chr(ord("A")+a-1)}{b:02d}'
    if key=='1m':return code
    n=int(cfg['rows']);row=n-_axis_index(lat,(a-1)*4,4,n);col=_axis_index(lon,(b-31)*6,6,n)+1
    digits=4 if n>999 else 3
    return f"{code}{cfg['letter']}{row:0{digits}d}{col:0{digits}d}"


def sheet_code_to_bbox(code):
    if not isinstance(code,str):raise ValueError('Invalid sheet code')
    code=code.strip().upper()
    if not re.fullmatch(r'[A-N](?:4[3-9]|5[0-3])(?:(?:[B-I][0-9]{6})|(?:[JK][0-9]{8}))?',code):
        raise ValueError('Invalid domestic sheet code')
    a=ord(code[0])-ord('A')+1;b=int(code[1:3]);base_x=(b-31)*6;base_y=(a-1)*4
    if len(code)==3:key='1m';n=1;row=col=1
    else:
        key=LETTER_TO_SCALE[code[3]];n=int(SCALES_CONFIG[key]['rows']);digits=4 if n>999 else 3
        row=int(code[4:4+digits]);col=int(code[4+digits:])
        if not (1<=row<=n and 1<=col<=n):raise ValueError('Invalid sheet row or column')
    return {'code':code,'scale':key,'lon_min':_edge(base_x,6,col-1,n),'lat_min':_edge(base_y,4,n-row,n),
        'lon_max':_edge(base_x,6,col,n),'lat_max':_edge(base_y,4,n-row+1,n)}

