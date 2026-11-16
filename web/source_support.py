"""Source-owned quotations and locations for grounded answers; no generated facts."""
import html
import re
import unicodedata
from decimal import Decimal

from pydantic import BaseModel, Field


class AnswerClaim(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    support_ids: list[str] = Field(min_length=1, max_length=12)


class GroundedAnswer(BaseModel):
    insufficient: bool
    claims: list[AnswerClaim] = Field(default_factory=list, max_length=10)
    limitation: str = Field(default='', max_length=400)


class GroundingError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(code + ': ' + message)


HTML = re.compile(r'<!--.*?-->|</?[A-Za-z][A-Za-z0-9:._-]*(?:\s[^<>]*?)?\s*/?>|&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);|.', re.S)
PUNCT = str.maketrans({'，':',','。':'.','；':';','：':':','！':'!','？':'?','（':'(', '）':')','“':'"','”':'"','‘':"'",'’':"'"})


def normalized_map(text):
    """Normalize display syntax while retaining every non-formatting source character."""
    chars, offsets = [], []
    for match in HTML.finditer(text):
        value = match.group()
        if value.startswith('<') and len(value)>1:
            continue
        value = html.unescape(value)
        for character in unicodedata.normalize('NFKC',value).translate(PUNCT):
            if not character.isspace():
                chars.append(character)
                offsets.append((match.start(),match.end()))
    return ''.join(chars), offsets


def resolve_quote(quote, body):
    """Return one real contiguous raw span, never concatenate selected sentences."""
    if not isinstance(quote,str) or not quote:
        raise GroundingError('empty_quote','引用为空')
    start=body.find(quote)
    if start>=0:
        return {'quote':quote,'start':start,'stop':start+len(quote),'mapping':'exact'}
    query,_=normalized_map(quote)
    source,offsets=normalized_map(body)
    at=source.find(query) if query else -1
    if at<0:
        raise GroundingError('non_contiguous_quote','引用不是同一连续原文；不能省略中间句子或改写事实')
    start,stop=offsets[at][0],offsets[at+len(query)-1][1]
    return {'quote':body[start:stop],'start':start,'stop':stop,'mapping':'format_mapped'}


def display_text(text):
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'</?[A-Za-z][^<>]*>',' ',text))).strip()


def catalog(sources):
    """Input sources contain ref/text/citation; each unit maps directly into that source."""
    result=[]
    for source in sources:
        body=source['text']
        marker=re.compile(r'【([^】\n]{1,180})】\s*\n')
        cursor=0; location=''; pieces=[]
        for match in marker.finditer(body):
            if match.start()>cursor:
                pieces.append((cursor,match.start(),location))
            location=match[1];cursor=match.end()
        if cursor<len(body):
            pieces.append((cursor,len(body),location))
        number=0
        for first,last,location in pieces:
            if body[first:last].lstrip().startswith('结构化表格行'):
                # Keep row identity, hierarchical headers, values and footnotes
                # together. Splitting at newlines would detach a value from its row.
                spans=[(first,last)]
            else:
                spans=[(first+m.start(),first+m.end()) for m in re.finditer(r'[^。！？!?\n]+[。！？!?]?|\n',body[first:last])]
            # Short original sentences preserve negation and local conditions.
            for start,stop in spans:
                while start<stop and body[start].isspace():start+=1
                while stop>start and body[stop-1].isspace():stop-=1
                size=max(420,stop-start) if body[first:last].lstrip().startswith('结构化表格行') else 420
                for a in range(start,stop,size):
                    b=min(a+size,stop)
                    quote=body[a:b]
                    if not display_text(quote):continue
                    number+=1
                    citation=source['citation']
                    cm=re.fullmatch(r'(.+#(?:block-\d+-)?chars-)(\d+)-(\d+)',citation)
                    if not cm:
                        raise GroundingError('invalid_locator','来源缺少有效字符定位')
                    base=int(cm[2])-1
                    result.append({'id':source['ref']+'-S'+str(number),'ref':source['ref'],
                        'text':display_text(quote),'quote':quote,'start':a,'stop':b,'location':location,
                        'citation':cm[1]+str(base+a+1)+'-'+str(base+b),
                        'source_title':source.get('title','')})
    return result


def numbers(value):
    return {str(Decimal(n).normalize()) for n in re.findall(r'(?<!\d)[+-]?\d+(?:\.\d+)?',unicodedata.normalize('NFKC',value))}


def validate_answer(answer, units):
    lookup={u['id']:u for u in units}
    if not answer.insufficient and not answer.claims:
        raise GroundingError('missing_support','可回答结论必须有原文依据')
    if not answer.claims and not answer.limitation.strip():
        raise GroundingError('missing_limitation','依据不足须说明本次片段的具体缺口')
    if re.search(r'原文(?:完全|没有|未|不存在)|全库(?:没有|不存在)',answer.limitation):
        raise GroundingError('global_absence','覆盖结论限定本次命中片段，不声称全文或全库不存在')
    for claim in answer.claims:
        if any(key not in lookup for key in claim.support_ids):
            raise GroundingError('unknown_support','只能选择本轮已提供的支持片段标识')
        if re.search(r'段落\s*\d+|#(?:block|chars)-|\[M\d+\]',claim.text):
            raise GroundingError('generated_locator','不自行填写段落号或引用编号；定位由程序生成')
        support=' '.join(lookup[key]['quote'] for key in claim.support_ids)
        if all(re.search(r'[？?]|是不是|能否|吗',lookup[key]['quote']) for key in claim.support_ids) and not re.search(r'问|可能|不确定|不能确定|咨询|疑问',claim.text):
            raise GroundingError('question_as_fact','来源仅为提问，不能改写成已经发生或已确定事实')
        if not numbers(claim.text)<=numbers(support):
            raise GroundingError('unsupported_number','结论数字未出现在所选支持片段中')
        if re.search(r'原文(?:完全|没有|未|不存在)|全库(?:没有|不存在)',claim.text+answer.limitation):
            raise GroundingError('global_absence','覆盖结论限定本次命中片段，不声称全文或全库不存在')
    return answer


def render_answer(answer, units):
    validate_answer(answer,units)
    lookup={u['id']:u for u in units}
    used=[];lines=[]
    for claim in answer.claims:
        for key in claim.support_ids:
            if key not in used:used.append(key)
        refs=''.join('[M'+str(used.index(key)+1)+']' for key in dict.fromkeys(claim.support_ids))
        lines.append(claim.text+' '+refs)
    if answer.insufficient:
        lines.insert(0,'本次命中片段不足以完整确定答案。')
    if answer.limitation.strip():lines.append(answer.limitation.strip())
    lines.append('\n资料依据与定位：')
    for n,key in enumerate(used,1):
        unit=lookup[key]
        lines.extend([f'\n[M{n}] {unit["source_title"]}'+(f' · {unit["location"]}' if unit['location'] else ''),
            '> '+display_text(unit['quote']),unit['citation']])
    return '\n\n'.join(lines)


def boundary_review(query, units):
    """Conservative source-first response when different bound directions conflict.

    No semantic assumption that different objects share a global maximum/minimum.
    Only activate for an explicit extremum question and matching quantity labels.
    """
    maximum=bool(re.search(r'最多|最大',query) or '上限' in query and '下限' not in query)
    minimum=bool(re.search(r'最少|最小',query) or '下限' in query and '上限' not in query)
    directions='上限' in query and '下限' in query
    if not maximum and not minimum and not directions:return None
    labels=('宽度','长度','高度','面积','体积','半径','直径','温度','速度','压力','浓度','金额','数量','密度','重量','功率','容量')
    metrics=[m for m in labels if m in query]
    if not metrics:return None
    relevant=[];upper={};lower={}
    for unit in units:
        text=unit['text']
        if not any(m in text for m in metrics):continue
        found=list(re.finditer(r'(不超过|不得超过|不大于|至多|最多|≤|不小于|不少于|至少|≥|加宽至)\s*(\d+(?:\.\d+)?)\s*(毫米|厘米|公里|平方米|立方米|千克|公斤|万元|米|亩|吨|元|%|％|兆帕|MPa|度|℃)',text))
        for match in found:
            preceding=[(text.rfind(label,0,match.start()),label) for label in labels]
            nearest=max(preceding)
            if nearest[0]>=0 and nearest[1] not in metrics:continue
            direction='lower' if match[1] in ('不小于','不少于','至少','≥') else 'upper'
            target=lower if direction=='lower' else upper
            target.setdefault(match[3],[]).append(float(match[2]))
        has_value=any(re.search(re.escape(m)+r'[^。！？]{0,40}\d',text) for m in metrics)
        if (found or has_value) and unit['quote'] not in [u['quote'] for u in relevant]:relevant.append(unit)
    conflict=any(unit in lower and max(lower[unit])>min(values) for unit,values in upper.items())
    if not conflict:return None
    # All text below is copied from real bounded spans; no inferred single bound.
    heading=('本次命中原文包含不同对象、条件或口径的上下限，不能直接概括为覆盖所有情况的统一'+('最大值。' if maximum else '最小值。')) if maximum or minimum else '以下按原文分别列出常规值、范围与上下限；不同对象和例外条件分别保留。'
    lines=[heading,'“不小于/至少”表示下限，“不超过/至多”表示上限；常规范围不能自动套用到例外对象。']
    for n,unit in enumerate(relevant,1):
        lines.extend([f'\n[M{n}] '+unit['source_title']+(f' · {unit["location"]}' if unit['location'] else ''),
            '> '+display_text(unit['quote']),unit['citation']])
    return '\n\n'.join(lines)
