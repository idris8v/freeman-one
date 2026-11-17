"""Five records per bot list, independent of action/control buttons."""
import re
SIZE=5
RULES={
 'admin:tariffs':r'admin:tariff:\d+',
 'payment_management':r'(?:payment_card:[^:]+|autorenew:\d+)',
 'g:list':r'g:item:\d+', 'g:keys':r'g:input:key:\d+',
 'admin:promos':r'admin:promo:\d+', 'admin:servers':r'admin:server:\d+',
 'admin:p:home':r'admin:p:(?:server|item):\d+', 'admin:v:home':r'admin:v:server:\d+',
 'admin:custombuttons':r'admin:custombutton:[^:]+',
 'admin:r:home':r'admin:r:server:\d+',
 'admin:x:content':r'admin:x:(?:input:description|instructionitem|faqitem|documentitem):[^:]+',
 'faq':r'answer:\d+', 'instructions':r'instruction:[^:]+',
 'buy':r'tariff:\d+(?::\d+)?', 'plans':r'tariff:\d+(?::\d+)?',
}
def rule(action):
 if action in RULES:return RULES[action]
 if re.fullmatch(r'admin:keys:\d+',action):return r'admin:key:\d+'
 if re.fullmatch(r'admin:subs:\d+',action):return r'admin:subscription:\d+'
 if re.fullmatch(r'autorenew:\d+:cards',action):return r'autorenew:\d+:card:\d+:[^:]+'
 if re.fullmatch(r'instructions:\d+',action):return r'instruction:[^:]+:\d+'
 if re.fullmatch(r'renew:\d+',action):return r'tariff:\d+:\d+'
 if re.fullmatch(r'v:locations:\d+',action):return r'v:select:\d+:\d+'
 return None
def split_action(action):
 base,mark,page=action.rpartition('~')
 if mark and page.isdigit() and rule(base):return base,min(int(page),1000000)
 return action,0
def paginate(screen,action,offset=0):
 pattern=rule(action)
 if not pattern:return screen
 records=[row for row in screen.rows if any(re.fullmatch(pattern,target) for _,target in row)]
 if len(records)<=SIZE and not offset:return screen
 offset=min(max(0,offset),max(0,(len(records)-1)//SIZE*SIZE))
 visible=records[offset:offset+SIZE];result=[];inserted=False
 for row in screen.rows:
  if row in records:
   if not inserted:result.extend(visible);inserted=True
  else:result.append(row)
 nav=[]
 if offset:nav.append(('← Предыдущие',f'{action}~{max(0,offset-SIZE)}'))
 if offset+SIZE<len(records):nav.append(('Следующие →',f'{action}~{offset+SIZE}'))
 if nav:result.insert(max(0,len(result)-1),nav)
 screen.rows=result
 return screen
