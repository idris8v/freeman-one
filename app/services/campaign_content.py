"""Preserve Telegram entities, media and native Rich Message in broadcasts."""
from aiogram.types import InputRichMessage
from app.services.screens import Screen

def rich_input(value):
    def convert(node):
        if isinstance(node,list):return [convert(x) for x in node]
        if not isinstance(node,dict):return node
        result={k:convert(v) for k,v in node.items()}
        for kind in ('photo','video','animation','audio','document','voice_note'):
            media=node.get(kind)
            if isinstance(media,list) and media and isinstance(media[-1],dict) and 'file_id' in media[-1]:
                result[kind]={'type':'photo','media':media[-1]['file_id']}
            elif isinstance(media,dict) and 'file_id' in media:
                result[kind]={'type':'voice' if kind=='voice_note' else kind,'media':media['file_id']}
        return result
    return InputRichMessage.model_validate(convert(value.model_dump(exclude_none=True)))

def capture(message,photo=None):
 rich=getattr(message,'rich_message',None)
 rich=rich_input(rich) if rich else None
 if rich and photo:
  rich.blocks.insert(0,__import__('aiogram.types',fromlist=['InputRichBlockPhoto']).InputRichBlockPhoto(photo=__import__('aiogram.types',fromlist=['InputMediaPhoto']).InputMediaPhoto(media=photo)))
 return dict(source_chat=message.chat.id,source_message=message.message_id,
  rich=rich.model_dump(mode='json',exclude_none=True,exclude_unset=True) if rich else None,
  html=message.html_text or '',
  photo=photo or (message.photo[-1].file_id if message.photo else None))

async def load(repo,campaign_id):
 from app.web.posts import load_json
 return await load_json(repo,f'campaign_content:{campaign_id}')

def as_rich(data):
 if data.get('rich'):return InputRichMessage.model_validate(data['rich'])
 from app.bot.rich_copy import text_blocks
 blocks=text_blocks(data.get('html',''))
 if data.get('photo'):blocks.insert(0,dict(type='photo',photo=dict(type='photo',media=data['photo'])))
 return InputRichMessage(blocks=blocks,skip_entity_detection=True)

async def preview(repo,item):
 from app.services.campaign_buttons import data as buttons,rows,options
 value=await buttons(repo,item.id);keyboard=rows(value)+[[('← К рассылке',f'admin:x:campaign:{item.id}')]]
 data=await load(repo,item.id)
 if data:
  rich=as_rich(data)
  return Screen(data.get('html') or 'Rich Message',keyboard,button_options=options(value),photo=data.get('photo'),rich=rich)
 return Screen(item.text,keyboard,button_options=options(value),photo=item.photo,native_text=True)

async def send(bot,chat_id,data):
 from app.bot.keyboards import keyboard
 markup=keyboard(data.get('buttons',[]),data.get('button_options',{})) if data.get('buttons') else None
 await bot.send_rich_message(chat_id,as_rich(data),reply_markup=markup)
