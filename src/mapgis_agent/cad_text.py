"""Explicit, hash-bound single-line font outlines, without font substitution."""
from __future__ import annotations

import hashlib
import io
import math
import re
import unicodedata
from pathlib import Path

from ezdxf import path as cad_path
from ezdxf.addons.text2path import alignment_transformation
from ezdxf.entities import factory
from ezdxf.fonts.ttfonts import TTFontRenderer
from ezdxf.math import Matrix44
from fontTools.ttLib import TTFont, TTLibError
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from shapely.geometry import LineString
from shapely.ops import polygonize, unary_union

from .core import BridgeError

TEXT_TYPES={'TEXT','ATTRIB','ATTDEF','MTEXT'}


class FontBinding(BaseModel):
    model_config=ConfigDict(extra='forbid')
    style:str=Field(min_length=1,max_length=255)
    file:str
    expected_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    face_index:StrictInt=Field(default=0,ge=0,le=63)


def load_fonts(bindings):
    """Load exact bytes directly into fontTools; never touch the FontManager."""
    result={};total_bytes=0
    for binding in bindings:
        key=binding.style.casefold()
        if key in result:raise BridgeError('DXF_FONT_BINDING: styles must have unique case-insensitive bindings')
        file=Path(binding.file).resolve(strict=True)
        if file.stat().st_size>128*1024*1024:raise BridgeError('DXF_FONT_LIMIT: font file exceeds128MiB')
        data=file.read_bytes()
        total_bytes+=len(data)
        if total_bytes>256*1024*1024:raise BridgeError('DXF_FONT_LIMIT: request fonts exceed256MiB')
        if hashlib.sha256(data).hexdigest()!=binding.expected_sha256:raise BridgeError('STALE_FONT')
        try:
            font=TTFont(io.BytesIO(data),fontNumber=binding.face_index)
            if 'fvar' in font:raise BridgeError('DXF_FONT_VARIATION: variable axes need an explicit verified instance')
            cmap=font.getBestCmap() or {}
            if not all(ord(c) in cmap for c in 'Axp '):raise BridgeError('DXF_FONT_METRICS: verified cap/x/descender/space metrics required')
            renderer=TTFontRenderer(font,kerning=False)
        except (TTLibError,KeyError,ValueError,ZeroDivisionError) as exc:
            raise BridgeError('DXF_FONT_UNSUPPORTED: no fallback font was selected') from exc
        if not math.isfinite(renderer.font_measurements.cap_height) or renderer.font_measurements.cap_height<=0:
            raise BridgeError('DXF_FONT_METRICS')
        result[key]=(binding,renderer)
    return result


def _filled_glyph(path,tolerance,budget):
    rings=[]
    for subpath in path.sub_paths():
        points=[]
        for vertex in subpath.flattening(tolerance,segments=4):
            budget[0]+=1
            if budget[0]>200000:raise BridgeError('DXF_TEXT_VERTEX_LIMIT: outline request exceeds200000vertices')
            if not all(math.isfinite(v) for v in vertex) or abs(vertex.z)>1e-9:
                raise BridgeError('DXF_2D_REQUIRED: text outline must remain a finite XY drawing')
            points.append((vertex.x,vertex.y))
        if len(points)<4 or points[0]!=points[-1]:raise BridgeError('DXF_TEXT_GLYPH: closed outline contours required')
        rings.append(points)
    def winding(point):
        x,y=point.x,point.y;total=0
        for ring in rings:
            for (x1,y1),(x2,y2) in zip(ring,ring[1:]):
                cross=(x2-x1)*(y-y1)-(x-x1)*(y2-y1)
                if y1<=y<y2 and cross>0:total+=1
                elif y2<=y<y1 and cross<0:total-=1
        return total
    faces=polygonize(unary_union([LineString(ring) for ring in rings]))
    return unary_union([face for face in faces if winding(face.representative_point())!=0])


def outline_entities(entity,matrix,kind,fonts,tolerance,budget):
    """Yield final-drawing-coordinate virtual HATCH/closed polylines and metadata.

    Outlines use cmap plus horizontal advances, cap-height metrics, and ezdxf's
    declared single-line alignment/OCS transform. No GSUB/GPOS or CAD renderer
    equivalence is implied. TrueType nonzero fill is evaluated per glyph before
    combining glyph surfaces, retaining overlaps and holes.
    """
    if entity.dxftype()=='MTEXT' or getattr(entity,'has_embedded_mtext_entity',False):
        raise BridgeError('DXF_MTEXT_OUTLINE: rich/multiline layout requires a separate verified workflow')
    if kind not in {'line','polygon'}:raise BridgeError('DXF_TEXT_OUTLINE_KIND: choose line contours or polygon glyph surfaces')
    if tolerance is None:raise BridgeError('DXF_CURVE_TOLERANCE: font curves require explicit final drawing-unit flattening tolerance')
    raw=entity.dxf.text
    if '%<' in raw or re.search(r'%%[uok]',raw,re.I) or '\\' in re.sub(r'\\U\+[0-9a-fA-F]{4,8}','',raw):
        raise BridgeError('DXF_TEXT_FORMAT: decorated/field/rich text is not a plain single-line outline')
    text=entity.plain_text()
    if not text.strip():return
    if len(text)>2048:raise BridgeError('DXF_TEXT_LENGTH: at most2048Unicodecodepoints per entity')
    if any(unicodedata.category(c) in {'Mn','Mc','Me','Cf','Cc'} or unicodedata.bidirectional(c) in {'R','AL','AN'} for c in text):
        raise BridgeError('DXF_TEXT_SHAPING: contextual/bidi/combining layout requires a shaping engine')
    binding_and_font=fonts.get(entity.dxf.style.casefold())
    if binding_and_font is None:raise BridgeError('DXF_FONT_BINDING: explicitly bind the source text style '+entity.dxf.style)
    binding,renderer=binding_and_font
    missing=sorted({ord(c) for c in text if ord(c) not in renderer.cmap})
    if missing:raise BridgeError('DXF_TEXT_GLYPH: bound font lacks '+','.join(f'U+{code:04X}' for code in missing))
    if any(not c.isspace() and not renderer.get_glyph_path(c) for c in text):
        raise BridgeError('DXF_TEXT_GLYPH: a nonblank character has no outline; no invisible replacement')
    dxf=entity.dxf
    values=(dxf.height,dxf.width,dxf.rotation,dxf.oblique,*dxf.insert,*(dxf.align_point or ()))
    if not all(math.isfinite(v) for v in values) or dxf.height<=0 or dxf.width<=0 or abs(dxf.oblique)>=85:
        raise BridgeError('DXF_TEXT_TRANSFORM: finite positive size/width and oblique angle below85degrees required')
    if dxf.halign not in range(6) or dxf.valign not in range(4) or dxf.text_generation_flag&~6:
        raise BridgeError('DXF_TEXT_ALIGNMENT: unknown alignment/generation flags')
    glyphs=renderer.get_text_glyph_paths(text,dxf.height)
    path=renderer.get_text_path(text,dxf.height).to_path()
    if not glyphs or not path:raise BridgeError('DXF_TEXT_GLYPH: nonblank text has no usable outlines')
    align=entity.get_align_enum();length=entity.fit_length()
    if align.name in {'FIT','ALIGNED'} and length<=0:raise BridgeError('DXF_TEXT_ALIGNMENT: two distinct placement points required')
    align_matrix=alignment_transformation(renderer.font_measurements.scale_from_baseline(dxf.height),
        cad_path.bbox([path],fast=True),align,length)
    transform=Matrix44.chain(align_matrix,entity.wcs_transformation_matrix(),matrix)
    surfaces=[_filled_glyph(glyph.to_path().transform(transform),tolerance,budget) for glyph in glyphs]
    geometry=unary_union(surfaces)
    if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in {'Polygon','MultiPolygon'}:
        raise BridgeError('DXF_TEXT_GLYPH: invalid filled outline')
    polygons=sorted(list(geometry.geoms) if geometry.geom_type=='MultiPolygon' else [geometry],key=lambda g:(g.bounds,g.wkb_hex))
    metadata={'source_text':text,'font_sha256':binding.expected_sha256,'font_face_index':binding.face_index,
        'source_text_style':entity.dxf.style,'text_layout':'single-line cmap/horizontaladvance; noGSUB/GPOS/kerning',
        'text_alignment':align.name}
    for index,polygon in enumerate(polygons):
        if kind=='polygon':
            hatch=factory.new('HATCH');hatch.paths.add_polyline_path(polygon.exterior.coords,is_closed=True,flags=1)
            for ring in polygon.interiors:hatch.paths.add_polyline_path(ring.coords,is_closed=True,flags=0)
            yield hatch,{**metadata,'text_part':index+1}
        else:
            for ring_index,ring in enumerate([polygon.exterior,*polygon.interiors]):
                polyline=factory.new('LWPOLYLINE');polyline.set_points(ring.coords,format='xy');polyline.closed=True
                yield polyline,{**metadata,'text_part':index+1,'text_ring':ring_index}
