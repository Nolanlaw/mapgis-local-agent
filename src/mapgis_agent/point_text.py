"""Native annotation data is a length-delimited GBK buffer, not a screenshot."""
import math,struct

from .core import BridgeError,annotation_text,native_ids,read_native


def prepare_point_text(parameters,reader):
    updates=parameters.get('updates') if isinstance(parameters,dict) else None
    if (reader.shapeType!='POINT' or not isinstance(parameters,dict) or set(parameters)!={'updates'}
        or not isinstance(updates,list) or not 1<=len(updates)<=1000):raise BridgeError('POINT_TEXT_UPDATES')
    kinds=dict(zip(native_ids(reader),reader.point_params['point_type']));seen=set();result=[];size=0
    for update in updates:
        if not isinstance(update,dict) or set(update)!={'record_id','text'}:raise BridgeError('POINT_TEXT_UPDATES')
        identity=update['record_id'];text=update['text']
        if type(identity) is not int or identity not in kinds or identity in seen or kinds[identity]!=0:raise BridgeError('POINT_TEXT_IDS: unique active annotation0 physical IDs required')
        if not isinstance(text,str) or not text or '\x00' in text:raise BridgeError('POINT_TEXT_BODY: nonempty text without NUL; delete the annotation to remove it')
        try:encoded=text.encode('gbk',errors='strict')
        except UnicodeEncodeError as error:raise BridgeError('POINT_TEXT_ENCODING: this native annotation requires representable GBK') from error
        if len(encoded)>32767:raise BridgeError('POINT_TEXT_LENGTH: signed native data length at most32767 bytes; no truncation')
        size+=len(encoded)
        if size>1048576:raise BridgeError('POINT_TEXT_BATCH_BYTES: split an explicit batch larger than1MiB')
        seen.add(identity);result.append({'record_id':identity,'text':text})
    return {'updates':sorted(result,key=lambda u:u['record_id']),'point_text_protocol':1}


def native_commands(parameters):
    return ' '.join(word for update in parameters['updates'] for word in
        (str(update['record_id']),update['text'].encode('gbk',errors='strict').hex()))


def validate_saved_texts(source,candidate,parameters):
    before,_,old_sections=read_native(source);after,_,new_sections=read_native(candidate);checked=[]
    for update in parameters['updates']:
        identity=update['record_id'];old=old_sections[0][0]+identity*93;new=new_sections[0][0]+identity*93
        if before[old+7:old+93]!=after[new+7:new+93]:raise BridgeError('POINT_TEXT_SAVED_POSITION_OR_GRAPHICS_DIFF')
        text=annotation_text(after,new_sections,identity)
        if text!=update['text']:raise BridgeError('POINT_TEXT_SAVED_BODY_DIFF')
        bounds=struct.unpack_from('<4d',after,new_sections[3][0]+identity*32)
        if not all(math.isfinite(v) for v in bounds) or bounds[0]>bounds[2] or bounds[1]>bounds[3]:raise BridgeError('POINT_TEXT_SAVED_BOUNDS_INVALID')
        checked.append({'record_id':identity,'saved_gbk_bytes':len(text.encode('gbk')),'native_bounds':list(bounds)})
    return {'native_annotation_content_verified':True,'native_annotation_updates':checked,
        'annotation_policy':'Exact GBK text including literal formatting markers; original XYZ/62graphic bytes retained, native rectangle recalculated'}
