from html import escape
from sqlalchemy import select
from app.database.models import User
from app.services.screens import Screen,back
NAMES={'support':'Поддержка','manager':'Менеджер','administrator':'Администратор','partner':'Партнёр'}
async def render(service,action,actor):
 repo,settings=service.repo,service.settings
 if not settings.is_owner(actor.telegram_id):raise PermissionError('Только владелец управляет сотрудниками.')
 p=action.split(':');page=p[1]
 if page=='roles':
  roles=await repo.settings_prefix('staff_role:');items=[(int(k.split(':')[1]),v) for k,v in roles.items() if v in NAMES];items.sort()
  offset=max(0,int(p[2])) if len(p)>2 else 0;rows=[]
  for tid,role in items[offset:offset+5]:
   user=await repo.session.scalar(select(User).where(User.telegram_id==tid))
   state='⏸' if await repo.setting(f'staff_blocked:{tid}')=='1' else '✅'
   rows.append([(f'{state} {user.display_name if user else tid} · {NAMES[role]}',f'admin:staff:{tid}')])
  if offset:rows.append([('← Предыдущие',f'admin:roles:{max(0,offset-5)}')])
  if offset+5<len(items):rows.append([('Следующие →',f'admin:roles:{offset+5}')])
  return Screen('🧑‍💼 <b>Сотрудники и доступ</b>\n\n<b>Поддержка:</b> обращения клиентов.\n\n<b>Менеджер:</b> бесплатные VPN-подписки и выдача прокси. Без клиентской базы, статистики, обращений и выплат.\n\n<b>Партнёр:</b> получение VPN-ключей, точный поиск по ссылке и отвязка устройств. Без клиентской базы и администрирования.\n\n<b>Администратор:</b> полный доступ, как у главного администратора.\n\nОткройте сотрудника, чтобы сменить роль, временно закрыть доступ или удалить его. Владельца изменить нельзя.',rows+[[('➕ Добавить сотрудника','admin:input:staff:add')],back('admin:more')],structured=True)
 tid=int(p[2])
 if tid<=0 or tid in settings.owner_ids():raise ValueError('Доступ владельца изменить нельзя.')
 role=await repo.setting(f'staff_role:{tid}')
 if role not in NAMES:raise ValueError('Сотрудник не найден.')
 blocked=await repo.setting(f'staff_blocked:{tid}')=='1'
 if page=='staffrole':
  role=p[3]
  if role not in NAMES:raise ValueError('Роль недоступна.')
  await repo.set_setting(f'staff_role:{tid}',role);settings.set_staff_role(tid,None if blocked else role)
  await service.audit(actor,'staff_role',tid,role)
 elif page=='staffblock':
  blocked=not blocked;await repo.set_setting(f'staff_blocked:{tid}','1' if blocked else '0');settings.set_staff_role(tid,None if blocked else role)
  await service.audit(actor,'staff_access',tid,'blocked' if blocked else 'active')
 elif page=='staffremove':
  return Screen(f'🗑 <b>Удалить доступ сотрудника {tid}?</b>\n\nДоступ к администрированию будет закрыт. Клиентский аккаунт и подписки сохранятся.',[[('🗑 Да, удалить доступ',f'admin:staffdelete:{tid}')],back(f'admin:staff:{tid}')],structured=True)
 elif page=='staffdelete':
  await repo.set_setting(f'staff_role:{tid}','');await repo.set_setting(f'staff_blocked:{tid}','1');settings.set_staff_role(tid,None)
  await service.audit(actor,'staff_remove',tid,'Доступ удалён')
  return await render(service,'admin:roles',actor)
 user=await repo.session.scalar(select(User).where(User.telegram_id==tid))
 return Screen(f'🧑‍💼 <b>{escape(user.display_name if user else "Сотрудник")}</b>\n\nTelegram ID: <code>{tid}</code>\n\nРоль: {NAMES[role]}\n\nДоступ: {"⏸ Заблокирован" if blocked else "✅ Активен"}\n\nВыберите роль, затем включите доступ. Блокировка закрывает администрирование, не удаляя клиентский аккаунт.',[[('🛟 Поддержка'+(' ✓' if role=='support' else ''),f'admin:staffrole:{tid}:support')],[('📊 Менеджер'+(' ✓' if role=='manager' else ''),f'admin:staffrole:{tid}:manager')],[('🤝 Партнёр'+(' ✓' if role=='partner' else ''),f'admin:staffrole:{tid}:partner')],[('⚙️ Администратор'+(' ✓' if role=='administrator' else ''),f'admin:staffrole:{tid}:administrator')],[('▶️ Включить доступ' if blocked else '⏸ Заблокировать доступ',f'admin:staffblock:{tid}')],[('🗑 Удалить сотрудника',f'admin:staffremove:{tid}')],back('admin:roles')],structured=True)
