"""Owner-only encrypted JPEG/PNG/WebP images; no arbitrary remote fetches."""
import base64,re
from sqlalchemy import select
from app.database.models import AIOwnerImage
from .security import encrypt,decrypt

def validate(raw):
 match=re.fullmatch(r'data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)',raw)
 if not match:raise ValueError('Только JPEG, PNG или WebP.')
 try:data=base64.b64decode(match[2],validate=True)
 except Exception:raise ValueError('Некорректное изображение.')
 if len(data)>1100000:raise ValueError('Изображение слишком большое.')
 valid=(match[1]=='jpeg' and data.startswith(b'\xff\xd8\xff')) or (match[1]=='png' and data.startswith(b'\x89PNG\r\n\x1a\n')) or (match[1]=='webp' and data.startswith(b'RIFF') and data[8:12]==b'WEBP')
 if not valid:raise ValueError('Формат файла не совпадает с изображением.')
 return raw
async def store(s,settings,message_id,values):
 if not values:return
 existing=await s.scalar(select(AIOwnerImage.id).where(AIOwnerImage.message_id==message_id))
 if existing:return
 for value in values:s.add(AIOwnerImage(message_id=message_id,encrypted_data=encrypt(settings,validate(value))))
async def model_images(s,settings,message_id):
 rows=await s.scalars(select(AIOwnerImage).where(AIOwnerImage.message_id==message_id).limit(4))
 return [{'type':'input_image','image_url':decrypt(settings,x.encrypted_data),'detail':'low'} for x in rows]
def budget_items(items):
 # Conservatively reserve 4096 tokens per resized low-detail image without counting base64 as text.
 return [{**x,'content':[dict(type='input_text',text='I'*4096) if p.get('type')=='input_image' else p for p in x['content']] if isinstance(x.get('content'),list) else x.get('content',x.get('output',''))} for x in items]
