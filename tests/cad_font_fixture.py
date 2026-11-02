"""A small original rectangular-glyph TTF with exact, independent geometry."""
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen


def make_font(path):
    builder=FontBuilder(1000,isTTF=True)
    names=['.notdef','space','A','x','p','geo','Q'];builder.setupGlyphOrder(names)
    builder.setupCharacterMap({32:'space',65:'A',81:'Q',120:'x',112:'p',ord('地'):'geo'})
    glyphs={}
    for name in names:
        pen=TTGlyphPen(None)
        if name=='Q':
            pen.moveTo((0,0));pen.qCurveTo((600,700),(600,0));pen.closePath()
        elif name!='space':
            bottom=-200 if name=='p' else 0;top=500 if name in {'x','p'} else 700
            for index,ring in enumerate([[(0,bottom),(600,bottom),(600,top),(0,top)]]+
                    ([[(100,100),(100,600),(500,600),(500,100)]] if name in {'A','geo'} else [])):
                pen.moveTo(ring[0])
                for point in ring[1:]:pen.lineTo(point)
                pen.closePath()
        glyphs[name]=pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name:(300 if name=='space' else 700,0) for name in names})
    builder.setupHorizontalHeader(ascent=800,descent=-200)
    builder.setupNameTable({'familyName':'MapGIS Exact Test Font','styleName':'Regular',
        'uniqueFontIdentifier':'MapGIS-Agent-Original-Rectangles-1','fullName':'MapGIS Exact Test Font',
        'psName':'MapGISExactTestFont'})
    builder.setupOS2(sTypoAscender=800,sTypoDescender=-200,usWinAscent=800,usWinDescent=200,sCapHeight=700,sxHeight=500)
    builder.setupPost();builder.setupMaxp();builder.save(path)
    return path
