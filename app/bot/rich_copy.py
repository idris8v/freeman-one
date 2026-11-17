"""Explicit client-side clipboard actions inside rich text values, not keyboard rows."""
from html.parser import HTMLParser
from aiogram.types import InputRichMessage,RichTextButton,RichMessageButton,CopyTextButton

class Content(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.parts=[];self.stack=[];self.code=None
    def append(self,item):
        (self.stack[-1][1] if self.stack else self.parts).append(item)
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='code':self.code=[];return
        if self.code is not None:return
        if tag=='br':self.append('\n');return
        if tag in ('b','strong','i','em','u','s','strike','a','tg-emoji'):
            self.stack.append((tag,[],attrs))
    def handle_endtag(self,tag):
        if tag=='code' and self.code is not None:
            value=''.join(self.code);self.code=None
            if value and len(value)<=256:
                self.append(RichTextButton(button=RichMessageButton(text=value,copy_text=CopyTextButton(text=value))))
            else:self.append(dict(type='code',text=value))
            return
        if self.code is not None:return
        if self.stack and self.stack[-1][0]==tag:
            name,text,attrs=self.stack.pop()
            if name=='tg-emoji':self.append(dict(type='custom_emoji',custom_emoji_id=attrs.get('emoji-id',''),alternative_text=''.join(str(x) for x in text)))
            else:
                kind={'b':'bold','strong':'bold','i':'italic','em':'italic','u':'underline','s':'strikethrough','strike':'strikethrough','a':'url'}[name]
                entity=dict(type=kind,text=text)
                if name=='a':entity['url']=attrs.get('href','')
                self.append(entity)
    def handle_data(self,data):
        if self.code is not None:self.code.append(data)
        else:self.append(data)

def text_blocks(html):
    import re
    blocks=[]
    for part in re.split(r'(<blockquote>.*?</blockquote>)',html,flags=re.S):
        if not part:continue
        if part.startswith('<blockquote>'):
            blocks.append(dict(type='blockquote',blocks=text_blocks(part[len('<blockquote>'):-len('</blockquote>')])))
            continue
        parser=Content();parser.feed(part);parser.close()
        # Adjacent paragraph blocks can be packed tightly by mobile clients.
        # Keep explicit line breaks in RichText, including those around copy values.
        if parser.parts and any(not isinstance(item,str) or item.strip() for item in parser.parts):
            blocks.append(dict(type='paragraph',text=parser.parts))
    return blocks


def copyable_rich(screen,media):
    blocks=[]
    for item in media:
        kind=item.media.type
        value=item.media
        if kind=='animation':
            from aiogram.types import InputMediaVideo
            kind='video';value=InputMediaVideo(media=value.media)
        blocks.append(dict(type=kind,**{kind:value}))
    blocks.extend(text_blocks(screen.text))
    return InputRichMessage(blocks=blocks,skip_entity_detection=True)
