"""Reproducible synthetic fixtures; never import these into a real industry."""
from pathlib import Path
import json
import zipfile

ROOT=Path(__file__).resolve().parents[1]

def make(folder):
    from PIL import Image,ImageDraw,ImageFont
    import openpyxl
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    (folder/'sales.csv').write_text('地区,2023销售额（万元）,2024销售额（万元）\n华东,100,120\n华南,80,\n',encoding='utf-8')
    book=openpyxl.Workbook();sheet=book.active;sheet.title='销售额（万元）'
    sheet.append(['地区','销售额（万元）',None]);sheet.append([None,'2023年','2024年'])
    sheet.merge_cells('A1:A2');sheet.merge_cells('B1:C1')
    sheet.append(['华东',100,120]);sheet.append(['华南',80,None]);sheet.append(['合计','=SUM(B3:B4)',None])
    sheet.append(['注：空白表示未披露，不等于0。'])
    book.save(folder/'sales.xlsx')
    available=next((p for p in (Path('C:/Windows/Fonts/msyh.ttc'),Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')) if p.is_file()),None)
    font=lambda n:ImageFont.truetype(str(available),n) if available else ImageFont.load_default(size=n)
    image=Image.new('RGB',(1200,650),'white');draw=ImageDraw.Draw(image)
    draw.text((60,30),'销售统计（单位：万元）',font=font(36),fill='black')
    xs=[60,400,740,1140];ys=[120,230,340,450]
    for x in xs:draw.line((x,120,x,450),fill='black',width=3)
    for y in ys:draw.line((60,y,1140,y),fill='black',width=3)
    for row,values in enumerate([['地区','2023年','2024年'],['华东','100','120'],['华南','80','']]):
        for col,value in enumerate(values):draw.text((xs[col]+28,ys[row]+30),value,font=font(34),fill='black')
    draw.text((60,500),'注：空白表示未披露，不等于0。',font=font(30),fill='black')
    image.save(folder/'sales-table.png');image.save(folder/'scanned-table.pdf','PDF',resolution=100)
    chart=Image.new('RGB',(1200,760),'white');d=ImageDraw.Draw(chart)
    d.text((60,25),'地区销售额对比（单位：万元）',font=font(36),fill='black')
    d.line((130,120,130,620,1120,620),fill='black',width=3)
    for value in (0,40,80,120):
        y=620-value*3.5;d.text((55,y-20),str(value),font=font(25),fill='black')
    for group,(label,values) in enumerate([('华东',(100,120)),('华南',(80,90))]):
        for year,value in enumerate(values):
            x=260+group*430+year*120;y=620-value*3.5
            d.rectangle((x,y,x+90,620),fill=['#276cb2','#d58024'][year])
            d.text((x+8,y-42),str(value),font=font(30),fill='black')
        d.text((300+group*430,640),label,font=font(30),fill='black')
    for i,label in enumerate(['2023年','2024年']):
        x=720+i*200;d.rectangle((x,80,x+28,108),fill=['#276cb2','#d58024'][i]);d.text((x+40,75),label,font=font(26),fill='black')
    chart.save(folder/'sales-chart.png')
    ns='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    def cell(text,span=1,merge=None):
        prop=(f'<w:gridSpan w:val="{span}"/>' if span>1 else '')+(f'<w:vMerge w:val="{merge}"/>' if merge else '')
        return f'<w:tc><w:tcPr>{prop}</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>'
    rows=[cell('地区',merge='restart')+cell('销售额（万元）',2),cell('',merge='continue')+cell('2023年')+cell('2024年'),cell('华东')+cell('100')+cell('120'),cell('华南')+cell('80')+cell('')]
    xml=f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>表1 销售统计</w:t></w:r></w:p><w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>'+''.join('<w:tr>'+r+'</w:tr>' for r in rows)+'</w:tbl><w:p><w:r><w:t>注：空白表示未披露，不等于0。</w:t></w:r></w:p></w:body></w:document>'
    with zipfile.ZipFile(folder/'sales.docx','w') as z:
        z.writestr('word/document.xml',xml)
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
    # Small native two-page PDF with an actual ruled table and a distinct second page.
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>']
    for page in range(2):
        pageid=3+page*2;streamid=pageid+1
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 7 0 R >> >> /Contents {streamid} 0 R >>'.encode())
        lines=['0.8 w']+[f'{x} 450 m {x} 650 l S' for x in [50,210,370,550]]+[f'50 {y} m 550 {y} l S' for y in [450,500,550,600,650]]
        for r,vals in enumerate([['Region','2023(kg)','2024(kg)'],['East','100',str(120+page)],['South','80','90'],['West','40','50']]):
            for c,val in enumerate(vals):lines.append(f'BT /F1 14 Tf {65+c*160} {620-r*50} Td ({val}) Tj ET')
        stream='\n'.join(lines).encode();objects.append(f'<< /Length {len(stream)} >>\nstream\n'.encode()+stream+b'\nendstream')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    output=bytearray(b'%PDF-1.4\n');offsets=[0]
    for i,obj in enumerate(objects,1):offsets.append(len(output));output.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    xref=len(output);output.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:output.extend(f'{offset:010d} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode());(folder/'native-table.pdf').write_bytes(output)
    (folder/'expected.json').write_text(json.dumps({'synthetic':True,'east_2024':120,'south_2024':None,'sum_2023':180,'chart_south_2024':90},ensure_ascii=False,indent=2),encoding='utf-8')
    return folder

if __name__=='__main__':print(make(ROOT/'work/document-fixtures'))
