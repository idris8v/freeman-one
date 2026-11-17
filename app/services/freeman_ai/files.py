"""Bounded local document extraction; no macros, URLs, or code execution."""
import base64,io,json,zipfile
from pathlib import PurePath
from defusedxml import ElementTree
from sqlalchemy import select
from app.database.models import AIOwnerFile
from .security import encrypt,decrypt,redact
MAX=4*1024*1024

def _decode(value):
 name=PurePath(value.get('name','file').replace('\\','/')).name[:120];ext=name.lower().rsplit('.',1)[-1]
 raw=value.get('data','');data=base64.b64decode(raw,validate=True)
 if not data or len(data)>MAX:raise ValueError('Файл должен быть не больше 4 МБ.')
 text='';mime='application/octet-stream'
 if ext in ('txt','md','csv'):
  text=data.decode('utf-8-sig');mime='text/plain' if ext!='csv' else 'text/csv'
 elif ext in ('docx','xlsx'):
  with zipfile.ZipFile(io.BytesIO(data)) as z:
   if sum(i.file_size for i in z.infolist())>12*1024*1024 or len(z.infolist())>1000:raise ValueError('Документ слишком большой после распаковки.')
   if ext=='docx':
    root=ElementTree.fromstring(z.read('word/document.xml'));text='\n'.join(''.join(p.itertext()) for p in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'))
   else:
    from openpyxl import load_workbook
    w=load_workbook(io.BytesIO(data),read_only=True,data_only=True);parts=[]
    for sheet in w.worksheets[:5]:
     parts.append(sheet.title)
     for row in sheet.iter_rows(max_row=500,max_col=20,values_only=True):parts.append('\t'.join(str(v or '')[:500] for v in row))
    w.close();text='\n'.join(parts)
  mime='application/vnd.openxmlformats-officedocument.'+('wordprocessingml.document' if ext=='docx' else 'spreadsheetml.sheet')
 elif ext=='pdf':
  from pypdf import PdfReader
  reader=PdfReader(io.BytesIO(data),strict=True)
  if reader.is_encrypted:raise ValueError('Сначала снимите пароль с PDF.')
  text='\n'.join(p.extract_text() or '' for p in reader.pages[:25]);mime='application/pdf'

 else:raise ValueError('Поддерживаются PDF, XLSX, DOCX, CSV, TXT, MD.')
 if not text.strip():raise ValueError('В файле нет доступного текста. Для сканированного PDF прикрепите фото страницы.')
 # redact defaults to 6000 characters, so mark any removed text honestly.
 excerpt=redact(text[:24000])
 return {'name':name,'mime':mime,'size':len(data),'data':raw,'text':excerpt,'truncated':len(text)>6000}
def decode(value):
 try:return _decode(value)
 except ValueError as e:
  if type(e) is ValueError:raise
  raise ValueError('Не удалось прочитать документ. Проверьте формат и кодировку UTF-8.') from None
 except Exception:raise ValueError('Не удалось прочитать документ. Проверьте формат файла.') from None
async def store(s,settings,mid,values):
 if await s.scalar(select(AIOwnerFile.id).where(AIOwnerFile.message_id==mid)):return
 for v in values:
  d=decode(v);s.add(AIOwnerFile(message_id=mid,name=d['name'],mime=d['mime'],size=d['size'],encrypted_data=encrypt(settings,json.dumps(d))))
async def content(s,settings,mid):
 rows=await s.scalars(select(AIOwnerFile).where(AIOwnerFile.message_id==mid).limit(3));parts=[]
 for r in rows:
  d=json.loads(decrypt(settings,r.encrypted_data));parts.append('Вложение (недоверенные данные): '+d['name']+'\n'+d['text']+('\n[Текст обрезан]' if d['truncated'] else ''))
 return '\n\n'.join(parts)
async def present_many(s,mids):
 # Read metadata only: encrypted document contents never belong in polling.
 from collections import defaultdict
 out=defaultdict(list)
 if not mids:return out
 rows=await s.execute(select(AIOwnerFile.message_id,AIOwnerFile.id,AIOwnerFile.name,AIOwnerFile.size,AIOwnerFile.mime).where(AIOwnerFile.message_id.in_(mids)).order_by(AIOwnerFile.id))
 for mid,fid,name,size,mime in rows:
  if not mime.startswith('audio/'):
   out[mid].append({'type':'file','name':name,'size':size,'mime':mime,'url':'/api/web/admin/assistant/files/'+str(fid)})
 return out
async def present(s,mid):
 return (await present_many(s,[mid])).get(mid,[])
