/* Shared presentation components. Controllers own actions, state and persistence.
   adopt() is called at render boundaries; no MutationObserver or per-render listeners. */
(()=>{'use strict';if(window.FreemanComponents)return;
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths={'external-link':'M14 3h7v7M21 3 10 14M10 3H3v18h18v-7',edit:'m16 3 5 5-12 12H4v-5zM14 5l5 5',plans:'M4 4h16v16H4zM8 8h8M8 12h8M8 16h5',latest:'M12 5v14m-6-6 6 6 6-6',image:'M3 3h18v18H3zM3 16l5-5 4 4 3-3 6 6M16 7h.01',camera:'M3 7h4l2-3h6l2 3h4v13H3zM16 13a4 4 0 1 0-8 0 4 4 0 0 0 8 0',menu:'M4 6h16M4 12h16M4 18h16',quick:'m13 2-9 12h7l-1 8 10-13h-7z',user:'M20 21v-2a8 8 0 0 0-16 0v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8',mic:'M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0zM5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8',send:'M12 19V5m-6 6 6-6 6 6',copy:'M9 9h11v12H9zM5 15H3V3h12v2',close:'M6 6l12 12M6 18 18 6',attach:'M12 4v16M4 12h16',search:'M21 21l-5-5M18 10a8 8 0 1 0-16 0 8 8 0 0 0 16 0',download:'M12 3v12m-5-5 5 5 5-5M5 17v4h14v-4'};
function icon(name){if(name==='more')return '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/></svg>';if(name==='telegram')return '<img class="telegram-logo" src="/assets/telegram-logo.webp" alt="" aria-hidden="true">';return paths[name]?`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]}"/></svg>`:window.AEGIS_ICONS?.icons?.[name]||'';}
// Resize only when content/width changes. Polling must not reset textarea scroll.
function sizeComposer(field,{min=30,max=116}={}){if(!field||field.hidden)return;const width=field.clientWidth,previous=field.__foSize;if(!width)return;if(previous?.text===field.value&&previous.width===width&&previous.min===min&&previous.max===max)return;const top=field.scrollTop;field.style.height=min+'px';field.style.height=Math.min(max,Math.max(min,field.value?field.scrollHeight:min))+'px';field.scrollTop=top;field.__foSize={text:field.value,width,min,max}}
function revealMessage(node){
 const direction=node.dataset.direction||node.querySelector('.fo-chat-message')?.dataset.direction;const outgoing=direction?direction==='outgoing':node.matches('.is-outgoing')||node.matches('.operator,.specialist')&&!!node.closest('.fo-messenger');
 const target=node.querySelector('.native-bubble')||node,host=node.closest('.fa-main,.fo-chat-workspace,.native-chat');
 const send=host?.querySelector('[type=submit]');let x=0,y=8;
 if(outgoing&&send){const from=send.getBoundingClientRect(),to=target.getBoundingClientRect();if(to.bottom>0&&to.top<innerHeight){x=Math.max(-80,Math.min(80,from.x+from.width/2-to.right+16));y=Math.max(8,Math.min(120,from.top-to.bottom+28))}}
 target.style.transformOrigin=outgoing?'bottom right':'bottom left';
 return window.PageMotion?.animate(target,[{opacity:.15,translate:`${x}px ${y}px`,scale:outgoing?.88:.985},{opacity:1,translate:'0 0',scale:1}],outgoing?440:window.PageMotion.tokens.message,'message');
}
function receipt(status){if(String(status).startsWith('notice_'))status='saved';if(!['read','sent','saved','delivered'].includes(status))return '';const read=status==='read',double=['read','delivered'].includes(status);return '<span class="fo-message-receipt" data-read="'+read+'" aria-label="'+(read?'Прочитано':status==='saved'?'Сохранено в чате':'Отправлено')+'"><svg viewBox="0 0 24 16" fill="none" aria-hidden="true"><path d="m2 8 4 4L17 2"/>'+(double?'<path d="m11 11 2 2L23 3"/>':'')+'</svg></span>'}

function historyTail(history,composer){
 if(!history||!composer)return null;
 if(history.__foTail)return history.__foTail;
 const host=history.parentElement,button=document.createElement('button');button.type='button';button.className='fo-button fo-icon-button fo-chat-latest';button.dataset.chatLatest='';button.setAttribute('aria-label','К последнему сообщению');button.innerHTML=icon('latest');button.hidden=true;document.body.append(button);
 let following=history.scrollHeight-history.clientHeight-history.scrollTop<80;
 const near=()=>history.scrollHeight-history.clientHeight-history.scrollTop<80;
 const layout=()=>{if(!host.isConnected){button.hidden=true;return}const h=history.getBoundingClientRect(),c=composer.getBoundingClientRect(),v=window.visualViewport;const top=v?.offsetTop||0,bottom=top+(v?.height||innerHeight);const y=Math.min(h.bottom,c.top,bottom)-46,x=Math.min(h.right,innerWidth)-46;Object.assign(button.style,{position:'fixed',left:Math.max(8,x)+'px',top:Math.max(top+8,y)+'px',bottom:'auto',right:'auto',transform:'none',translate:'none',zIndex:'10001'});button.hidden=near()||h.bottom<=top||h.top>=bottom||!h.width};
 const viewportLayout=()=>layout();window.addEventListener('resize',viewportLayout);window.addEventListener('scroll',viewportLayout,true);window.visualViewport?.addEventListener('resize',viewportLayout);
 const scroll=()=>{following=near();layout()};
 const latest=()=>{following=true;history.scrollTo({top:history.scrollHeight,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});layout()};
 const resize=new ResizeObserver(()=>{if(following)history.scrollTop=history.scrollHeight;layout()});resize.observe(history);resize.observe(composer);
 const load=()=>{if(following)history.scrollTop=history.scrollHeight;layout()};
 history.addEventListener('scroll',scroll,{passive:true});history.addEventListener('load',load,true);button.addEventListener('click',latest);
 const api={sync(force=false){if(force)history.scrollTop=history.scrollHeight;following=near();layout()},destroy(){window.removeEventListener('resize',viewportLayout);window.removeEventListener('scroll',viewportLayout,true);window.visualViewport?.removeEventListener('resize',viewportLayout);resize.disconnect();history.removeEventListener('scroll',scroll);history.removeEventListener('load',load,true);button.remove();delete history.__foTail}};history.__foTail=api;return api;
}
// Old generated titles included device count and price, already shown beside the term.
function tariffTerm(months){const n=Number(months),plural=(v,forms)=>forms[v%100>=11&&v%100<=14?2:v%10===1?0:v%10>=2&&v%10<=4?1:2];return n===0?'24 часа':n>0&&n%12===0?(n/12)+' '+plural(n/12,['год','года','лет']):n+' '+plural(n,['месяц','месяца','месяцев'])}
function isAutomaticTariffName(name){return !String(name||'').trim()||/^\d+\s*(?:мес\.?|месяц(?:а|ев)?|год(?:а)?|лет)(?:[\s·,|—–-]+\d+\s*устройств(?:о|а)?)?(?:[\s·,|—–-]+[\d\s]+(?:[,.]\d{1,2})?\s*(?:₽|руб(?:\.|ль|ля|лей)?))?$/iu.test(String(name).trim())}
function tariffLabel(tariff){return isAutomaticTariffName(tariff.name)?tariffTerm(tariff.months):String(tariff.name)}
function media(item){const url=String(item?.url||'');if(item?.kind==='guide'&&/^\/assets\/setup-guide-[a-z_]+\.png$/.test(url)){const name=escape(item.name||'Схема подключения');return `<figure class="fo-chat-media fo-chat-photo"><a href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="${name}" loading="lazy" decoding="async" style="width:100%;height:auto"></a></figure>`;}if(!/^\/api\/web\/support\/(attachments\/\d+|messages\/\d+\/media\/(photo|document)|materials\/appstore-region\/video)$/.test(url)&&url!=='/api/support-agent/materials/appstore-region/video')return '';const name=escape(item.name||'Вложение');if(item.kind==='image')return `<figure class="fo-chat-media fo-chat-photo ai-media-link message-media image" data-photo-frame="${url}" data-photo-state="idle" aria-busy="true"><button type="button" class="fo-photo-open" data-chat-photo="${url}" aria-label="Открыть фото: ${name}"><img src="${url}?preview=tiny" alt="${name}" loading="lazy" decoding="async"></button><button type="button" class="fo-photo-transfer" data-photo-transfer aria-label="Загрузить фото"><span class="fo-photo-spinner" aria-hidden="true"></span></button><span data-photo-status role="status">Загружаем фото</span></figure>`;if(item.kind==='video')return `<figure class="fo-chat-media ai-media-video"><video class="support-video" src="${url}" controls playsinline preload="metadata" aria-label="${name}"></video></figure>`;if(item.kind==='audio')return `<audio class="fo-chat-audio" src="${url}" controls preload="metadata" aria-label="${name}"></audio>`;return `<a class="fo-chat-file" href="${url}" target="_blank" rel="noopener">${icon('download')}<span>${name}</span></a>`;}

function quickReply(reply,attributes=''){
 const preview=document.createElement('template');
 preview.innerHTML=richText(reply.text);
 const snippet=Array.from(preview.content.firstElementChild.children,block=>block.textContent)
   .join(' ').replace(/\s+/g,' ').trim();
 const shortcut=Number.isSafeInteger(reply.shortcut)&&reply.shortcut>0?'/'+reply.shortcut+' · ':'';

 const asset=(reply.attachments||[]).find(a=>['image','video'].includes(a.kind)&&/^\/api\/web\/support\/attachments\/[1-9]\d*$/.test(String(a.url||'')));
 const thumb=asset?(asset.kind==='image'?`<img src="${escape(asset.url+'?preview=1')}" alt="" loading="lazy" decoding="async">`:`<video src="${escape(asset.url)}" muted playsinline preload="metadata" aria-hidden="true"></video><span class="fo-quick-play" aria-hidden="true">▶</span>`):'<img src="/assets/freeman-mark-20261029.png" alt="" loading="lazy" decoding="async">';
 return `<button type="button" class="fo-row-action fo-quick-reply" ${attributes}><span class="fo-quick-icon ${asset?'has-preview':'is-brand'}" aria-hidden="true">${thumb}</span><span class="fo-quick-body"><strong>${escape(shortcut+reply.title)}</strong><span>${escape(snippet)}</span></span></button>`;
}

function chatHeader({name,subtitle='',extra='',person={},backAttributes='',backLabel='Назад',backIcon='latest',personAttributes='',menu='',className=''}){
 menu=menu.replace(/(<summary\b[^>]*>)•••(<\/summary>)/g,'$1'+icon('more')+'$2');
 return `<header class="cm-chat-header fo-chat-header ${className}"><button type="button" class="fo-button cm-header-back native-control ai-chat-back" ${backAttributes} aria-label="${escape(backLabel)}">${icon(backIcon)}</button><${personAttributes?'button type="button"':'div'} class="cm-header-person ${className.includes('native-header')?'native-person':'ai-chat-person'}" ${personAttributes}>${avatar({...person,name},{className:'cm-header-avatar'})}<span class="cm-header-identity"><strong>${escape(name)}</strong><small>${escape(subtitle)}${extra}</small></span></${personAttributes?'button':'div'}><div class="cm-header-tools"><button type="button" class="fo-button cm-header-search" data-chat-search aria-label="Поиск в переписке">${icon('search')}</button>${menu}</div></header>`;
}
const supportScopeIcons = new Map([
 ['Не работает что-то конкретное', '📱'],
 ['Вообще ничего не работает', '🌐'],
]);
function supportChoiceText(source, actions) {
 const labels = (Array.isArray(actions) ? actions : [])
  .filter(action => action?.type === 'reply_choice' && supportScopeIcons.has(action.label))
  .map(action => action.label);
 if (labels.length !== 2 || new Set(labels).size !== 2) return source;
 const suffix = '\n\n' + labels.map((label, index) => `${index + 1}. ${label}`).join('\n\n');
 const trimmed = source.trimEnd();
 return trimmed.endsWith(suffix) ? trimmed.slice(0, -suffix.length) : source;
}
function inlineActions(actions){
 const rows=new Map(),seen=new Set();
 const trusted=value=>{if(typeof value!=='string'||value.length>2048||/[\u0000-\u001f]/.test(value))return false;let u;try{u=new URL(value,location.origin)}catch{return false}if(u.username||u.password)return false;if(u.origin===location.origin&&/^\/support\/(action|connect|renew)$/.test(u.pathname)&&/^#[A-Za-z0-9_-]{32}$/.test(u.hash))return true;if(u.hash)return false;if(u.origin===location.origin&&['/buy','/buy/renew'].includes(u.pathname))return true;if(u.origin===location.origin&&u.pathname==='/'&&/^\?cabinet_view=keys&managed_key=[1-9][0-9]{0,9}(?:&key_action=antig)?$/.test(u.search))return true;if(value==='https://support.apple.com/ru-ru/118283')return true;if(u.protocol!=='https:'||u.port)return false;if(['https://yandex.ru/internet/','https://t.me/FreemanOneBot','https://www.happ.su/main','https://github.com/INCY-DEV/incy-platforms','https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk','https://docs.incy.cc/tv-relay/'].includes(value))return true;if(u.origin===location.origin&&/^\/support\/(action|connect|renew)\/[A-Za-z0-9_-]{32}$/.test(u.pathname))return true;return u.hostname==='apps.apple.com'&&/^\/(?:[a-z]{2}\/)?(?:[^/?#]+\/)*id\d+\/?$/.test(u.pathname)||u.hostname==='play.google.com'&&u.pathname==='/store/apps/details'&&/^[A-Za-z0-9_.]{3,200}$/.test(u.searchParams.get('id')||'')};
 for(const [i,a]of (Array.isArray(actions)?actions:[]).slice(0,24).entries()){
  if(!a||typeof a!=='object')continue;const label=String(a.label||'').slice(0,120);if(!label)continue;let button='',identity='';
  const emoji = ['reply_choice','agent_action'].includes(a.type) ? supportScopeIcons.get(label) : undefined;
  const choiceContent = emoji ? `<span class="cm-choice-emoji" aria-hidden="true">${emoji}</span><span>${escape(label)}</span>` : escape(label);
  const choiceClass = emoji ? ' cm-inline-choice' : '';
  const askService = emoji === '📱' ? ' data-chat-ask-service' : '';
  if(a.type==='link'&&trusted(a.url)){identity='link:'+a.url;button=`<a class="cm-inline-button" href="${escape(a.url)}" ${new URL(a.url,location.origin).origin!==location.origin?'target="_blank" rel="noopener noreferrer" data-chat-external':''}><span>${escape(label)}</span>${icon('external-link')}</a>`}
  if(['copy','copy_text'].includes(a.type)&&typeof a.text==='string'&&a.text.length&&a.text.length<=512){identity='copy:'+a.text;button=`<button type="button" class="cm-inline-button" data-cm-copy="${escape(a.text)}"><span>${escape(label)}</span>${icon('copy')}</button>`}
  if(a.type==='material'&&['appstore-region','key-install'].includes(a.material)){identity='guide:'+a.material;button=`<button type="button" class="cm-inline-button" data-chat-material="${escape(a.material)}"><span>${escape(label)}</span>${icon('download')}</button>`}
  if(a.type==='agent_action'&&/^[A-Za-z0-9_-]{32}$/.test(a.token)&&['open','confirm','cancel'].includes(a.decision||'open')){identity='agent:'+a.token+':'+(a.decision||'open');button=`<button type="button" class="cm-inline-button${choiceClass}"${askService} data-agent-token="${escape(a.token)}" data-agent-decision="${escape(a.decision||'open')}">${choiceContent}</button>`}
  if(a.type==='reply_choice'){
   identity='reply-choice:'+label;
   button=`<button type="button" class="cm-inline-button${choiceClass}"${askService} data-chat-reply-choice="${escape(label)}">${choiceContent}</button>`;
  }
  if(a.type==='agent_image'&&/^[A-Za-z0-9_-]{32}$/.test(a.token)){identity='qr:'+a.token;button=`<a class="cm-inline-button" href="/support/action#${escape(a.token)}" target="_blank" rel="noopener">Открыть ключ</a><img data-agent-qr="${escape(a.token)}" alt="QR подписки" style="display:block;max-width:192px;width:100%;height:auto">`}
  if(!button||seen.has(identity))continue;seen.add(identity);const row=Number.isSafeInteger(a.row)?Math.max(0,Math.min(23,a.row)):i;if(!rows.has(row))rows.set(row,[]);rows.get(row).push(button);
 }
 return rows.size?'<div class="cm-inline-keyboard" role="group" aria-label="Кнопки сообщения">'+[...rows.values()].map(buttons=>'<div class="cm-keyboard-row">'+buttons.join('')+'</div>').join('')+'</div>':'';
}
function refreshChatSearch(root=document){root.querySelectorAll('.cm-chat-find').forEach(row=>row.__search?.())}
function toggleChatSearch(button){
 const chat=button.closest('.fo-chat-workspace,.native-chat');if(!chat)return;const existing=chat.querySelector('.cm-chat-find');if(existing){existing.__close?.();return}
 const row=document.createElement('form');row.className='cm-chat-find';row.setAttribute('role','search');row.innerHTML='<label>'+icon('search')+'<input type="search" maxlength="100" aria-label="Поиск в загруженных сообщениях" placeholder="Поиск в переписке"></label><output aria-live="polite"></output><button type="button" data-find-prev aria-label="Предыдущее совпадение">↑</button><button type="button" data-find-next aria-label="Следующее совпадение">↓</button><button type="button" data-find-close aria-label="Закрыть поиск в чате">'+icon('close')+'</button>';
 chat.querySelector('.fo-chat-header').after(row);const input=row.querySelector('input');let current=0,hits=[];
 const move=()=>{chat.querySelectorAll('.cm-search-current').forEach(n=>n.classList.remove('cm-search-current'));const hit=hits[current];if(hit){hit.classList.add('cm-search-current');const history=hit.closest('.ai-messages,.native-history'),r=hit.getBoundingClientRect(),h=history.getBoundingClientRect();history.scrollTo({top:history.scrollTop+r.top-h.top-(h.height-r.height)/2,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'})}row.querySelector('output').textContent=hits.length?(current+1)+' из '+hits.length:input.value?'Нет совпадений':'';row.querySelectorAll('[data-find-prev],[data-find-next]').forEach(b=>b.disabled=!hits.length)};
 row.__search=()=>{const q=input.value.trim().toLocaleLowerCase();hits=[];chat.querySelectorAll('.fo-chat-message').forEach(n=>{const match=!!q&&n.querySelector('.fo-message-text')?.textContent.toLocaleLowerCase().includes(q);n.classList.toggle('cm-search-hit',match);if(match)hits.push(n)});current=Math.min(current,Math.max(0,hits.length-1));move()};
 row.__close=()=>{chat.querySelectorAll('.cm-search-hit,.cm-search-current').forEach(n=>n.classList.remove('cm-search-hit','cm-search-current'));row.remove();button.focus({preventScroll:true})};
 row.addEventListener('submit',e=>{e.preventDefault();current=(current+1)%Math.max(1,hits.length);move()});input.addEventListener('input',()=>{current=0;row.__search()});row.addEventListener('click',e=>{if(e.target.closest('[data-find-close]'))row.__close();if(e.target.closest('[data-find-prev]')){current=(current-1+hits.length)%Math.max(1,hits.length);move()}if(e.target.closest('[data-find-next]')){current=(current+1)%Math.max(1,hits.length);move()}});row.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();row.__close()}});input.focus({preventScroll:true});row.__search();
}
document.addEventListener('click',async e=>{const external=e.target.closest('[data-chat-external]'),tg=window.Telegram?.WebApp;if(external&&tg?.initData&&typeof tg.openLink==='function'&&!e.ctrlKey&&!e.metaKey&&!e.shiftKey&&!e.altKey){try{tg.openLink(external.href);e.preventDefault()}catch{}}const search=e.target.closest('[data-chat-search]');if(search){toggleChatSearch(search);return}const material=e.target.closest('[data-chat-material]');if(!material||!['appstore-region','key-install'].includes(material.dataset.chatMaterial))return;material.disabled=true;try{const {open}=await import('/assets/support-materials.js?v=29f154f3d9eb');await open({base:(material.closest('.fo-support-agent-bubble')?'/api/support-agent/materials/':'/api/web/support/materials/')+material.dataset.chatMaterial})}catch{cmFeedback('Не удалось открыть инструкцию. Попробуйте ещё раз.')}finally{material.disabled=false}});

function avatar(user={}, {className='ai-avatar',tone=''}={}){const name=user.name||user.username||'Клиент',initials=String(name).trim().split(/\s+/).slice(0,2).map(word=>Array.from(word)[0]||'').join('').toUpperCase();const url=String(user.avatar||'');const safe=/^\/api\/web\/admin\/users\/[1-9]\d*\/avatar$/.test(url)||url==='/assets/freeman-mark-20261029.png';return `<span class="fo-avatar ${escape(className)}" data-tone="${escape(tone)}" aria-hidden="true"><span class="fo-avatar-initials">${escape(initials||'F')}</span>${safe?`<img src="${escape(url)}" alt="" loading="lazy" decoding="async">`:''}</span>`;}
const destinations={
 customer:[['profile','Профиль','profile'],['keys','Мои ключи','keys'],['plans','Тарифы','cart'],['support','Поддержка','support']],
 admin:[['admin-dashboard','Профиль','profile'],['admin-search','Поиск','search'],['admin-clients','Клиенты','users'],['admin-keys','Ключи','keys'],['subscriptions','Подписки','plans'],['support','Поддержка','support'],['admin-reports','Отчётность','chart'],['admin-payments','Платежи','wallet'],['admin-posts','Публикации','file-text'],['admin-catalog','Тарифы и акции','plans'],['referrals','Реферальная программа','users'],['admin-servers','Сервисы','router'],['admin-proxy','Telegram Proxy','telegram'],['/admin/assistant','Freeman Assistant','assistant'],['/admin/ai','Freeman AI','support']]
};
const adminNavigationGroups=[['Работа',['admin-search','admin-clients','support']],['Бизнес',['subscriptions','admin-reports','admin-payments','admin-posts','admin-catalog','referrals']],['Инфраструктура',['admin-servers']],['AI-инструменты',['/admin/assistant','/admin/ai']]];
function destinationURL(key){if(!Object.values(destinations).flat().some(item=>item[0]===key))return '/';return key.startsWith('/')?key:'/?cabinet_view='+encodeURIComponent(key)}
function navigation({role='customer',active='',renderItem}={}){
 const items=destinations[role]||destinations.customer;
 const render=renderItem||(item=>{const [key,label,name]=item,aliases={profile:'user',support:'headset',plans:'receipt',keys:'key',assistant:'star'};return `<a class="fo-button fo-button--plain fo-nav-item" href="${destinationURL(key)}" ${key===active?'aria-current="page"':''}>${icon(aliases[name]||name)}<span>${escape(label)}</span></a>`});
 const item=key=>render(items.find(row=>row[0]===key));
 return role==='admin'?item('admin-dashboard')+adminNavigationGroups.map(([label,keys])=>`<span class="sidebar-nav-label">${label}</span>${keys.map(item).join('')}`).join(''):items.map(render).join('');
}
function conversation({name,identity='',preview='',avatar:person={},when='',datetime='',status='',tone='neutral',unread=0,attributes='',className=''}){return `<button type="button" class="fo-chat-row fo-row-action fo-scope ${escape(className)}" ${attributes}>${avatar({...person,name},{className:'support-person-avatar'})}<span class="fo-chat-row-body fo-conversation-body ai-row-content"><span class="fo-chat-row-heading fo-conversation-heading ai-row-headline"><strong>${escape(name)}</strong>${identity?`<small>${escape(identity)}</small>`:''}</span><span class="fo-chat-row-preview fo-conversation-preview ai-row-preview">${escape(preview||'Нет сообщений')}</span></span><span class="fo-chat-row-meta fo-conversation-meta"><time datetime="${escape(datetime)}">${escape(when)}</time>${status?`<span class="fo-chat-row-status fo-status" data-tone="${escape(tone)}"><i class="fo-status-dot" aria-hidden="true"></i>${escape(status)}</span>`:''}${unread?`<b class="fo-chat-unread" aria-label="Новых сообщений: ${Number(unread)}">${Number(unread)}</b>`:''}</span></button>`}
function composer({className='',attributes='',text='',attachment='',tools='',files='',tail='',disabled=false,formatting=false}){return `<form class="fo-chat-composer ${escape(className)}" data-fo-chat-composer ${attributes}>${files}${attachment}<textarea name="text" rows="1" aria-label="Сообщение" placeholder="Сообщение…" maxlength="3500">${escape(text)}</textarea><div class="fo-chat-tools">${tools}${formatting?'<button type="button" class="fo-button fo-chat-control" data-cm-format aria-label="Форматирование сообщения" title="Форматирование">Aa</button>':''}<button type="submit" class="fo-button fo-icon-button fo-chat-send ai-send native-send support-send-glass" aria-label="Отправить сообщение" ${disabled?'disabled':''}>${icon('send')}</button></div>${tail}</form>`;}
function richText(text){
 const inline=(raw,depth=0)=>{if(depth>5)return escape(raw);const token=/(`[^`\n]+`|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\)|\*\*[^*\n]+\*\*|\*[^*\n]+\*|~~[^~\n]+~~|\|\|[^\n]+?\|\||https?:\/\/[^\s<>]+)/g;let out='',last=0;for(const m of raw.matchAll(token)){out+=escape(raw.slice(last,m.index));const v=m[0];if(v[0]==='`')out+='<button type="button" class="cm-code" data-cm-copy="'+escape(v.slice(1,-1))+'" aria-label="Скопировать фрагмент">'+escape(v.slice(1,-1))+'</button>';else if(v.startsWith('**'))out+='<strong>'+inline(v.slice(2,-2),depth+1)+'</strong>';else if(v.startsWith('~~'))out+='<s>'+inline(v.slice(2,-2),depth+1)+'</s>';else if(v.startsWith('||'))out+='<button type="button" class="cm-spoiler" data-cm-spoiler aria-expanded="false" aria-label="Показать скрытый текст"><span aria-hidden="true">'+escape(v.slice(2,-2))+'</span></button>';else if(v[0]==='*')out+='<em>'+inline(v.slice(1,-1),depth+1)+'</em>';else{const link=v.match(/^\[([^\]]+)\]\((.+)\)$/);let url;try{url=new URL(link?link[2]:v)}catch{}out+=url&&['https:','http:'].includes(url.protocol)&&!url.username&&!url.password?'<a target="_blank" rel="noopener noreferrer" href="'+escape(url.href)+'">'+escape(link?link[1]:v)+'</a>':escape(v)}last=m.index+v.length}return out+escape(raw.slice(last))};
 const lines=String(text||'').slice(0,20000).split('\n');let out='',list=null;const end=()=>{if(list){out+='</'+list+'>';list=null}};
 for(let i=0;i<lines.length;i++){const line=lines[i];if(line.startsWith('```')){end();let block=[];while(++i<lines.length&&!lines[i].startsWith('```'))block.push(lines[i]);const value=block.join('\n');out+='<div class="cm-code-block"><button type="button" data-cm-copy="'+escape(value)+'">Копировать код</button><pre><code>'+escape(value)+'</code></pre></div>';continue}if(line.includes('|')&&i+1<lines.length&&/^\s*\|?\s*:?-{3,}/.test(lines[i+1])){end();const cells=x=>x.trim().replace(/^\||\|$/g,'').split('|').map(v=>v.trim());const cols=cells(line);i++;let rows='';while(i+1<lines.length&&lines[i+1].includes('|'))rows+='<tr>'+cells(lines[++i]).map(v=>'<td>'+inline(v)+'</td>').join('')+'</tr>';out+='<div class="cm-table" tabindex="0" role="region" aria-label="Таблица, прокрутка по горизонтали"><table><thead><tr>'+cols.map(v=>'<th scope="col">'+inline(v)+'</th>').join('')+'</tr></thead><tbody>'+rows+'</tbody></table></div>';continue}const field=line.match(/^(.*?(?:Улица|Город|Регион|Индекс|Телефон):)(?:\*\*|__)?\s*(.+)$/);if(field){field[1]=field[1].replace(/[*_]/g,'');field[2]=field[2].replace(/^`(.*)`$/,'$1').replace(/^(?:\*\*|__)(.*)(?:\*\*|__)$/,'$1');end();out+='<p class="cm-copy-field"><span>'+inline(field[1])+'</span> <button type="button" class="cm-code" data-cm-copy="'+escape(field[2])+'" aria-label="Скопировать: '+escape(field[2])+'">'+escape(field[2])+'</button></p>';continue}const li=line.match(/^\s*(?:([-*])|(\d+)\.)\s+(.+)/);if(li){const type=li[2]?'ol':'ul';if(list!==type){end();out+='<'+type+'>';list=type}out+='<li'+(li[2]?' value="'+li[2]+'"':'')+'>'+inline(li[3])+'</li>';continue}end();if(line.startsWith('> '))out+='<blockquote>'+inline(line.slice(2))+'</blockquote>';else if(/^#{1,4} /.test(line))out+='<h4>'+inline(line.replace(/^#{1,4} /,''))+'</h4>';else if(line.trim())out+='<p>'+inline(line)+'</p>';}
 end();return '<div class="cm-rich">'+out+'</div>';
}
// Preserve the mobile keyboard when Send is pressed; polling never focuses a field.
document.addEventListener('pointerdown',event=>{const send=event.target.closest('.fo-chat-composer [type=submit]');if(send&&!send.disabled&&document.activeElement===send.closest('form')?.querySelector('textarea'))event.preventDefault()});
let cmAudio=null;const soundOn=()=>{try{return localStorage.getItem('fo-chat-sound')!=='off'}catch{return true}};
function prepareChatSound(){if(!soundOn())return;try{const A=window.AudioContext||window.webkitAudioContext;if(A){cmAudio||=new A();if(cmAudio.state==='suspended')void cmAudio.resume().catch(()=>{})}}catch{}}
function sentChatSound(){if(!soundOn()||!cmAudio||cmAudio.state!=='running')return;try{const at=cmAudio.currentTime,o=cmAudio.createOscillator(),g=cmAudio.createGain();o.type='sine';o.frequency.setValueAtTime(560,at);o.frequency.exponentialRampToValueAtTime(880,at+.085);g.gain.setValueAtTime(.0001,at);g.gain.exponentialRampToValueAtTime(.022,at+.015);g.gain.exponentialRampToValueAtTime(.0001,at+.15);o.connect(g);g.connect(cmAudio.destination);o.start(at);o.stop(at+.16);o.onended=()=>{o.disconnect();g.disconnect()}}catch{}}
function cmFeedback(text){let node=document.querySelector('#cm-feedback');if(!node){node=document.createElement('div');node.id='cm-feedback';node.className='cm-feedback';node.setAttribute('role','status');document.body.append(node)}node.textContent=text;clearTimeout(node.timer);node.timer=setTimeout(()=>node.remove(),1800)}
let activeCmMenu=null;
function cmMenu(anchor,items,row=false){
 if(activeCmMenu)FreemanUI.close(activeCmMenu.root,{force:true,restoreFocus:false});
 const root=document.createElement('div');root.className='cm-menu-layer fo-scope';root.dataset.foOverlay='';const panel=document.createElement('div');panel.className='cm-floating-menu'+(row?' is-row':'');panel.dataset.foPanel='';panel.setAttribute('role','menu');panel.setAttribute('aria-label','Действия');
 panel.innerHTML=items.map((x,i)=>'<button type="button" role="menuitem" data-cm-action="'+i+'">'+(x.icon?icon(x.icon):'')+'<span>'+escape(x.label)+'</span></button>').join('');root.append(panel);document.body.append(root);
 let closing=false;const outside=e=>{if(!root.contains(e.target)&&!anchor.contains(e.target))FreemanUI.close(root,{force:true})};const closeOnScroll=e=>{if(!root.contains(e.target))FreemanUI.close(root,{force:true,restoreFocus:false})};
 const record=FreemanUI.mount(root,{modal:false,kind:'menu',opener:anchor,onClose:()=>{closing=true;root.remove();document.removeEventListener('pointerdown',outside,true);document.removeEventListener('scroll',closeOnScroll,true);if(activeCmMenu===record)activeCmMenu=null}});record.anchor=anchor;activeCmMenu=record;
 const rect=anchor.getBoundingClientRect(),height=panel.offsetHeight,width=panel.offsetWidth,v=window.visualViewport,chat=anchor.closest('.fo-chat-workspace,.native-chat')?.getBoundingClientRect(),upper=Math.max(8,(v?.offsetTop||0)+8,chat?.top||0),lower=Math.min((v?.offsetTop||0)+(v?.height||innerHeight),chat?.bottom||Infinity)-8;panel.style.left=Math.max(8,Math.min(innerWidth-width-8,rect.left+(rect.width-width)/2))+'px';panel.style.top=Math.max(upper,Math.min(lower-height,rect.top-height-9>=upper?rect.top-height-9:rect.bottom+9))+'px';
 queueMicrotask(()=>{if(!closing){document.addEventListener('pointerdown',outside,true);document.addEventListener('scroll',closeOnScroll,true)}});
 panel.addEventListener('click',async e=>{const b=e.target.closest('[data-cm-action]');if(!b)return;const item=items[Number(b.dataset.cmAction)];FreemanUI.close(root,{force:true});await item.action()});return record;
}
async function editChatMessage(marker){
 const id=Number(marker.dataset.cmEditId);if(!id)return;
 const source=marker.dataset.cmSource||'',version=Number(marker.dataset.cmEditVersion||0),sheet=FreemanUI.chatSheet({title:'Изменить сообщение',kind:'message-edit',content:'<form class="cm-edit-form"><textarea name="text" maxlength="3500" rows="5" aria-label="Текст сообщения">'+escape(source)+'</textarea><p class="cm-edit-note">Можно исправить в течение 30 минут после отправки.</p><p class="cm-edit-error" role="alert"></p><div><button type="button" class="fo-button" data-fo-dismiss>Отмена</button><button type="submit" class="fo-button">Сохранить</button></div></form>'});
 const form=sheet.root.querySelector('form');let busy=false;form.addEventListener('submit',async e=>{e.preventDefault();if(busy)return;busy=true;form.querySelectorAll('button').forEach(b=>b.disabled=true);const error=form.querySelector('.cm-edit-error');error.textContent='';try{const response=await fetch('/api/web/support/messages/'+id,{method:'PATCH',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:form.elements.text.value,expected_version:version}),signal:AbortSignal.timeout(20000)});const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'Не удалось сохранить сообщение');document.querySelectorAll('[data-cm-edit-id="'+id+'"]').forEach(node=>{node.dataset.cmSource=data.text;node.dataset.cmEditVersion=String(data.version);node.innerHTML=richText(data.text);const bubble=node.closest('.native-bubble,.ai-bubble,.fo-chat-message');if(data.edited_at&&bubble&&!bubble.querySelector('.cm-edited')){const footer=bubble.querySelector('footer,time');if(footer){const label=document.createElement('span');label.className='cm-edited';label.textContent='изменено';footer.prepend(label)}}});document.dispatchEvent(new CustomEvent('fo:message-edited',{detail:{id,text:data.text,version:data.version,edited_at:data.edited_at}}));FreemanUI.close(sheet.root,{force:true});cmFeedback('Сообщение изменено')}catch(e){error.textContent=e.name==='TimeoutError'?'Ответ не получен. Можно повторить сохранение без дублирования.':e.message}finally{busy=false;form.querySelectorAll('button').forEach(b=>b.disabled=false)}});
}
function messageActions(bubble){
 const marker=bubble.querySelector('[data-cm-source]');if(!marker)return;const source=marker.dataset.cmSource||'',items=[];
 if(source)items.push({label:'Скопировать',icon:'copy',action:async()=>{try{await navigator.clipboard.writeText(source);cmFeedback('Скопировано')}catch{cmFeedback('Не удалось скопировать. Выделите текст.')}}});
 if(marker.dataset.cmEditId&&Date.parse(marker.dataset.cmEditUntil)>Date.now())items.push({label:'Изменить',icon:'edit',action:()=>editChatMessage(marker)});
 if(items.length)cmMenu(bubble,items,true);
}
function bubbleFrom(target){if(target.closest('button,a,input,textarea,video,audio,summary,.cm-menu-layer,.cm-reply-preview'))return null;return target.closest('.native-bubble,.ai-bubble,.fo-chat-message')}
document.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.matches('[data-cm-source]')){e.preventDefault();const bubble=e.target.closest('.native-bubble,.ai-bubble,.fo-chat-message');if(bubble)messageActions(bubble)}});
let cmHold=null,cmSuppress=0,cmLongActive=false;
document.addEventListener('pointerdown',e=>{const bubble=bubbleFrom(e.target);if(!bubble||e.button!==0)return;cmHold={x:e.clientX,y:e.clientY,id:e.pointerId,bubble,timer:setTimeout(()=>{cmLongActive=true;cmSuppress=Date.now()+900;messageActions(bubble);cmHold=null},700)}});
const cancelCmHold=e=>{if(cmLongActive&&e?.type==='pointerup'){cmSuppress=Date.now()+600;cmLongActive=false}if(cmHold)clearTimeout(cmHold.timer);cmHold=null};document.addEventListener('pointermove',e=>{if(cmHold&&(Math.abs(e.clientX-cmHold.x)>8||Math.abs(e.clientY-cmHold.y)>8))cancelCmHold()},{passive:true});document.addEventListener('pointerup',cancelCmHold);document.addEventListener('pointercancel',cancelCmHold);document.addEventListener('scroll',cancelCmHold,{capture:true,passive:true});
document.addEventListener('contextmenu',e=>{const bubble=bubbleFrom(e.target);if(bubble){e.preventDefault();cancelCmHold();if(Date.now()>=cmSuppress)messageActions(bubble)}});
document.addEventListener('click',async event=>{
 const format=event.target.closest('[data-cm-format]');if(format){const field=format.closest('form').querySelector('textarea');if(!field||field.disabled||field.readOnly)return;const start=field.selectionStart,end=field.selectionEnd;const items=[['**','Жирный'],['*','Курсив'],['`','Копируемый текст'],['||','Спойлер'],['> ','Цитата']].map(([mark,label])=>({label,action:()=>{const selected=field.value.slice(start,end)||'текст',value=mark==='> '?'> '+selected.replace(/\n/g,'\n> '):mark+selected+mark;if(field.value.length-(end-start)+value.length>3500)return cmFeedback('Сообщение слишком длинное');field.setRangeText(value,start,end,'select');field.dispatchEvent(new Event('input',{bubbles:true}));field.focus({preventScroll:true})}}));items.push({label:(soundOn()?'Выключить':'Включить')+' звук отправки',action:()=>{try{localStorage.setItem('fo-chat-sound',soundOn()?'off':'on')}catch{}cmFeedback(soundOn()?'Звук включён':'Звук выключен')}});cmMenu(format,items);return}
 const copy=event.target.closest('[data-cm-copy]');if(copy){try{await navigator.clipboard.writeText(copy.dataset.cmCopy);cmFeedback('Скопировано')}catch{cmFeedback('Не удалось скопировать')}return}
 const spoiler=event.target.closest('[data-cm-spoiler]');if(spoiler){spoiler.setAttribute('aria-expanded','true');spoiler.querySelector('span').removeAttribute('aria-hidden');return}
 if(Date.now()<cmSuppress||window.getSelection()?.toString())return;const bubble=bubbleFrom(event.target);if(bubble)messageActions(bubble);
});

const cmRead=new Map();let cmReading=false;
async function acknowledgeChatRead(){if(cmReading||document.hidden||!document.hasFocus())return;const seen=new Map();for(const node of document.querySelectorAll('[data-support-ticket][data-support-read-id]')){if(node.closest('[inert],[hidden]'))continue;const r=node.getBoundingClientRect(),ticket=Number(node.dataset.supportTicket),id=Number(node.dataset.supportReadId);const container=node.closest('.ai-messages,.native-history,.native-chat-messages,.support-messages')?.getBoundingClientRect();const top=Math.max(0,container?.top||0),bottom=Math.min(innerHeight,container?.bottom||innerHeight);if(ticket>0&&id>0&&r.bottom>top&&r.top<bottom&&r.width>0)seen.set(ticket,Math.max(id,seen.get(ticket)||0))}cmReading=true;try{for(const [ticket,id]of seen){if((cmRead.get(ticket)||0)>=id)continue;const response=await fetch('/api/web/support/tickets/'+ticket+'/read',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({message_id:id})});if(response.ok)cmRead.set(ticket,id)}}catch{}finally{cmReading=false}}
setInterval(acknowledgeChatRead,2500);

function messageBody({text='',source=null,media='',actions='',inline=null,footer='',editId=null,editVersion=0,editUntil='',edited=false}){const supplied=source===null?null:String(source),placeholder=!!media&&/^(?:📎\s*)?(?:(?:Клиент|Оператор)\s+(?:приложил|прикрепил)\s+(?:файл|вложение|фото|фотографию|видео)|Вложение|Фото|Видео)\.?$/iu.test((supplied||'').trim()),raw=placeholder?'':supplied,meta=raw===null?'':`tabindex="0" aria-keyshortcuts="Enter" data-cm-source="${escape(raw)}" ${Number(editId)>0?`data-cm-edit-id="${Number(editId)}" data-cm-edit-version="${Number(editVersion)||0}" data-cm-edit-until="${escape(editUntil)}"`:''}`;return `<div class="ai-message-media native-media fo-chat-media-list">${media}</div><div class="ai-message-text fo-message-text" ${meta}>${raw===null?text:richText(supportChoiceText(raw,inline))}</div><div class="cm-message-actions">${inline===null?actions:inlineActions(inline)}</div>${edited&&footer?footer.replace(/(<(?:time|footer)\b[^>]*>)/,'$1<span class="cm-edited">изменено</span>'):footer}`;}

  const tabGroups='.fo-tabs,.fa-task-tabs,[role=tablist],.ra-tabs,.fo-infra-tabs,.pay-periods,.report-periods,.admin-periods,.segment-control,.catalog-tabs,.customer-tabs,.proxy-status-filters,.px-tabs,.ai-tabs,.ai-routing-tabs,.pay-tabs,.pub-tabs,.pub-client-tabs,.support-settings-tabs,.analytics-view-switch,.analytics-periods,.grant-recipient-switch,.ref-period';
  const rowControls='.pp-qr,.fo-row-action,.contact-row,.fo-nav-item,.fo-choice,.fo-disclosure,.fo-conversation,.fo-tab,.ma-row,.ma-identity,.ma-account,.ma-balance,.profile-wallet-card,.telegram-glass-card,.list-row,.faq__q,.customer-subscription-toggle,.customer-devices-toggle,.px-server-row,.ai-row,.post-list-row,.cp-section-toggle';
  const menuSurfaces='.fa-chat-options,.pub-menu,.pub-row-menu,.px-menu,.px-popover';
  const cardSurfaces='.data-card,.admin-card,.catalog-panel,.ai-panel,.fa-card,.money-hero,.login-card,.unified-card,.card,.pay-kpi,.analytics-card,.proxy-admin-card,.pp-card,.support-bottom-sheet,.pay-detail,.pay-filter,.px-drawer';
  const adminViews=new Set(['admin-clients','admin-customer','admin-key','admin-catalog','admin-payments','admin-reports','admin-posts','admin-referrals','admin-servers','admin-keys','admin-proxy','admin-proxy-server']);
  let adminContext=false;
  function adminWorkspace(view,content,isAdmin=false){adminContext=adminViews.has(view)||(isAdmin&&view==='referrals');return adminContext?`<div class="fo-admin-workspace fo-scope" data-fo-admin-workspace="${escape(view)}">${content}</div>`:content;}
  // Shared admin composition. Domains supply content/actions, not a second UI kit.
  function adminHeader({title,description='',actions='',back='',className=''}){
    return `<header class="fo-admin-header ${escape(className)}" data-fo-admin-header><div class="fo-admin-heading">${back}<div class="fo-admin-title"><h1>${escape(title)}</h1>${description?`<p>${escape(description)}</p>`:''}</div></div>${actions?`<div class="fo-admin-header-actions">${actions}</div>`:''}</header>`;
  }
  // Pages compose the same desk; controllers keep their existing state and hooks.
  function adminScreen({title,description='',actions='',headingClass='',classes='',deskClass='',nav='',summary='',tools='',content='',footer='',after='',busy=false}){
    return `<section class="fo-admin-screen fo-scope ${escape(classes)}" data-fo-admin-page aria-busy="${!!busy}">${adminHeader({title,description,actions,className:headingClass})}<div class="fo-admin-desk ${escape(deskClass)}">${nav?`<div class="fo-admin-desk-nav">${nav}</div>`:''}${summary?`<div class="fo-admin-desk-summary">${summary}</div>`:''}${tools?`<div class="fo-admin-desk-tools">${tools}</div>`:''}<div class="fo-admin-body">${content}</div>${footer?`<div class="fo-admin-desk-footer">${footer}</div>`:''}</div>${after}</section>`;
  }

  function adminPresentation(root){
    const inAdmin=node=>node.closest('.fo-admin-workspace,.fo-admin-controls');
    const apply=(selector,classes)=>root.querySelectorAll(selector).forEach(n=>{if(inAdmin(n)&&!n.matches('.fo-admin-desk')&&!(n.closest('.fo-flat-content .fo-admin-desk')&&['fo-admin-panel','fo-admin-metrics','fo-admin-metric'].includes(classes)))n.classList.add(...classes.split(' '))});
    apply('.catalog-tabs,.ra-tabs,.pub-tabs,.pay-tabs','fo-admin-tabs');
    apply('.crm-toolbar,.pay-toolbar,.pub-commandbar,.ra-toolbar,.analytics-controls,.catalog-list-tools','fo-admin-toolbar');
    apply('.crm-register,.catalog-panel,.pay-register,.pub-list-card,.ra-overview,.ra-personal-section,.ra-payouts,.ra-invitations,.analytics-card,.cp-card,.digest-settings','fo-admin-panel');
    apply('.pay-kpis,.analytics-primary,.ra-metrics','fo-admin-metrics');
    apply('.pay-kpi,.analytics-kpi,.ra-metrics>article','fo-admin-metric');
    apply('.pay-table,.pub-table,.ra-table','fo-admin-table');
  }
  function adopt(root){
    if(!root)return root;
    window.FreemanChatMedia?.enhance(root);
    const nodes=selector=>[...(root.matches?.(selector)?[root]:[]),...root.querySelectorAll(selector)];
    if(root===document.body)root.classList.add('fo-document');
    nodes('.fo-plans,.fo-catalog,.guest-checkout,.fo-keys,.fo-key-dialog,.fo-rename').forEach(node=>node.classList.add('fo-type-medium'));
    nodes('.fo-overlay,.modal-backdrop,.pub-portal,.pub-editor,.pay-portal,#crm-campaign-root,.px-overlay').forEach(node=>node.classList.toggle('fo-admin-controls',adminContext));
    nodes(tabGroups).forEach(group=>{
      group.classList.add('fo-tabs');if(group.matches('.ai-routing-tabs'))group.classList.add('fo-tabs--compact');
      group.querySelectorAll(':scope>button').forEach(button=>{
        if(button.matches('[aria-haspopup]')&&!button.hasAttribute('aria-pressed')&&!button.hasAttribute('aria-selected')){button.classList.add('fo-tab-action');return}
        [...button.classList].filter(c=>c==='fo-button'||c.startsWith('fo-button--')||c==='fo-icon-button'||c==='pressable').forEach(c=>button.classList.remove(c));
        button.classList.add('fo-tab','fo-scope');
      });
      requestAnimationFrame(()=>{if(!group.isConnected||group.scrollWidth<=group.clientWidth)return;const active=group.querySelector(':scope>[aria-selected=true],:scope>[aria-pressed=true]');if(!active)return;const a=active.getBoundingClientRect(),g=group.getBoundingClientRect();if(a.right>g.right-4)group.scrollLeft+=a.right-g.right+4;else if(a.left<g.left+4)group.scrollLeft+=a.left-g.left-4});
    });
    nodes('.fa-shade,.support-sheet-dismiss,.crm-drawer-scrim,.pay-filter-veil,.pay-detail-veil,.pub-editor-veil').forEach(node=>node.classList.add('fo-backdrop','fo-scope'));
    nodes(menuSurfaces).forEach(node=>node.classList.add('fo-menu','fo-menu-surface','fo-scope'));
    nodes('.ql-toolbar,.pub-rich-toolbar').forEach(node=>{node.classList.add('fo-editor-toolbar');node.dataset.foComponent='editor-toolbar';node.querySelectorAll('button').forEach(b=>{[...b.classList].filter(c=>c==='fo-button'||c==='fo-icon-button'||c.startsWith('fo-button--')).forEach(c=>b.classList.remove(c));b.classList.add('fo-editor-tool');b.dataset.foComponent='editor-tool'})});
    nodes('.fa-composer,.native-composer,.ai-compose').forEach(node=>node.classList.add('fo-input-group','fo-scope'));
    nodes('.option[role=radio]').forEach(node=>{node.classList.add('fo-choice','fo-scope','fo-choice--compact');node.querySelector('.radio')?.classList.add('fo-choice-mark')});
    nodes('button.has-border-beam').forEach(button=>{button.classList.remove('has-border-beam','beam-periodic','beam-blue');button.querySelector(':scope>.border-beam')?.remove()});
    nodes('.contact-row').forEach(node=>node.classList.add('fo-data-row'));
    nodes(cardSurfaces).forEach(node=>{if(!(node.closest('.fo-flat-content .fo-admin-desk')&&node.matches('.analytics-card,.data-card,.admin-card,.proxy-admin-card')))node.classList.add('fo-card','fo-scope')});
    nodes('button,summary,a.btn,a.primary,a.secondary,a.checkout-button,a.unified-buy,a.pp-button,a.fo-button,a.account-link,a.unified-text,a.back-link,a.customer-contact-action,a.ma-row,a.list-row,a[role=button],.fa-actions a,.fa-entity-actions a,.fa-file nav a').forEach(button=>{
      if(button.matches('.fo-photo-open,.fo-photo-transfer')){button.dataset.foComponent='photo-control';return;}
      if(button.matches('.fo-key-action')){button.classList.add('fo-scope');return;}
      if(button.matches('.cp-disclosure>summary')){button.classList.add('fo-disclosure','fo-scope');return;}
      if(button.closest('.ql-toolbar,.pub-rich-toolbar')||button.matches('.fo-tab,.fo-backdrop'))return;
      if(button.matches(rowControls)){if(!button.matches('.fo-nav-item,.fo-choice,.fo-disclosure,.fo-conversation'))button.classList.add('fo-row-action');return;}
      button.classList.add('fo-button','fo-scope');button.classList.remove('pressable');
      if(button.matches('.unified-text,.back-link'))button.classList.add('fo-button--plain');
      if(button.closest('.cp-workspace,.cp-dialog,.fo-support-shortcuts,.fo-support-filters,.fo-catalog,.payments-page,.publications-page,.pub-editor,.pub-button-dialog,.fo-insights,.digest-settings,.fo-notice-toolbar,.fo-notice-preferences,.ai-editor-actions,.ai-editor-toolbar,.fa-file,.fa-response-tools,.fa-table-block'))button.classList.add('fo-button--compact');
      if(button.closest('.fo-support-shortcuts,.cp-actions'))button.classList.add('fo-button--nowrap');
      if(button.matches('.fa-image-button,.fo-chat-photo')){button.classList.add('fo-button--media');button.classList.remove('fo-icon-button')}
      if(button.closest('.fa-file'))button.classList.add('fo-button--compact');
      if(button.matches('.primary,.btn-primary,.cp-primary,.catalog-primary,.pub-primary,.fa-primary,.unified-primary,.pp-blue,.proxy-action.primary,[data-new]:not(.fo-button--plain)'))button.classList.add('fo-button--primary');
      if(button.matches('.pp-green,[data-checkout],#purchase,[data-key-renew],[data-plan],.buy-key,.keys-buy-row,.keys-renew,.checkout-button:not(.checkout-edit-button),[data-confirm-checkout],[data-proxy-buy],[data-topup],[data-shortfall-topup],[data-cp-action=credit],[data-customer-proxy-renew],[data-renewal]'))button.classList.add('fo-button--success');
      // One purchase/renewal material; domains retain layout only.
      if(button.matches('.my-keys-buy,[data-px-purchase],[data-px-buy],[data-customer-proxy-renew],[data-key-renew],.keys-renew,[data-renewal],#purchase,[data-checkout],[data-confirm-checkout],.buy-key,.keys-buy-row,.checkout-button:not(.checkout-edit-button),[data-customer-key-action="renew"],.customer-confirm footer [data-customer-confirm-submit]')&&(!button.matches('[data-customer-confirm-submit]')||button.closest('.customer-confirm')?.querySelector('.customer-renew-options'))){
        button.classList.remove('fo-button--primary','fo-button--primary-solid','fo-button--success-solid','fo-button--flat','fo-button--plain');
        button.classList.add('fo-button--success','fo-button--glass');button.dataset.foComponent='purchase-action';
      }
      if(button.matches('.danger,.px-danger,.pub-danger-text,.cp-danger,.keys-danger,.key-dialog-danger,[data-delete],[data-catalog-delete],[data-admin-key-operation=delete],[data-remove-card],[data-device-disconnect],[data-keys-unlink],[data-customer-unlink],[data-support-delete-ticket],[data-delete-news],[data-delete-link],[data-quick-delete]'))button.classList.add('fo-button--danger');
      if(button.matches('[data-key-install],[data-copy-install],[data-download-install],.key-install,[download]'))button.classList.add('fo-button--primary');
      if(!button.matches('.fo-button--media')&&(button.getAttribute('aria-label')&&!button.textContent.trim()&&button.querySelector('svg,img')||button.matches('[aria-label="Закрыть"],[data-chat-options],[data-attach],.fa-menu')))button.classList.add('fo-icon-button');
    });
    nodes('input,select,textarea').forEach(input=>{
      if(['hidden','file'].includes(input.type)||input.closest('.ql-toolbar,.unified-code'))return;
      if(['range','color','submit','button'].includes(input.type)){input.classList.add(input.type==='range'?'fo-range':input.type==='color'?'fo-color':'fo-button');input.dataset.foComponent=input.type;return;}
      input.classList.add('fo-scope');
      if(input.type==='checkbox'&&input.closest('.control-switch,.pub-toggle')){
        input.classList.add('fo-switch-input');const group=input.closest('.control-switch,.pub-toggle');group.classList.add('fo-native-switch');group.querySelector(':scope>i,:scope>.control-slider')?.remove();
      }else input.classList.add(input.type==='checkbox'?'fo-check':input.type==='radio'?'fo-radio':input.tagName==='SELECT'?'fo-select':'fo-input');
      const label=input.closest('label');if(label&&input.type!=='checkbox'&&input.type!=='radio'){if(label.querySelector(':scope>svg'))label.classList.add('fo-input-group','fo-scope');else if(!label.classList.contains('fo-input-group'))label.classList.add('fo-field');}
    });
    nodes('.tariff-badge').forEach(node=>node.classList.add('fo-badge','fo-badge--success','fo-badge--compact'));
    nodes('[role=status],[role=alert]').forEach(node=>{node.dataset.foComponent='feedback'});
    nodes('.fo-status,.status-badge,.ai-mode-badge,.native-status,.key-status,.pay-status').forEach(node=>{node.dataset.foComponent='status'});
    nodes('dialog:not(.fa-image-viewer) :is(button,summary),.fo-dialog :is(button,summary)').forEach(node=>node.classList.add('fo-button--flat'));
    nodes('.fa-response-tools').forEach(node=>node.classList.add('fo-message-tools'));
    nodes('dialog:not(.fa-image-viewer)').forEach(panel=>panel.classList.add('fo-dialog','fo-scope'));
    nodes('dialog>header').forEach(header=>header.classList.add('fo-dialog-header'));
    nodes('.fa-sidebar button,.fa-header button,.ai-sidebar nav button').forEach(node=>node.classList.add('fo-button--plain'));
    nodes('.fo-button,.fo-tab,.fo-row-action,.fo-input,.fo-select,.fo-check,.fo-radio,.fo-card,.fo-dialog,.fo-menu').forEach(node=>{node.dataset.foComponent=node.classList.contains('fo-tab')?'tab':node.classList.contains('fo-row-action')?'row':node.classList.contains('fo-input')||node.classList.contains('fo-select')?'field':node.classList.contains('fo-card')?'card':node.classList.contains('fo-dialog')?'dialog':node.classList.contains('fo-menu')?'menu':node.classList.contains('fo-check')?'checkbox':node.classList.contains('fo-radio')?'radio':'button'});
    nodes('.fa-composer,.native-composer,.ai-compose').forEach(node=>{node.classList.add('fo-chat-composer');node.dataset.foComponent='composer';for(const b of node.querySelectorAll('button:not(.fa-voice-panel button)')){b.classList.add('fo-chat-control');if(b.type==='submit'){b.classList.add('fo-chat-send');if(!b.dataset.foSendIcon){b.innerHTML=icon('send');b.dataset.foSendIcon='true'}}else if(b.matches('[data-attach],[data-chat-attachments],[data-ai-attach]'))b.innerHTML=icon('attach');}});
    nodes('.native-header,.ai-chat-head,.fa-header').forEach(node=>node.classList.add('fo-chat-header'));
    nodes('.native-bubble,.ai-bubble,.fa-message').forEach(node=>{node.classList.add('fo-chat-message');node.dataset.foComponent='message';const host=node.closest('.native-message');node.dataset.direction=host?.classList.contains('is-system')||node.classList.contains('system')?'system':host?host.classList.contains('is-outgoing')?'outgoing':'incoming':node.classList.contains('client')?(node.matches('.fa-message')?'outgoing':'incoming'):'outgoing';if(node.matches('.fa-message.assistant'))node.dataset.direction='incoming';});
    nodes('[data-copy-message]').forEach(node=>{node.classList.add('fo-message-copy');node.dataset.foComponent='button';node.innerHTML=icon('copy')});
    nodes('.native-chat,.fo-chat-workspace,#assistant-app').forEach(node=>node.classList.add('fo-chat-host'));
    nodes('.native-media,.ai-message-media').forEach(node=>node.classList.add('fo-chat-media-list'));
    nodes('.fa-search,.ai-search,.client-search,.pay-search,.px-search,.support-search').forEach(node=>node.classList.add('fo-search'));
    adminPresentation(root);
    return root;
  }


 document.addEventListener('error',event=>{if(event.target.matches?.('.fo-avatar img,.fo-quick-icon img'))event.target.remove()},true);
function noticeIcon(name, tone='info'){
  return `<span class="fo-notice-symbol notice-icon" data-tone="${escape(tone)}" aria-hidden="true">${icon(name)}</span>`;
}

function noticeRow({title,text,avatarUser,iconName='bell',tone='info',read=false,date='',attributes='',readAttributes=''}){
  return `<article class="fo-notice-item" data-read="${!!read}">
    <button type="button" class="animated-notice-card fo-notice-row fo-row-action fo-scope" ${attributes}>
      ${avatarUser?avatar(avatarUser,{className:'fo-notice-avatar'}):noticeIcon(iconName,tone)}
      <span class="notice-copy">
        <span class="notice-row-heading"><strong>${escape(title)}</strong>${date}</span>
        <span class="notice-preview-text">${escape(text)}</span>
      </span>
    </button>
    ${!read?`<button type="button" class="fo-button fo-icon-button fo-button--plain fo-notice-read" ${readAttributes} aria-label="Прочитано: ${escape(title)}" title="Отметить прочитанным">${icon('check')}</button>`:''}
  </article>`;
}

let currentNoticeBanner;
function dismissNoticeBanner(node=document.querySelector('.digest-toast'), immediate=false){
  if(!node)return;
  if(currentNoticeBanner?.node===node){
    clearTimeout(currentNoticeBanner.timer);
    currentNoticeBanner=null;
  }
  if(immediate){node.remove();return}
  if(node.inert)return;
  node.inert=true;
  node.setAttribute('aria-hidden','true');
  const animation=window.PageMotion?.animate(node,[
    {opacity:1,translate:'0 0',scale:1},
    {opacity:0,translate:'0 -12px',scale:.98}
  ],140,'notice-banner');
  if(animation)animation.finished.then(()=>node.remove(),()=>node.remove());
  else node.remove();
}

function showNoticeBanner({className='',avatarUser,iconName='bell',tone='info',label,title,text,openAttributes,dismissAttributes,duration=5000}){
  dismissNoticeBanner(document.querySelector('.digest-toast'),true);
  const node=document.createElement('aside');
  node.className=`digest-toast fo-notice-banner fo-scope ${className}`;
  node.setAttribute('role','status');
  node.setAttribute('aria-live','polite');
  node.innerHTML=`<button type="button" class="digest-toast-open" ${openAttributes}>
    ${avatarUser?avatar(avatarUser,{className:'fo-notice-avatar'}):noticeIcon(iconName,tone)}
    <span class="fo-notice-banner-copy">${label?'<small>'+escape(label)+'</small>':''}<strong>${escape(title)}</strong><span>${escape(text)}</span></span>
  </button><button type="button" class="fo-button fo-icon-button fo-button--plain fo-notice-close" ${dismissAttributes} aria-label="Скрыть уведомление">${icon('close')}</button>`;
  document.body.append(node);
  const record={node,timer:0};
  currentNoticeBanner=record;
  const pause=()=>clearTimeout(record.timer);
  const schedule=()=>{
    pause();
    if(currentNoticeBanner===record&&!node.matches(':hover')&&!node.contains(document.activeElement)){
      record.timer=setTimeout(()=>dismissNoticeBanner(node),duration-140);
    }
  };
  node.addEventListener('mouseenter',pause);
  node.addEventListener('mouseleave',schedule);
  node.addEventListener('focusin',pause);
  node.addEventListener('focusout',()=>queueMicrotask(schedule));
  window.PageMotion?.animate(node,[
    {opacity:0,translate:'0 -20px',scale:.97},
    {opacity:1,translate:'0 0',scale:1}
  ],360,'notice-banner');
  schedule();
  return node;
}

  // Reusable dashboard primitives: integer money arrives from server ledgers.
  function metric({label,value,note='',iconName='wallet',tone='blue'}){
    return `<article class="fo-stat-tile" data-tone="${escape(tone)}"><span class="fo-stat-symbol">${icon(iconName)}</span><div><span class="fo-stat-label">${escape(label)}</span><strong>${escape(value??'—')}</strong>${note?`<small>${escape(note)}</small>`:''}</div></article>`;
  }
  function chart({key,title,rows=[],value='value',date='date',money=false,note='',tone='blue'}){
    const points=rows.filter(r=>!r.future).map(r=>({date:String(r[date]||''),value:Number(r[value])||0})),format=n=>money?new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(n/100)+' ₽':new Intl.NumberFormat('ru-RU').format(n),label=p=>`${p.date.slice(8,10)}.${p.date.slice(5,7)} · ${format(p.value)}`;
    const max=Math.max(1,...points.map(p=>p.value)),x=i=>24+(points.length<2?316:i/(points.length-1)*632),y=n=>144-Math.max(0,n)/max*116;
    const line=points.map((p,i)=>`${x(i)},${y(p.value)}`).join(' '),last=points.length-1;
    return `<section class="fo-data-chart fo-dashboard-card" data-tone="${escape(tone)}" data-fo-chart="${escape(key)}" data-points="${escape(JSON.stringify(points.map(label)))}" data-coordinates="${escape(JSON.stringify(points.map((p,i)=>[x(i),y(p.value)])))}"><header><div><h2>${escape(title)}</h2>${note?`<p>${escape(note)}</p>`:''}</div><output data-fo-chart-value>${last>=0?escape(label(points[last])):'Нет данных'}</output></header>${points.length?`<div class="fo-chart-plot"><svg viewBox="0 0 680 164" role="img" aria-label="${escape(title)}. Точные значения доступны ниже в таблице."><g class="fo-chart-grid">${[28,86,144].map(h=>`<path d="M24 ${h}H656"/>`).join('')}</g>${points.length>1?`<polygon class="fo-chart-area" points="24,144 ${line} ${x(last)},144"/>`:``}<polyline class="fo-chart-line" points="${line}"/><line data-fo-chart-cursor class="fo-chart-cursor" x1="${x(last)}" x2="${x(last)}" y1="24" y2="144"/><circle data-fo-chart-selection class="fo-chart-selection" cx="${x(last)}" cy="${y(points[last].value)}" r="4"/><text x="24" y="18">${escape(format(max===1&&points.every(p=>!p.value)?0:max))}</text><text x="656" y="160" text-anchor="end">0</text></svg></div><div class="fo-chart-scan"><label for="chart-scan-${escape(key)}" class="sr-only">Дата на графике: ${escape(title)}</label><input type="range" id="chart-scan-${escape(key)}" data-fo-chart-scan min="0" max="${last}" value="${last}" step="1" ${last===0?'disabled':''} aria-valuetext="${escape(label(points[last]))}"><div><span>${escape(points[0].date)}</span><span>${escape(points[last].date)}</span></div></div><details class="fo-chart-table"><summary>Данные таблицей</summary><div class="fo-table-scroll"><table><thead><tr><th>Дата · МСК</th><th>${escape(title)}</th></tr></thead><tbody>${points.map(p=>`<tr><td>${escape(p.date)}</td><td>${escape(format(p.value))}</td></tr>`).join('')}</tbody></table></div></details>`:'<p class="fo-empty">Операций за выбранный период пока нет.</p>'}</section>`;
  }
  document.addEventListener('input',event=>{if(!event.target.matches('[data-fo-chart-scan]'))return;const root=event.target.closest('[data-fo-chart]');try{const labels=JSON.parse(root.dataset.points),text=labels[Number(event.target.value)]||'';root.querySelector('[data-fo-chart-value]').textContent=text;event.target.setAttribute('aria-valuetext',text);const [x,y]=JSON.parse(root.dataset.coordinates)[Number(event.target.value)],cursor=root.querySelector('[data-fo-chart-cursor]'),point=root.querySelector('[data-fo-chart-selection]');cursor.setAttribute('x1',x);cursor.setAttribute('x2',x);point.setAttribute('cx',x);point.setAttribute('cy',y)}catch{}});

  function purchaseTerms({tariffs=[],selectedId,scope='customer',discount=()=>''}){
    const selected=tariffs.find(t=>t.id===selectedId),devices=t=>t.device_limit??t.devices??1,price=t=>t.price??t.amount??0;
    const attr=scope==='guest'?'data-tariff':'data-plan';
    return tariffs.filter(t=>devices(t)===devices(selected||{})).map((t,i)=>`<button type="button" class="fo-purchase-choice option fo-subscription-choice ${t.id===selectedId?'selected':''}" role="radio" aria-checked="${t.id===selectedId}" tabindex="${t.id===selectedId?0:-1}" ${attr}="${t.id}"><span class="fo-purchase-radio" aria-hidden="true"></span><span class="fo-purchase-term">${escape(tariffTerm(t.months))}${i===1?'<small class="fo-value-badge">Популярный</small>':''}</span><span class="fo-purchase-price">${discount(t.base_price,price(t))}<strong>${new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(price(t)/100)} ₽</strong></span></button>`).join('');
  }
  function purchaseChoices({tariffs=[],selectedId,scope='customer',discount=()=>''}){
    const devices=t=>t.device_limit??t.devices??1,selected=tariffs.find(t=>t.id===selectedId),limits=[...new Set(tariffs.map(devices))].sort((a,b)=>(a||Infinity)-(b||Infinity)),limit=devices(selected||{}),attr=scope==='guest'?'data-device':'data-device-limit';
    return `<div class="fo-purchase-config" data-fo-purchase-config><section class="fo-purchase-group"><h2><span aria-hidden="true">${icon('phone')}</span>Сколько устройств?</h2><div class="fo-purchase-grid device-options" role="radiogroup" aria-label="Количество устройств">${limits.map(n=>`<button type="button" class="fo-purchase-choice option ${n===limit?'selected':''}" role="radio" aria-checked="${n===limit}" tabindex="${n===limit?0:-1}" ${attr}="${n}"><span class="fo-purchase-radio" aria-hidden="true"></span><span>${n===0?'Без лимита':n+' '+(n===1?'устройство':n<5?'устройства':'устройств')}</span></button>`).join('')}</div></section><section class="fo-purchase-group"><h2><span aria-hidden="true">${icon('calendar')}</span>На какой срок?</h2><div class="fo-purchase-grid options" role="radiogroup" aria-label="Срок подписки">${purchaseTerms({tariffs,selectedId,scope,discount})}</div></section></div>`;
  }
  document.addEventListener('keydown',event=>{const button=event.target.closest('[data-plan],[data-device-limit]');if(!button?.closest('[data-fo-purchase-config]')||!['ArrowDown','ArrowUp','ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const buttons=[...button.parentElement.querySelectorAll('[role="radio"]')],index=buttons.indexOf(button),next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(['ArrowDown','ArrowRight'].includes(event.key)?1:-1)+buttons.length)%buttons.length;buttons[next].click();const attr=buttons[next].hasAttribute('data-plan')?'data-plan':'data-device-limit';document.querySelector(`[${attr}="${buttons[next].getAttribute(attr)}"]`)?.focus()});
  function updateAntigOption(root,{available=false,selected=false,surcharge=0,disabled=false}={}){
    if(root)root.hidden=!available;
    const toggle=root?.querySelector('[data-antig-toggle]');if(!toggle)return;
    toggle.setAttribute('aria-checked',String(!!selected&&!!available));toggle.disabled=!available||disabled;
    root.querySelector('[data-antig-price]').textContent='+'+new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(surcharge/100)+' ₽';
    root.querySelector('p').textContent=available?'Дополнительный сервер для подключения при ограничениях интернета (БПЛА). Доплата указана за весь выбранный срок.':'Сейчас недоступна. Можно оформить обычную подписку.';
  }
  function antigOption({available=false,selected=false,surcharge=0,disabled=false}={}){
    if(!available)return '';
    const price=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(surcharge/100)+' ₽';
    return `<section class="fo-purchase-group fo-antig-card"><h2><span aria-hidden="true">${icon('shield-check')}</span>Нужна ли антиглушилка?</h2><div class="fo-antig-row"><span class="fo-antig-copy"><span>Добавить антиглушилку</span><small data-antig-price>+${price}</small></span><button type="button" class="fo-antig-toggle" data-antig-toggle role="switch" aria-checked="${selected&&available}" aria-label="Добавить антиглушилку" aria-describedby="antig-description" ${!available||disabled?'disabled':''}><span class="fo-antig-switch" aria-hidden="true"><i></i></span></button></div><p id="antig-description">${available?'Дополнительный сервер для подключения при ограничениях интернета (БПЛА). Доплата указана за весь выбранный срок.':'Сейчас недоступна. Можно оформить обычную подписку.'}</p></section>`;
  }
  function tariffPicker({tariffs=[],selectedId,mode='customer',view='list',disabled=false,discount=()=>''}){
    const selected=tariffs.find(t=>t.id===selectedId),devices=t=>t.device_limit||t.devices||1,price=t=>t.price??t.amount??0,word=n=>n===1?'устройство':n<5?'устройства':'устройств',money=n=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(n/100)+' ₽';
    const attribute=mode==='owner'?'data-shop-tariff':'data-plan',deviceAttribute=mode==='owner'?'data-shop-devices':'data-device-limit',viewAttribute=mode==='owner'?'data-shop-view':'data-tariff-view',limits=[...new Set(tariffs.map(devices))].sort((a,b)=>a-b),limit=selected?devices(selected):limits[0];
    return `<div class="fo-tariff-picker" data-fo-tariff-picker><section class="fo-card purchase-card device-card"><h2>Количество устройств</h2><div class="device-options">${limits.map(n=>`<button type="button" class="device-option fo-choice ${n===limit?'selected':''}" ${deviceAttribute}="${n}" aria-pressed="${n===limit}" ${disabled?'disabled':''}><span class="radio-dot fo-choice-mark" aria-hidden="true"></span>${n} ${word(n)}</button>`).join('')}</div></section><section class="fo-card purchase-card period-card"><div class="selection-heading"><h2>Срок подписки</h2><div class="tariff-view-toggle" aria-label="Вид тарифов"><button type="button" class="fo-button fo-icon-button" ${viewAttribute}="list" aria-label="Список" aria-pressed="${view==='list'}">${icon('menu')}</button><button type="button" class="fo-button fo-icon-button" ${viewAttribute}="grid" aria-label="Сетка" aria-pressed="${view==='grid'}">${icon('grid')}</button></div></div><div class="tariff-list ${view==='grid'?'tariff-grid':''}">${tariffs.filter(t=>devices(t)===limit).map((t,i)=>`<button type="button" class="fo-subscription-choice tariff-choice fo-choice ${t.id===selectedId?'selected':''}" ${attribute}="${t.id}" aria-pressed="${t.id===selectedId}" ${disabled?'disabled':''}><span class="radio-dot fo-choice-mark" aria-hidden="true"></span><span class="tariff-name">${escape(tariffLabel(t))}${i===1?'<b class="fo-value-badge">Популярный</b>':''}</span><span class="tariff-price">${discount(t.base_price,price(t))}<strong>${money(price(t))}</strong>${t.months>1?`<small>${money(Math.round(price(t)/t.months))}/мес</small>`:''}</span></button>`).join('')||'<p class="fo-empty">Доступных тарифов пока нет.</p>'}</div></section></div>`;
  }
  function keyAction({action,id,label,icon='',tile=false}={}){
    if(!['install','renew','upgrade','antig'].includes(action)||!Number.isSafeInteger(id)||id<1)throw Error('Invalid subscription action');
    const tone=!tile&&action==='renew'?' fo-button--success':'';
    return `<button type="button" data-fo-component="subscription-action" class="fo-button fo-button--compact fo-key-action${tile?' fo-key-action--tile':''}${tone}" data-key-${action}="${id}"${action==='antig'?' aria-label="Добавить антиглушилку к этой подписке"':''}><span class="fo-key-action-icon" aria-hidden="true">${icon}</span><span class="fo-key-action-label">${escape(label)}</span></button>`;
  }
 function chatDayKey(value){const d=new Date(value);return Number.isFinite(d.getTime())?new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(d):'';}
 function chatDayLabel(value,reference=new Date()){const key=chatDayKey(value);if(!key)return '';if(key===chatDayKey(reference))return 'Сегодня';if(key===chatDayKey(new Date(new Date(reference).getTime()-86400000)))return 'Вчера';return new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'long',year:new Date(value).getFullYear()!==new Date(reference).getFullYear()?'numeric':undefined}).format(new Date(value));}
 function serviceIcon(name){return ['youtube','whatsapp','gemini','proxy'].includes(name)?'<img class="fo-service-icon" src="/assets/service-'+name+'-20261115.svg" alt="" aria-hidden="true">':'';}
 document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-agent-token]');if(!button||button.disabled)return;
 event.preventDefault();const content=[...button.childNodes];const focusComposer=button.hasAttribute('data-chat-ask-service');if(focusComposer)button.closest('.native-chat')?.querySelector('[data-support-message] textarea')?.focus({preventScroll:true});button.disabled=true;button.textContent='Выполняем…';
 try{const response=await fetch('/api/web/support/agent-action',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:button.dataset.agentToken,decision:button.dataset.agentDecision||'open'})});const data=await response.json();if(!response.ok)throw Error(data.detail||'Действие не подтверждено.');document.dispatchEvent(new CustomEvent('fo-agent-result',{detail:{...data,focusComposer}}));button.replaceChildren(...content)}catch(error){button.replaceChildren(...content);let status=button.parentElement.querySelector('[data-agent-error]');if(!status){status=document.createElement('p');status.dataset.agentError='';status.setAttribute('role','alert');button.after(status)}status.textContent=error.message}finally{button.disabled=false}
 });
 const qrLoads=new Map();
 function loadAgentQRs(){document.querySelectorAll('img[data-agent-qr]:not([data-loading])').forEach(async image=>{image.dataset.loading='1';const token=image.dataset.agentQr;if(!/^[A-Za-z0-9_-]{32}$/.test(token))return;try{if(!qrLoads.has(token))qrLoads.set(token,fetch('/api/web/support/agent-qr',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})}).then(async r=>{if(!r.ok)throw Error('QR недоступен');return URL.createObjectURL(await r.blob())}).catch(e=>{qrLoads.delete(token);throw e}));image.src=await qrLoads.get(token)}catch{image.alt='Не удалось загрузить QR. Запросите новый в чате.'}})}
 new MutationObserver(loadAgentQRs).observe(document.documentElement,{childList:true,subtree:true});
 window.FreemanComponents={chatDayKey,chatDayLabel,serviceIcon,chatHeader,inlineActions,refreshChatSearch,richText,prepareChatSound,sentChatSound,acknowledgeChatRead,keyAction,purchaseTerms,purchaseChoices,antigOption,updateAntigOption,receipt,revealMessage,noticeRow,noticeIcon,showNoticeBanner,dismissNoticeBanner,metric,chart,tariffPicker,adminWorkspace,adminHeader,adminScreen,adopt,icon,sizeComposer,historyTail,tariffTerm,tariffLabel,isAutomaticTariffName,media,quickReply,escape,composer,messageBody,avatar,conversation,destinations,navigation,destinationURL};
})();
