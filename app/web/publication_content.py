"""Canonical restricted Quill document -> safe customer HTML. Never accepts raw HTML."""
import html
from urllib.parse import urlsplit
from fastapi import HTTPException
COLORS={'#0a84ff':'primary','#667085':'muted','#16864b':'success','#a96c13':'warning','#d64141':'danger'}
MEDIA_RE=r'[a-f0-9]{48}\.(jpg|png|webp|mp4|webm|mov)'
def valid_url(url):
    try:
        p=urlsplit(url)
        return p.scheme=='https' and bool(p.hostname) and not p.username and not p.password and not any(c.isspace() or ord(c)<32 for c in url)
    except ValueError:return False

def render_content(document,media):
    if not isinstance(document,dict) or set(document)!={'ops'} or not isinstance(document['ops'],list) or len(document['ops'])>5000:raise HTTPException(422,'Неверный документ редактора')
    names={m['name']:m for m in media};ops=[];lines=[];line='';plain=[];list_kind=None;out=[];chars=0
    def finish(attrs):
        nonlocal line,list_kind
        kind=attrs.get('list');tag='ol' if kind=='ordered' else 'ul' if kind=='bullet' else None
        if tag!=list_kind:
            if list_kind:out.append('</'+list_kind+'>')
            if tag:out.append('<'+tag+'>')
            list_kind=tag
        align=attrs.get('align');cl=' class="post-align-'+align+'"' if align in ('center','right') else ''
        block='li' if tag else 'h'+str(attrs['header']) if attrs.get('header') in (1,2,3) else 'blockquote' if attrs.get('blockquote') else 'pre' if attrs.get('code-block') else 'p'
        out.append('<'+block+cl+'>'+(line or '<br>')+'</'+block+'>');line=''
    for op in document['ops']:
        if not isinstance(op,dict) or 'insert' not in op or set(op)-{'insert','attributes'}:raise HTTPException(422,'Документ должен содержать только вставки')
        attrs=op.get('attributes') or {}
        if not isinstance(attrs,dict) or set(attrs)-{'bold','italic','underline','strike','link','color','header','list','blockquote','code-block','align','alt'}:raise HTTPException(422,'Неподдерживаемое форматирование')
        for k in ('bold','italic','underline','strike','blockquote'):
            if k in attrs and type(attrs[k]) is not bool:raise HTTPException(422,'Неверное форматирование')
        if 'code-block' in attrs and attrs['code-block'] is not True and attrs['code-block'] not in ('plain','javascript','python','json','bash'):raise HTTPException(422,'Неверный блок кода')
        if 'header' in attrs and (type(attrs['header']) is not int or attrs['header'] not in (1,2,3)):raise HTTPException(422,'Неверный заголовок')
        if 'list' in attrs and attrs['list'] not in ('ordered','bullet'):raise HTTPException(422,'Неверный список')
        if 'align' in attrs and attrs['align'] not in ('left','center','right'):raise HTTPException(422,'Неверное выравнивание')
        if 'color' in attrs and attrs['color'] not in COLORS:raise HTTPException(422,'Выберите цвет из палитры')
        if 'link' in attrs and (not isinstance(attrs['link'],str) or len(attrs['link'])>2048 or not valid_url(attrs['link'])):raise HTTPException(422,'Ссылка должна начинаться с HTTPS')
        value=op['insert']
        if isinstance(value,str):
            chars+=len(value)
            if chars>50000:raise HTTPException(422,'Текст публикации — до 50 000 символов')
            plain.append(value)
            parts=value.split('\n')
            for i,part in enumerate(parts):
                text=html.escape(part)
                for k,t in [('bold','strong'),('italic','em'),('underline','u'),('strike','s')]:
                    if attrs.get(k):text='<'+t+'>'+text+'</'+t+'>'
                if attrs.get('color'):text='<span class="post-color-'+COLORS[attrs['color']]+'">'+text+'</span>'
                if attrs.get('link'):text='<a href="'+html.escape(attrs['link'],quote=True)+'" target="_blank" rel="noopener noreferrer">'+text+'</a>'
                line+=text
                if i<len(parts)-1:finish(attrs)
        elif isinstance(value,dict) and set(value)=={'image'}:
            url=value['image'];name=url.removeprefix('/api/web/posts/media/') if isinstance(url,str) else ''
            if name not in names or names[name].get('kind')!='image':raise HTTPException(422,'Изображение должно быть загружено в эту публикацию')
            alt=names[name].get('alt','')
            line+='<img src="/api/web/posts/media/'+name+'" alt="'+html.escape(alt,quote=True)+'" loading="lazy">'
        elif isinstance(value,dict) and set(value)=={'button'}:
            b=value['button']
            if not isinstance(b,dict) or set(b)!={'text','url'} or not isinstance(b['text'],str) or not 1<=len(b['text'].strip())<=80 or not isinstance(b['url'],str) or len(b['url'])>2048 or not valid_url(b['url']):raise HTTPException(422,'У кнопки нужны название до 80 символов и безопасная HTTPS-ссылка')
            if line:finish({})
            if list_kind:out.append('</'+list_kind+'>');list_kind=None
            out.append('<p class="post-cta-row"><a class="post-cta" href="'+html.escape(b['url'],quote=True)+'" target="_blank" rel="noopener noreferrer">'+html.escape(b['text'].strip())+'</a></p>')
            plain.append('\n'+b['text'].strip()+'\n'+b['url']+'\n')
        else:raise HTTPException(422,'Неподдерживаемый блок')
        ops.append(op)
    if line:finish({})
    if list_kind:out.append('</'+list_kind+'>')
    if not ops or not isinstance(ops[-1]['insert'],str) or not ops[-1]['insert'].endswith('\n'):ops.append({'insert':'\n'})
    return {'ops':ops},''.join(out),''.join(plain).strip()
