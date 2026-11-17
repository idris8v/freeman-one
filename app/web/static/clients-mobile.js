/* Responsive presentation only; registry, exports and campaign audiences stay shared. */
(() => {
  const compact = () => matchMedia('(max-width:1099px)').matches;
  let sheet = null, segment = null;
  const page = adminClientsPage;
  adminClientsPage = function () {
    let html = page();
    if(state.extra.crmListError) html=html.replace('<div class="crm-rows fo-group">',`<div class="crm-list-error fo-feedback" data-tone="danger" role="alert">${esc(state.extra.crmListError)} <button class="fo-button" data-crm-retry>Повторить</button></div><div class="crm-rows fo-group">`);
    if(state.extra.crmListLoading) html=html.replace('<p class="crm-empty fo-empty">Клиенты не найдены</p>', '<div class="crm-skeleton-list" role="status" aria-label="Загрузка клиентов">'+Array.from({length:5},()=>'<div class="crm-skeleton-row fo-skeleton"><i></i><span></span></div>').join('')+'</div>');
    else if(!state.extra.crmListError) html=html.replace('Клиенты не найдены',crmFilters().q?'Ничего не найдено':'Нет клиентов по выбранным условиям');
    if (!compact()) return html;
    const doc = document.createElement('template'); doc.innerHTML = html;
    const root = doc.content;
    root.querySelector('.crm-filters')?.remove();
    const segments = root.querySelector('.crm-segments');
    const launch = document.createElement('button');
    launch.className = 'crm-segment-launch fo-button'; launch.dataset.crmMobileSegments = '';
    launch.innerHTML = `Сегменты ${actionIcon('chevron-right')}`;
    segments.replaceWith(launch);
    const history = root.querySelector('.crm-history-launch');
    launch.after(history);
    history.innerHTML = `История рассылок ${actionIcon('chevron-right')}`;
    const selectPage = root.querySelector('.crm-column-head .crm-check');
    const selection = document.createElement('div'); selection.className='crm-page-selection fo-menu-field';
    if(selectPage)selection.append(selectPage); selection.insertAdjacentHTML('beforeend','<span>Страница</span>');
    selection.querySelector('span').textContent='Выбрать всех на странице';
    if (segment) {
      const chip = document.createElement('button'); chip.className = 'crm-segment-chip fo-button';
      chip.dataset.crmMobileClearSegment = ''; chip.innerHTML = `${esc(segment)} ${actionIcon('close')}`;
      history.after(chip);
    }
    const toolbar=root.querySelector('.crm-toolbar');
    const actions=document.createElement('details');actions.className='crm-list-actions';actions.dataset.foDetails='';
    actions.innerHTML='<summary class="fo-button fo-icon-button" aria-label="Действия со списком">⋯<span class="fo-visually-hidden">Ещё</span></summary><div class="crm-list-menu fo-menu" data-fo-popup="popover"></div>';
    const menu=actions.querySelector('div');menu.append(root.querySelector('.crm-heading [data-crm-export]'),launch,history);if(crm.selecting)menu.append(selection);menu.querySelectorAll('button').forEach(n=>n.dataset.foMenuAction='');
    const limit=document.createElement('label');limit.className='crm-list-limit fo-menu-field';limit.innerHTML='<span>На странице</span>';limit.append(toolbar.querySelector('[data-admin-list-limit]'));toolbar.append(limit);
    toolbar.append(actions);

    root.querySelectorAll('.crm-row').forEach(row => { row.tabIndex = 0; row.setAttribute('aria-label', 'Открыть клиента ' + row.querySelector('strong').textContent); });
    const bulk=root.querySelector('.crm-bulk');
    if(bulk){
      bulk.querySelector('[data-crm-offer]')?.remove();
      const more=document.createElement('details');more.className='crm-selection-actions';more.dataset.foDetails='';
      more.innerHTML='<summary class="fo-button">Действия '+actionIcon('chevron-down')+'</summary><div class="crm-selection-menu fo-menu" data-fo-popup="menu"></div>';
      const menu=more.querySelector('div');
      bulk.querySelectorAll('[data-crm-message],[data-crm-export],[data-crm-select-all]').forEach(button=>menu.append(button));
      bulk.querySelector('strong').setAttribute('role','status');
      bulk.append(more);
    }
    return doc.innerHTML;
  };
  function closeSheet(commit=false) {
    if(sheet)FreemanUI.close(sheet,{force:commit});
  }
  function openSheet(title, body, footer = '') {
    closeSheet(true);
    const root=document.createElement('div');root.id='crm-mobile-sheet';
    root.className='fo-overlay fo-overlay--sheet fo-scope';root.dataset.foOverlay='sheet';
    if(title==='Фильтр')root.dataset.foDirty='';
    root.innerHTML=`<section role="dialog" aria-modal="true" aria-label="${title}" class="crm-sheet fo-dialog fo-scope" data-fo-panel><header class="fo-dialog-header"><h2>${title}</h2>${title==='Фильтр'?'<button class="fo-button" data-crm-sheet-reset>Сбросить</button>':''}<button class="fo-button fo-icon-button" data-crm-sheet-close aria-label="Закрыть">${actionIcon('close')}</button></header><div class="crm-sheet-body">${body}</div>${footer?`<footer class="fo-dialog-actions fo-dialog-actions--stretch">${footer}</footer>`:''}</section>`;
    document.body.append(root);sheet=root;
    FreemanUI.mount(root,{onClose:()=>{root.remove();if(sheet===root)sheet=null;}});
  }
  function filterSheet() {
    // Derive options from the existing desktop filter, never a second query schema.
    const temp = document.createElement('template'); temp.innerHTML = crmFilterPanel();
    const fields = [...temp.content.querySelectorAll('label')];
    const body = fields.map(label => {
      const select = label.querySelector('select'), key = select.dataset.crmFilter;
      return `<details class="crm-filter-group"><summary class="fo-button fo-button--row fo-button--plain">${esc(label.firstChild.textContent)}<span>${esc(select.selectedOptions[0].textContent)}</span>${actionIcon('chevron-down')}</summary>${[...select.options].map(opt=>`<label class="fo-check-label"><input class="fo-radio" type="radio" name="${key}" value="${esc(opt.value)}" ${opt.selected?'checked':''}>${esc(opt.textContent)}</label>`).join('')}</details>`;
    }).join('');
    openSheet('Фильтр', body, '<button class="crm-primary fo-button fo-button--primary" data-crm-sheet-apply>Применить фильтры</button>');
  }
  document.addEventListener('click', async e => {
    if (e.target.closest('[data-crm-retry]')) {await loadAdminList('admin-clients');return;}
    if (compact() && e.target.closest('[data-crm-filter-toggle]')) {
      e.preventDefault(); e.stopImmediatePropagation(); filterSheet(); return;
    }
    if (e.target.closest('[data-crm-mobile-segments]')) {
      const temp = document.createElement('template'); temp.innerHTML = page();
      openSheet('Сегменты', temp.content.querySelector('.crm-segments [data-fo-popup]').innerHTML); return;
    }
    if (e.target.closest('[data-crm-sheet-reset]')) {
      sheet.querySelectorAll('input[type=radio]').forEach(n=> n.checked = n.value === (n.name==='sort'?'newest':'all'));
      sheet.querySelectorAll('summary span').forEach(n=>n.textContent='Все'); return;
    }
    if (e.target.closest('[data-crm-sheet-apply]')) {
      const filters = {...crmFilters()};
      sheet.querySelectorAll('input:checked').forEach(n=>filters[n.name]=n.value);
      state.extra['admin-clientsFilter']=filters; crm.selected.clear(); crm.all=false;
      closeSheet(true); await loadAdminList('admin-clients'); return;
    }
    const choice = e.target.closest('[data-crm-segment]');
    if (choice && sheet) { segment = choice.textContent; closeSheet(true); } // existing handler applies the same rules
    if (e.target.closest('[data-crm-mobile-clear-segment]')) {
      segment=null; state.extra['admin-clientsFilter']=crmDefault(); crm.selected.clear(); crm.all=false;
      await loadAdminList('admin-clients'); return;
    }
    if (e.target.closest('[data-crm-mobile-more]')) {
      e.target.closest('.crm-bulk').classList.toggle('expanded'); return;
    }
    const row = e.target.closest('.crm-row');
    if (row && !e.target.closest('button,input,label,a')) await openAdminCustomer(Number(row.dataset.clientRow));
  }, true);
  document.addEventListener('change', e=> {
    if (!sheet || !e.target.matches('input[type=radio]')) return;
    e.target.closest('details').querySelector('summary span').textContent = e.target.parentElement.textContent;
  });
  document.addEventListener('keydown', e => {
    if ((e.key==='Enter'||e.key===' ') && e.target.matches('.crm-row')) {e.preventDefault();openAdminCustomer(Number(e.target.dataset.clientRow));}
  });
  const campaignStep=crmCampaignStep;
  crmCampaignStep=function(){campaignStep();const root=document.querySelector('#crm-campaign-root .crm-steps');if(root){root.dataset.progress=`${crmCampaign.step} из 4`;root.setAttribute('aria-label',`${crmCampaign.step} из 4`);}};
  const campaignMount=crmCampaignMount;
  let campaignOpener=null;
  crmCampaignMount=function(){
    const wasOpen=!!document.querySelector('.crm-drawer');
    if(crmCampaign&&!wasOpen)campaignOpener=document.activeElement;
    campaignMount();
    document.body.classList.toggle('crm-campaign-open',!!crmCampaign);
    if(crmCampaign)document.querySelector('.crm-drawer [data-crm-campaign-close]')?.focus();
    else if(wasOpen){campaignOpener?.focus({preventScroll:true});campaignOpener=null;}
  };
  document.addEventListener('keydown',e=>{
    if(!crmCampaign||e.key!=='Tab')return;
    const nodes=[...document.querySelectorAll('.crm-drawer button,.crm-drawer input,.crm-drawer select,.crm-drawer textarea,.crm-drawer summary')].filter(n=>!n.disabled&&n.getClientRects().length);
    const first=nodes[0],last=nodes.at(-1);
    if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}
    if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}
  });
  crmCampaignMount=FreemanUI.enhance(crmCampaignMount,()=>document.querySelector('#crm-campaign-root'));
  matchMedia('(max-width:1099px)').addEventListener('change',()=>{if(state.view==='admin-clients')render();});
})();

