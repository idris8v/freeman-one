/* Entry actions only. Role-aware navigation is owned by app.js. */
document.addEventListener('click',async e=>{
 const entry=e.target.closest('[data-ai-open]');if(entry){if(entry.disabled)return;entry.disabled=true;const target=entry.dataset.aiOpen;if(window.PageMotion)await PageMotion.navigate(target);else location.href=target;return}
 const remove=e.target.closest('[data-support-delete-ticket]');if(!remove||remove.disabled)return;
 const ticket=state.support?.selected;if(!ticket||!state.support.is_admin)return;
 remove.disabled=true;
 if(!await FreemanUI.confirm({title:'Удалить обращение #'+ticket.id+'?',message:'Переписка и вложения исчезнут у администратора и клиента.',accept:'Удалить',danger:true})){remove.disabled=false;return;}
 try{const r=await fetch('/api/web/support/tickets/'+ticket.id,{method:'DELETE'});if(!r.ok)throw Error('Не удалось удалить обращение');SupportInboxCache.clear();SupportChatCache.clear();closeSupportSheet();state.supportTicket=null;await loadSupport(null);say('Обращение удалено')}catch(error){say(error.message)}finally{remove.disabled=false}
});


document.addEventListener('keydown',e=>{if(e.target.matches('[data-support-message] textarea')&&e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();e.target.form.requestSubmit()}});

// Stable entity IDs from assistant cards open existing application flows.
(async()=>{const params=new URLSearchParams(location.search),view=params.get('assistant_view'),id=Number(params.get('entity'));if(!view)return;
 for(let tries=0;tries<120;tries++){if(typeof state!=='undefined'&&state.data?.profile&&state.view?.startsWith('admin-')&&!state.extra?.routePending){if(!state.data.profile.is_admin)return;try{
 const url=new URL(location.href);url.searchParams.delete('assistant_view');url.searchParams.delete('entity');
 if(view==='client'&&id>0)await openAdminCustomer(id);
 else if(view==='offer'&&id>0){await loadView('admin-clients');await crmCampaignOpen('offer',{user_ids:[id]})}
 else if(view==='keys'){if(id>0){await openAdminCustomer(id)}else await loadView('admin-keys')}
 else if(view==='key'&&id>0)await openAdminKey(id);
 else if(view==='renew'&&id>0){await loadView('admin-keys');await keysRenew(id)}
 else if(view==='devices'&&id>0){await loadView('admin-keys');await keysDevices(id)}
 else if(view==='payment'&&id>0){await loadView('admin-payments');await paySelect('P'+id)}
 else if(view==='reports')await loadView('admin-reports');
 else if(view==='server'){if(id>0){await loadView('admin-proxy');await pxOpenServer(id)}else await loadView('admin-servers')}
 history.replaceState(history.state,'',url);
 }catch(error){say('Не удалось открыть раздел: '+error.message)}return}await new Promise(resolve=>setTimeout(resolve,250))}
})();
