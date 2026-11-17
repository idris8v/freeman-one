"""Carry the key's entry point in callbacks, independently for every message."""
import re

def split(action):
    base,sep,origin=action.rpartition('~')
    return (base,origin) if sep and re.fullmatch(r'(?:h|s\d{1,7}|l\d{1,19}\.\d{1,7}|c\d{1,19}(?:\.(?:h|s\d{1,7}))?)',origin) else (action,'')

def route(action,origin):
    return action+'~'+origin if origin else action

def decorate(screen,origin,action):
    if screen is None:return screen
    p=action.split(':')[2:]
    if p and p[0]=='keys' and len(p)>2:
        for row in screen.rows:
            for i,(label,target) in enumerate(row):
                if target.startswith('admin:r:key:'):row[i]=(label,route(target,f'l{p[1]}.{p[2]}'))
    key_input=len(p)>2 and p[0]=='input' and p[1] in ('extend','name','limit','traffic','transfer')
    updated={}
    for row in screen.rows:
        for i,(label,target) in enumerate(row):
            original=target;base,existing=split(target)
            if label.startswith('←'):
                if key_input and base=='admin:r:home':base=f'admin:r:key:{p[2]}'
                if origin.startswith('c') and base.startswith('admin:r:keys:'):
                    client,_,parent=origin[1:].partition('.')
                    base=route(f'admin:keys:{client}',parent)
                if origin=='h' and base.startswith('admin:r:keys:'):base='home'
                if origin.startswith('s') and base.startswith('admin:r:keys:'):base='admin:search:results:'+origin[1:]
                elif origin.startswith('l') and base.startswith('admin:r:keys:'):
                    panel,offset=origin[1:].split('.');base=f'admin:r:keys:{panel}:{offset}'
            operation=base.split(':')[2] if base.startswith('admin:r:') else ''
            is_key_input=operation=='input' and any(base.startswith('admin:r:input:'+v+':') for v in ('extend','name','limit','traffic','transfer'))
            if base.startswith(('admin:renewcustom:','admin:input:renewcustom:','admin:input:agfee:','admin:input:antig_custom:','admin:saleprice:','admin:input:saleprice:')) or operation in ('key','refresh','qr','devices','device','device_confirm','device_apply','client','confirm','apply','mv') or is_key_input:
                target=route(base,origin or existing)
            else:target=base if base!=split(original)[0] else original
            row[i]=(label,target)
            if original in screen.button_options:updated[target]=screen.button_options[original]
    screen.button_options.update(updated)
    return search_context(screen,origin,action)


def search_context(screen,origin,action):
    """Keep search entry across client cards, grants and their input screens."""
    if screen is None or not (origin=='h' or origin.startswith('s')):return screen
    root='home' if origin=='h' else 'admin:search:results:'+origin[1:]
    updated={}
    for row in screen.rows:
        for i,(label,target) in enumerate(row):
            base,existing=split(target)
            original=target
            if label.startswith('←') and (base.startswith(('admin:clients:','admin:r:server:')) or base in ('admin:home','admin:r:home') or origin=='h' and base.startswith('admin:search:results:')):
                base=root
            if base.startswith('admin:') and not base.startswith('admin:search:'):
                target=route(base,existing or origin)
            else:target=base
            row[i]=(label,target)
            if original in screen.button_options:updated[target]=screen.button_options[original]
    screen.button_options.update(updated)
    return screen
