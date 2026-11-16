"""CPU OCR with bounded boxes and an optional ruled-table reconstruction."""
import io
import threading

_engine=None
_lock=threading.RLock()


def recognize(png):
    global _engine
    import numpy as np
    from PIL import Image
    from rapidocr import RapidOCR
    with _lock:
        if _engine is None:
            _engine=RapidOCR(params={'EngineConfig.onnxruntime.intra_op_num_threads':4,
                'EngineConfig.onnxruntime.inter_op_num_threads':1,'EngineConfig.onnxruntime.use_cuda':False,
                'Global.max_side_len':2000,'Global.log_level':'warning'})
        result=_engine(np.array(Image.open(io.BytesIO(png)).convert('RGB')))
    texts=list(result.txts) if result.txts is not None else []
    boxes=result.boxes.tolist() if result.boxes is not None else []
    scores=result.scores.tolist() if hasattr(result.scores,'tolist') else list(result.scores or [])
    return {'lines':[{'id':f'ocr-{i+1}','text':text,'bbox':box,'confidence':float(score)} for i,(text,box,score) in enumerate(zip(texts,boxes,scores))],
        'text':'\n'.join(texts),'method':'RapidOCR CPU','quality':'ocr_needs_review'}


def ruled_tables(png,ocr,table_id='image-table-1'):
    import cv2
    import numpy as np
    from .table_structure import table_from_cells
    image=cv2.imdecode(np.frombuffer(png,dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
    binary=cv2.threshold(image,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    height,width=image.shape
    horizontal=cv2.morphologyEx(binary,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(max(20,width//12),1)))
    vertical=cv2.morphologyEx(binary,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(1,max(20,height//12))))
    def positions(projection,minimum):
        hits=np.where(projection>minimum)[0].tolist();groups=[]
        for hit in hits:
            if not groups or hit-groups[-1][-1]>3:groups.append([hit])
            else:groups[-1].append(hit)
        return [int(sum(g)/len(g)) for g in groups]
    ys=positions((horizontal>0).sum(axis=1),width*.35)
    xs=positions((vertical>0).sum(axis=0),height*.35)
    if not 3<=len(xs)<=31 or not 3<=len(ys)<=101:return []
    cells=[]
    for row,(top,bottom) in enumerate(zip(ys,ys[1:]),1):
        for column,(left,right) in enumerate(zip(xs,xs[1:]),1):
            lines=[]
            for line in ocr['lines']:
                box=line['bbox'];cx=sum(p[0] for p in box)/4;cy=sum(p[1] for p in box)/4
                if left<cx<right and top<cy<bottom:lines.append(line)
            lines.sort(key=lambda l:(l['bbox'][0][1],l['bbox'][0][0]))
            cells.append({'row':row,'column':column,'text':' '.join(l['text'] for l in lines),
                'source':{'format':'ocr_grid','bbox':[left,top,right,bottom],'ocr_line_ids':[l['id'] for l in lines],
                    'confidence':min((l['confidence'] for l in lines),default=None)}})
    above=[line['text'] for line in ocr['lines'] if sum(p[1] for p in line['bbox'])/4<ys[0]]
    below=[line['text'] for line in ocr['lines'] if sum(p[1] for p in line['bbox'])/4>ys[-1]]
    table=table_from_cells(cells,' '.join(above) or '图片有线表格',notes=below,
        source={'format':'ocr_grid','quality':'ocr_structure_needs_review'},table_id=table_id)
    table['quality']='ocr_structure_needs_review'
    return [table]
