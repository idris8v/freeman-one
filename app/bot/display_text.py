"""Presentation-only protocol wording; identifiers, URLs and copied values stay intact."""
import re
from pydantic import BaseModel

def wording(value):return re.sub(r'\bVLESS\b(?!://)','VPN',value,flags=re.I)

def html(value):
    parts=re.split(r'(<code\b[^>]*>.*?</code>|<pre\b[^>]*>.*?</pre>|<[^>]+>)',value,flags=re.S|re.I)
    return ''.join(p if p.startswith('<') else re.sub(r'\bVLESS\b(?!://)','VPN',p,flags=re.I) for p in parts)

def rich(value,text=False):
    if isinstance(value,str):return html(value) if text else value
    if isinstance(value,list):return [rich(v,text) for v in value]
    if isinstance(value,dict):
        if value.get('type') in ('code','pre','preformatted','button') or 'copy_text' in value:return value
        return {k:rich(v,k in ('text','summary','alternative_text','html') or text and isinstance(v,list)) if k in ('text','summary','alternative_text','html','blocks','items','cells','caption') else v for k,v in value.items()}
    if isinstance(value,BaseModel):
        if getattr(value,'type',None) in ('code','pre','preformatted','button') or hasattr(value,'copy_text'):return value
        changed={k:rich(getattr(value,k),k in ('text','summary','alternative_text','html')) for k in type(value).model_fields if k in ('text','summary','alternative_text','html','blocks','items','cells','caption')}
        return value.model_copy(update=changed)
    return value

def clean(screen):
    screen.text=html(screen.text)
    screen.rows=[[(wording(label),target) for label,target in row] for row in screen.rows]
    if screen.rich:screen.rich=rich(screen.rich)
    return screen
