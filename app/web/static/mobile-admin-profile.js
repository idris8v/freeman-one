/* Admin-only mobile presentation; native navigation and business handlers stay shared. */
const mobileAdminProfileBase=adminProfilePage;
const mobileAdminEnabled=()=>state.data?.profile?.is_admin&&matchMedia('(max-width:1100px)').matches;
function mobileAdminIcon(name){return name==='reports'?reportIcon():name==='assistant'&&typeof aiEntryIcon==='function'?aiEntryIcon():svg(name)}
function mobileAdminRow(label,icon,attrs,subtitle='',tone='blue'){
 return `<button type="button" class="ma-row" ${attrs}><span class="ma-icon ma-${tone}" aria-hidden="true">${mobileAdminIcon(icon)}</span><span class="ma-copy"><strong>${esc(label)}</strong>${subtitle?`<small>${esc(subtitle)}</small>`:''}</span><span class="ma-chevron" aria-hidden="true">${actionIcon('chevron-right')}</span></button>`;
}
function mobileAdminGroup(title,rows){return `<section class="ma-group"><h2 class="profile-section-title">${esc(title)}</h2><div class="ma-list">${rows.join('')}</div></section>`}
function mobileAdminPage(title,body,back='settings'){return `<div class="profile-page ma-page" data-ai-profile><header class="ma-page-title"><button type="button" data-admin-panel="${back}" aria-label="Назад">${actionIcon('back')}</button><h1>${esc(title)}</h1></header>${body}</div>`}
function mobileAdminSettings(d){
 const row=(label,icon,panel)=>mobileAdminRow(label,icon,`data-admin-panel="${panel}"`,'','neutral');
 return mobileAdminPage('Настройки',mobileAdminGroup('Аккаунт',[row('Личные данные','profile','personal'),row('Безопасность','shield-lock','security')])+mobileAdminGroup('Система',[mobileAdminRow('Уведомления','bell','data-notifications','','neutral')])+mobileAdminGroup('Интерфейс',[row('Оформление','moon','appearance')])+mobileAdminGroup('Прочее',[`<form action="/api/web/logout" method="post">${mobileAdminRow('Выйти из аккаунта','logout','data-admin-logout','','neutral').replace('type="button"','type="submit"')}</form>`]),'home');
}
function mobileAdminDetail(d,panel){
 const p=d.profile;
 if(panel==='personal')return mobileAdminPage('Личные данные',`<section class="ma-list ma-personal"><dl><div><dt>Имя</dt><dd>${esc(p.name)}</dd></div><div><dt>Freeman ID</dt><dd>${esc(displayFreemanId(p.freeman_id))}</dd></div><div><dt>Роль</dt><dd>Администратор</dd></div><div><dt>Email</dt><dd>${esc(p.email||'Не привязан')}</dd></div><div><dt>Telegram ID</dt><dd>${esc(p.telegram_id||'Не привязан')}</dd></div></dl>${mobileAdminRow(p.email_verified?'Изменить почту':p.email?'Подтвердить почту':'Привязать почту','mail','data-bind-email')}${!p.telegram_id?mobileAdminRow('Привязать Telegram','telegram','data-bind-telegram'):''}</section>`);
 if(panel==='security')return mobileAdminPage('Безопасность',mobileAdminGroup('Доступ к аккаунту',[mobileAdminRow('Активные сеансы','laptop','data-admin-panel="sessions"'),mobileAdminRow('Доверенные браузеры','devices','data-admin-panel="browsers"'),...(p.password_login_available?[`<a class="ma-row" href="/buy/security"><span class="ma-icon ma-neutral">${svg('lock')}</span><span class="ma-copy"><strong>Смена пароля</strong></span>${actionIcon('chevron-right')}</a>`]:[])]));
 if(panel==='sessions')return mobileAdminPage('Активные сеансы',`<section class="ma-list"><div class="ma-note"><h2>Управление входом</h2><p>Завершение сеансов выйдет из аккаунта на всех устройствах, включая это. Доверие к браузерам управляется отдельно.</p></div>${mobileAdminRow('Завершить все сеансы','logout','data-admin-revoke-sessions','','orange')}</section>`);
 if(panel==='browsers')return mobileAdminPage('Браузеры',`<p class="ma-caption">Доверенные браузеры узнают ваш аккаунт при покупке. Для управления сервисом нужен вход.</p><section class="ma-list" data-admin-browser-list role="status"><div class="ma-note">Загружаем устройства…</div></section>`);
 if(panel==='appearance')return mobileAdminPage('Оформление',mobileAdminGroup('Тема сайта',[mobileAdminRow(state.theme==='dark'?'Тёмная тема · переключить на светлую':'Светлая тема · переключить на тёмную',state.theme==='dark'?'sun':'moon','data-theme-toggle','','neutral')]));
 if(panel==='language')return mobileAdminPage('Язык',`<section class="ma-list"><div class="ma-note"><h2>Русский</h2><p>Текущий язык интерфейса. Другие переводы пока не подключены.</p></div></section>`);
 return mobileAdminSettings(d);
}
adminProfilePage=function(d){
 if(!d?.profile?.is_admin||!matchMedia('(max-width:1100px)').matches)return mobileAdminProfileBase(d);
 const panel=state.extra.mobileAdminPanel||'home';if(panel==='settings')return mobileAdminSettings(d);if(panel!=='home')return mobileAdminDetail(d,panel);
 const p=d.profile,initial=(p.name||'A').trim().slice(0,1).toUpperCase();
 const avatar=`<span>${esc(initial)}</span>${p.avatar?`<img src="${esc(p.avatar)}" alt="" decoding="async">`:''}`;
 const row=(route,title,icon,tone)=>{const item=navigationItem(route);return mobileAdminRow(item?.[1]||title,item?.[2]||icon,`data-view="${route}"`,'',tone)};
 return `<div class="profile-page ma-page ma-home" data-ai-profile><div class="ma-summary"><button class="ma-identity" data-admin-panel="personal"><span class="ma-avatar">${avatar}</span><span><strong>${esc(p.name||'Admin')}${actionIcon('chevron-right')}</strong><small>Freeman ID: ${esc(displayFreemanId(p.freeman_id))}</small></span></button><button class="ma-balance" data-view="balance" aria-label="Заработок за текущий месяц"><span class="ma-icon ma-green">${svg('wallet')}</span><span><small>Баланс</small><strong>${state.extra.adminMonth?rub(state.extra.adminMonth.stats.revenue):'—'}</strong></span><span class="ma-chevron">${actionIcon('chevron-right')}</span></button></div>${mobileAdminGroup('Управление',[row('admin-clients','Клиенты','users','blue'),row('subscriptions','Подписки','plans','green'),row('admin-catalog','Тарифы и акции','plans','blue'),row('admin-payments','Платежи','wallet','orange'),row('admin-reports','Отчётность','reports','violet'),row('admin-posts','Публикации','file-text','blue')])}${mobileAdminGroup('Сервисы',[row('admin-servers','Серверы','router','violet'),row('referrals','Реферальная программа','users','green')])}${mobileAdminGroup('AI-инструменты',[mobileAdminRow('Freeman Assistant','assistant','data-ai-open="/admin/assistant"','Личный ассистент и анализ сервиса','blue'),mobileAdminRow('Freeman AI','support','data-ai-open="/admin/ai"','Клиентская поддержка и база знаний','violet')])}</div>`;
};
// Legacy home/profile aliases (including error recovery) share the admin renderer.
const mobileAdminViewContentBase=viewContent;
viewContent=function(d){if(d?.profile?.is_admin&&matchMedia('(max-width:1100px)').matches&&['profile','home','admin-dashboard'].includes(state.view))return adminProfilePage(d);return mobileAdminViewContentBase(d)};
let mobileAdminBrowserRequest=0;
async function mobileAdminBrowsers(){
 const request=++mobileAdminBrowserRequest;
 try{const r=await fetch('/api/web/trusted-devices',{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.detail||'Не удалось загрузить устройства');const box=document.querySelector('[data-admin-browser-list]');if(!box||request!==mobileAdminBrowserRequest)return;
 box.innerHTML=d.items.length?d.items.map(x=>`<div class="ma-browser"><span class="ma-icon ma-neutral">${svg('devices')}</span><div><strong>${esc(x.label)}${x.current?' · Это устройство':''}</strong><small>${esc(new Date(x.last_seen_at).toLocaleString('ru-RU'))}</small><button type="button" data-admin-browser-revoke="${x.id}">Удалить доверие</button></div></div>`).join(''):'<div class="ma-note">Нет доверенных браузеров.</div>';
 }catch(error){const box=document.querySelector('[data-admin-browser-list]');if(box)box.innerHTML=`<div class="ma-note">${esc(error.message)}<button type="button" data-admin-browser-retry>Повторить</button></div>`}
}
document.addEventListener('click',async e=>{
 if(!mobileAdminEnabled())return;
 const panel=e.target.closest('[data-admin-panel]');
 if(panel){e.preventDefault();e.stopImmediatePropagation();if(state.view==='admin-posts'&&typeof pubCanLeave==='function'&&!(await pubCanLeave()))return;state.extra.mobileAdminPanel=panel.dataset.adminPanel||'settings';state.modal=null;state.view='admin-dashboard';render();const screen=document.querySelector('.screen[data-screen="admin-dashboard"]');if(screen)screen.scrollTop=0;if(state.extra.mobileAdminPanel==='browsers')mobileAdminBrowsers();document.querySelector('.ma-page-title h1')?.setAttribute('tabindex','-1');document.querySelector('.ma-page-title h1')?.focus({preventScroll:true});return}
 const revoke=e.target.closest('[data-admin-revoke-sessions]');if(revoke){e.preventDefault();e.stopImmediatePropagation();if(!await FreemanUI.confirm({title:'Завершить все сеансы?',message:'Потребуется снова войти в аккаунт.',accept:'Завершить',danger:true}))return;revoke.disabled=true;try{await accountRequest('/api/web/profile/sessions/revoke',{});location.href='/'}catch(error){revoke.disabled=false;say(error.message)}return}
 const browser=e.target.closest('[data-admin-browser-revoke]');if(browser){browser.disabled=true;try{const r=await fetch('/api/web/trusted-devices/'+browser.dataset.adminBrowserRevoke,{method:'DELETE'});if(!r.ok)throw Error('Не удалось удалить доверие');await mobileAdminBrowsers()}catch(error){browser.disabled=false;say(error.message)}return}
 if(e.target.closest('[data-admin-browser-retry]'))mobileAdminBrowsers();
 if(e.target.closest('[data-view]'))state.extra.mobileAdminPanel='home';
},true);
matchMedia('(max-width:1100px)').addEventListener('change',()=>{if(state.data?.profile?.is_admin&&state.view==='admin-dashboard')render()});
