"""Trusted business results -> typed visual blocks. Never accepts model HTML or URLs."""
def link(kind,entity=None,label=None):
 return {'label':label or {'client':'Открыть клиента','keys':'Открыть ключи','key':'Открыть ключ','renew':'Продлить','devices':'Устройства','payment':'Открыть платёж','server':'Открыть сервер','reports':'Открыть отчёт','offer':'Создать предложение'}[kind],'url':'/?assistant_view='+kind+(('&entity='+str(int(entity))) if entity else '')}
def build(name,r):
 blocks=[]
 if name=='find_client':
  for c in r.get('clients',[]):blocks.append({'type':'client','data':c,'actions':[link('client',c['id']),link('offer',c['id']),link('keys',c['id'])]})
  if not r.get('clients'):blocks.append({'type':'alert','text':'Клиент не найден. Уточни имя или Freeman ID.','tone':'neutral'})
 elif name=='client_detail':
  c=dict(r['client']);c['subscriptions']=len(r.get('subscriptions',[]))
  blocks.append({'type':'client','data':c,'actions':[link('client',c['id']),link('offer',c['id']),link('keys',c['id'])]})
  for sub in r.get('subscriptions',[])[:8]:blocks.append({'type':'subscription','data':sub,'actions':[link('key',sub['credential_id']),link('renew',sub['credential_id']),link('devices',sub['credential_id'])] if sub.get('credential_id') else [link('client',c['id'],'Управлять подпиской')]})
 elif name=='key_detail':blocks.append({'type':'subscription','data':r,'actions':[link('key',r['credential']['id']),link('renew',r['credential']['id']),link('devices',r['credential']['id'])] if r.get('credential') and r['credential'].get('id') else [link('client',r['client_id'],'Управлять подпиской')]})
 elif name=='payment_detail':blocks.append({'type':'payment','data':r,'actions':[link('payment',r['id']),link('client',r['client_id'])]})
 elif name=='server_overview':
  for kind in ('vpn','proxy'):
   for server in r.get(kind,[]):blocks.append({'type':'server','data':{**server,'kind':kind},'actions':[link('server',server['id'])] if kind=='proxy' else [link('server')]})
  blocks.append({'type':'alert','text':r.get('cpu_ram',''),'tone':'neutral'})
 elif name=='expiring_subscriptions':
  blocks.append({'type':'table','title':'Истекающие подписки','columns':['Клиент','Ключ','До','Устройств'],'rows':[[x['client']['freeman_id'],x['id'],x['expires_at'][:10],x['device_limit']] for x in r['items']]})
  blocks.append({'type':'actions','items':[link('keys')]})
 elif name=='sales_report':
  report=r['report'];stats=report['stats'];prior=report['previous'];money=lambda x:round(float(x or 0)/100,2)
  blocks.append({'type':'metric','items':[{'label':'Продажи','value':stats.get('sales',0)},{'label':'Выручка','value':money(stats.get('revenue',0)),'unit':'₽'},{'label':'Предыдущий период','value':money(prior.get('revenue',0)),'unit':'₽'},{'label':'Новые регистрации','value':stats.get('new_customers',0)}]})
  previous=float(prior.get('revenue',0));delta=float(stats.get('revenue',0))-previous
  comparison=(f'{delta/previous*100:+.1f}% · {money(delta):+.2f} ₽ к предыдущему периоду' if previous else 'Нет базы для процентного сравнения с предыдущим периодом.')
  blocks.append({'type':'alert','tone':'neutral','text':comparison})
  rows=[[x.get('name') or (str(x.get('months',''))+' мес. · '+str(x.get('devices',''))+' устр.'),x.get('sales',0),money(x.get('revenue',0)),x.get('buyers',0)] for x in report.get('tariffs',[])[:30]]
  blocks.append({'type':'table','title':'Продажи · '+report['start']+' — '+report['end'],'columns':['Тариф','Продаж','Выручка, ₽','Покупателей'],'rows':rows,'footer':['Всего',stats.get('sales',0),money(stats.get('revenue',0)),stats.get('buyers',0)]})
  series=report.get('series',[])
  if len(series)>1:blocks.append({'type':'chart','title':'Выручка по периодам, ₽','variant':'bar','labels':[x['date'] for x in series],'values':[money(x['revenue']) for x in series]})
  blocks.append({'type':'actions','items':[link('reports')]})
 elif name=='save_knowledge' and r.get('saved'):blocks.append({'type':'alert','tone':'success','text':'Сохранено в базе знаний · #'+str(r['id'])})
 return blocks[:55]
