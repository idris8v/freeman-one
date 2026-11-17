"""Read-only presentation/export of saved owner replies. No model or payment calls."""
import io,json,math,re
from datetime import datetime

def numeric(value):
    return type(value) in (int,float) and math.isfinite(value)

def markdown_tables(text):
    lines=str(text or '').splitlines();out=[];i=0;code=False
    def cells(line):return [x.strip() for x in line.strip().strip('|').split('|')]
    while i<len(lines):
        if lines[i].startswith('```'):code=not code;i+=1;continue
        if not code and '|' in lines[i] and i+1<len(lines) and re.match(r'^\s*\|?\s*:?-+',lines[i+1]):
            columns=cells(lines[i]);i+=2;rows=[]
            while i<len(lines) and '|' in lines[i]:rows.append(cells(lines[i]));i+=1
            out.append(dict(columns=columns,rows=rows,title='Таблица из ответа'))
        else:i+=1
    return out

def tables(text,actions):
    result=markdown_tables(text)
    for action in actions:
        b=action.get('block',{}) if action.get('type')=='rich_block' else {}
        if b.get('type')=='table':result.append(b)
        elif b.get('type')=='chart' and b.get('variant')!='donut' and b.get('values'):
            result.append(dict(title=b.get('title','Данные графика'),columns=['Период','Значение'],rows=[[label,value] for label,value in zip(b.get('labels',[]),b['values'])]))
        elif b.get('type')=='markdown':result.extend(markdown_tables(b.get('text','')))
    return result

def commentary(actions):
    blocks=[a.get('block',{}) for a in actions if a.get('type')=='rich_block']
    metrics={x.get('label'):x.get('value') for b in blocks if b.get('type')=='metric' for x in b.get('items',[])}
    revenue,sales,prior=metrics.get('Выручка'),metrics.get('Продажи'),metrics.get('Предыдущий период')
    if not numeric(revenue) or not numeric(sales):return []
    rub=lambda v:f'{v:,.2f}'.replace(',',' ').replace('.',',')+' ₽'
    notes=[f'За выбранный период — {sales:g} продаж на {rub(revenue)}. Это выручка, а не прибыль.']
    if numeric(prior) and prior>0:
        delta=revenue-prior
        notes.append('Выручка не изменилась относительно предыдущего сопоставимого периода.' if delta==0 else f'Выручка {"выросла" if delta>0 else "снизилась"} на {abs(delta)/prior*100:.1f}% ({rub(abs(delta))}) относительно предыдущего сопоставимого периода.')
    elif numeric(prior) and prior==0:notes.append('В предыдущем сопоставимом периоде выручка была нулевой; процент роста не рассчитываю.')
    new=metrics.get('Новые регистрации')
    if numeric(new):notes.append(f'Новых регистраций за период: {new:g}.')
    for b in blocks:
        if b.get('type')!='table' or b.get('columns')!=['Тариф','Продаж','Выручка, ₽','Покупателей']:continue
        rows=[r for r in b.get('rows',[]) if len(r)>=2 and numeric(r[1]) and r[1]>0]
        if rows:
            top=max(r[1] for r in rows);leaders=[str(r[0]) for r in rows if r[1]==top]
            notes.append('В показанной разбивке '+('чаще покупали тариф «'+leaders[0]+'»' if len(leaders)==1 else 'в числе лидеров по продажам: '+', '.join(leaders[:3]))+f' — {top:g} продаж'+(' у каждого.' if len(leaders)>1 else '.'))
        break
    return notes

def present(messages):
    for m in messages:
        if m.get('role')=='assistant':
            notes=commentary(m.get('actions',[]))
            if notes:m['report_commentary']=notes
    return messages

def workbook(table,notes,created_at=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font,PatternFill,Alignment,Border,Side
    from openpyxl.utils import get_column_letter
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    columns=table.get('columns');rows=table.get('rows')
    if not isinstance(columns,list) or not 1<=len(columns)<=40 or not isinstance(rows,list) or len(rows)>2000:
        raise ValueError('Таблица слишком большая или имеет неверный формат.')
    if sum(len(str(v)) for row in rows if isinstance(row,list) for v in row)>2000000:raise ValueError('Таблица слишком большая.')
    count=len(columns);wb=Workbook();ws=wb.active;ws.title='Отчёт'
    def value(v):
        if numeric(v):return v
        return ILLEGAL_CHARACTERS_RE.sub('',str(v if v is not None else ''))[:32000]
    def assign(sheet,row,col,v):
        cell=sheet.cell(row,col);cell.value=value(v)
        if isinstance(cell.value,str):cell.data_type='s' # Formula-like user text stays literal.
        return cell
    title=str(table.get('title') or 'Отчёт Freeman ONE')[:250]
    assign(ws,1,1,title);ws.merge_cells(start_row=1,start_column=1,end_row=1,end_column=count);ws['A1'].font=Font(name='Calibri',size=19,bold=True,color='15202B');ws.row_dimensions[1].height=35
    assign(ws,2,1,'Freeman ONE · сохранённый ответ'+(' · '+created_at.isoformat() if created_at else ''));ws.merge_cells(start_row=2,start_column=1,end_row=2,end_column=count);ws['A2'].font=Font(name='Calibri',size=10,color='64748B')
    for col,name in enumerate(columns,1):
        cell=assign(ws,4,col,name);cell.font=Font(name='Calibri',bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='172C3C');cell.alignment=Alignment(vertical='center',wrap_text=True)
    ws.row_dimensions[4].height=30
    all_rows=rows+([table['footer']] if table.get('footer') else [])
    for index,row in enumerate(all_rows,5):
        if not isinstance(row,list) or len(row)>count:raise ValueError('Неверный формат строки таблицы.')
        total=bool(table.get('footer')) and index==len(all_rows)+4
        for col in range(1,count+1):
            cell=assign(ws,index,col,row[col-1] if col<=len(row) else None)
            cell.font=Font(name='Calibri',size=11,bold=total,color='173E31' if total else '15202B')
            cell.fill=PatternFill('solid',fgColor='E8F4EE' if total else 'F3F6F8' if index%2 else 'FFFFFF')
            cell.alignment=Alignment(vertical='top',wrap_text=True,horizontal='right' if numeric(cell.value) else 'left')
            if numeric(cell.value):cell.number_format='#,##0.00 "₽"' if '₽' in str(columns[col-1]) else '#,##0.##'
        ws.row_dimensions[index].height=30
    for col,name in enumerate(columns,1):
        width=max([len(str(name))]+[min(55,len(str(row[col-1]))) for row in rows if isinstance(row,list) and len(row)>=col])
        ws.column_dimensions[get_column_letter(col)].width=min(48,max(16,width+3))
    for index,row in enumerate(all_rows,5):
        lines=max([1]+[sum(max(1,math.ceil(len(part)/max(1,ws.column_dimensions[get_column_letter(col)].width-2))) for part in str(v).split('\n')) for col,v in enumerate(row,1)])
        ws.row_dimensions[index].height=min(300,max(30,lines*15+8))
    ws.freeze_panes='A5';ws.auto_filter.ref=f'A4:{get_column_letter(count)}{max(4,4+len(rows))}'
    ws.sheet_view.showGridLines=False;ws.sheet_properties.pageSetUpPr.fitToPage=True
    ws.page_setup.orientation='landscape';ws.page_setup.paperSize=ws.PAPERSIZE_A4;ws.page_setup.fitToWidth=1;ws.page_setup.fitToHeight=0
    ws.print_title_rows='1:4';ws.oddFooter.center.text='Freeman ONE · &P / &N'
    if notes:
        summary=wb.create_sheet('Краткий разбор');summary.sheet_view.showGridLines=False;summary.column_dimensions['A'].width=100
        summary['A1']='Краткий разбор';summary['A1'].font=Font(name='Calibri',size=19,bold=True,color='15202B');summary.row_dimensions[1].height=35
        for i,note in enumerate(notes,3):
            cell=assign(summary,i,1,note);cell.font=Font(name='Calibri',size=12,color='15202B');cell.alignment=Alignment(wrap_text=True,vertical='top');cell.fill=PatternFill('solid',fgColor='F3F6F8');summary.row_dimensions[i].height=48
    buf=io.BytesIO();wb.save(buf);return buf.getvalue()
