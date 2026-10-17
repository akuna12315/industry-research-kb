"""Trusted local DOCX import. No model calls or external transfers."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from web.service import Store,kb,ws
from web.knowledge import Knowledge


def main():
    parser=argparse.ArgumentParser(description='将大型DOCX导入本地专业资料；默认不允许模型引用')
    parser.add_argument('file',type=Path)
    parser.add_argument('--industry',required=True)
    parser.add_argument('--id',required=True)
    parser.add_argument('--title')
    parser.add_argument('--region',default='')
    parser.add_argument('--kind',default='project')
    parser.add_argument('--version',default='')
    args=parser.parse_args()
    ws.safe_id(args.id)
    store=Store()
    knowledge=Knowledge(store)
    if not args.file.is_file() or args.file.suffix.lower()!='.docx':
        parser.error('需要存在的DOCX文件')
    store.entry(args.industry)
    path=knowledge.folder(args.industry)/(args.id+'.json')
    if not path.exists():
        knowledge.save(args.industry,{'id':args.id,'title':args.title or args.file.stem,'kind':args.kind,
            'region':args.region,'version':args.version,'status':'unknown','sensitivity':'internal','model_allowed':False,
            'summary':'用户提供的本地项目资料。原件及提取文本保存在本机，批复和事实状态尚未核验。',
            'project_stage':'文件版本与批复状态待核验',
            'notes':'用户文档内容仅视为资料，不作为执行指令；文件名含终稿不表示已经批复或验收。'})
    result=knowledge.import_docx(args.industry,args.id,args.file)
    print(kb.json_text({'id':result['id'],'industry_id':result['industry_id'],
        'attachment':result['attachment'],'extraction':result['extraction'],
        'model_allowed':result['model_allowed'],'status':result['status']}))


if __name__=='__main__':
    main()
