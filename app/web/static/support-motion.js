/* Scoped support motion: keep the live conversation and keyboard focus intact. */
window.SupportMotion = (() => {
  const reduced = () => window.PageMotion?.reduced.matches ?? matchMedia('(prefers-reduced-motion: reduce)').matches;
  function animate(node, frames, duration = window.PageMotion?.tokens.message || 220) {
    return window.PageMotion?.animate(node,frames,duration,'support');
  }
  // Conversation drafts stay in this document, never in serialized navigation
  // history/session storage. Keyed patching continues to own the live composer.
  const drafts = new Map();
  function rememberDraft(root) {
    const key=root?.__supportContext,input=root?.querySelector('.fo-chat-composer textarea'),history=root?.querySelector('.native-history');
    if(!key||!input&&!history)return;
    drafts.set(key,{text:input?.value||'',start:input?.selectionStart||0,end:input?.selectionEnd||0,inputTop:input?.scrollTop||0,request:input?.form?.dataset.requestId,top:history?.scrollTop||0,bottom:!history||history.scrollHeight-history.scrollTop-history.clientHeight<80});
    if(drafts.size>20)drafts.delete(drafts.keys().next().value);
  }
  function restoreDraft(root,key){
    const draft=drafts.get(key),input=root.querySelector('.fo-chat-composer textarea');
    if(draft&&input){input.value=draft.text;FreemanComponents.sizeComposer(input);input.setSelectionRange(draft.start,draft.end);input.scrollTop=draft.inputTop;if(draft.request)input.form.dataset.requestId=draft.request;}
    return draft;
  }
  function capture(root, active, key) {
    if(active)rememberDraft(root);
    const history = root?.querySelector('.native-history');
    return {
      active, key, previous: root?.__supportContext,
      top: history?.scrollTop || 0,
      nearBottom: !history || history.scrollHeight - history.clientHeight - history.scrollTop < 80,
      ids: new Set([...root?.querySelectorAll('[data-message-id]') || []].map(node => node.dataset.messageId)),
      rows: new Map([...root?.querySelectorAll('.fo-chat-list [data-support-ticket]') || []].map(node => [node.dataset.supportTicket, node.getBoundingClientRect().top]))
    };
  }
  function nodeKey(node) {
    if (node.dataset.messageId) return 'message:' + node.dataset.messageId;
    if (node.classList.contains('native-date')) return 'date:' + node.textContent;
    if (node.matches('[data-support-older]')) return 'older';
    return null;
  }
  function patch(root, markup, before, continuation) {
    if(before.active&&root.querySelector('[data-fo-chat-workspace]')&&markup.includes('data-fo-chat-workspace')){const old=root.querySelector('.fo-support-dialogs');if(old){const next=document.createElement('div');next.innerHTML=markup;const livePane=root.querySelector('.fo-support-preview'),nextPane=next.querySelector('.fo-support-preview');if(livePane&&nextPane){livePane.inert=nextPane.inert;if(nextPane.hasAttribute('aria-busy'))livePane.setAttribute('aria-busy','true');else livePane.removeAttribute('aria-busy')}const proposed=next.querySelector('.fo-support-dialogs');if(proposed&&old.innerHTML!==proposed.innerHTML){const field=old.querySelector('[data-support-search]'),focused=field===document.activeElement,caret=field?.selectionStart,top=old.querySelector('[data-support-list-scroll]')?.scrollTop||0;old.replaceWith(proposed);const list=proposed.querySelector('[data-support-list-scroll]');if(list)list.scrollTop=top;if(focused){const input=proposed.querySelector('[data-support-search]');input?.focus({preventScroll:true});input?.setSelectionRange(caret,caret)}}}return true;}
    if (!before.active || !root.querySelector('.native-chat')) return false;
    const next = document.createElement('div'); next.innerHTML = markup;
    next.querySelectorAll('button:not(.fo-button,.fo-tab),a:not(.fo-button,.fo-tab)').forEach(node => node.classList.add('pressable'));
    if (!next.querySelector('.native-chat')) return false;
    const changed=before.previous!==before.key&&!continuation;
    const liveChat=root.querySelector('.native-chat');liveChat.inert=false;liveChat.removeAttribute('aria-busy');
    const oldHistory = root.querySelector('.native-history'), nextHistory = next.querySelector('.native-history');
    if (!oldHistory || !nextHistory) return false;
    const oldHeader = root.querySelector('.native-header'), nextHeader = next.querySelector('.native-header');
    if (changed && oldHeader?.outerHTML !== nextHeader?.outerHTML) { oldHeader?.replaceWith(nextHeader); animate(nextHeader, [{opacity:.65},{opacity:1}], window.PageMotion.tokens.press); }
    const existing = new Map([...oldHistory.children].map(node => [nodeKey(node), node]).filter(([key]) => key));
    let cursor = oldHistory.firstElementChild;
    const keep = new Set();
    for (const proposed of [...nextHistory.children]) {
      let node = existing.get(nodeKey(proposed));
      if (!node || changed) node = proposed;
      else if (node.dataset.messageId) {
        // Delivery/read updates must not reload media or collapse an expanded spoiler.
        const bubble=node.querySelector('.native-bubble'),nextBubble=proposed.querySelector('.native-bubble');
        if(!bubble||!nextBubble)node=proposed;
        else {
          node.className=proposed.className;
          for(const key of ['supportTicket','supportReadId'])node.dataset[key]=proposed.dataset[key];
          for(const selector of ['.fo-chat-media-list','.fo-message-text','.cm-message-actions','time']){
            const live=bubble.querySelector(':scope>'+selector),next=nextBubble.querySelector(':scope>'+selector);
            if(!live||!next){if(live)live.remove();else if(next)bubble.append(next);continue;}
            if(selector==='.fo-message-text'&&live.dataset.cmSource===next.dataset.cmSource){
              for(const key of ['cmEditId','cmEditVersion','cmEditUntil']){
                if(next.dataset[key]===undefined)delete live.dataset[key];else live.dataset[key]=next.dataset[key];
              }
            }else if(!live.isEqualNode(next))live.replaceWith(next);
          }
        }
      }else if(node.outerHTML!==proposed.outerHTML)node=proposed;
      keep.add(node);
      if (node !== cursor) oldHistory.insertBefore(node, cursor);
      cursor = node.nextElementSibling;
    }
    [...oldHistory.children].forEach(node => { if (!keep.has(node)) node.remove(); });
    const composer = root.querySelector('.native-composer'), nextComposer = next.querySelector('.native-composer');
    const closed = root.querySelector('.native-closed'), nextClosed = next.querySelector('.native-closed');
    if(changed)oldHistory.__stopSupportScroll?.();
    if (changed&&composer) {
      composer.__dictation?.destroy();
      composer.querySelector('textarea').value='';delete composer.dataset.requestId;
      composer.querySelectorAll('input[type=file]').forEach(input=>input.value='');
      const count=composer.querySelector('[data-file-count]');if(count)count.textContent='';
      const error=composer.querySelector('.chat-error');if(error){error.hidden=true;error.textContent='';}
      PageMotion.content(oldHistory,before.key);
    }
    if (composer && nextComposer) {
      const send = composer.querySelector('[type=submit]'), nextSend = nextComposer.querySelector('[type=submit]');
      if (send && nextSend) send.disabled = nextSend.disabled;
    } else if (composer) { composer.replaceWith(nextClosed); animate(nextClosed, [{opacity:0},{opacity:1}], window.PageMotion.tokens.panel); }
    else if (closed) { const replacement=nextComposer || nextClosed; closed.replaceWith(replacement); animate(replacement, [{opacity:0},{opacity:1}], window.PageMotion.tokens.panel); }
    return true;
  }
  function bottom(history) {
    if (!history) return;
    history.__stopSupportScroll?.();
    const start = history.scrollTop, end = history.scrollHeight - history.clientHeight;
    if (reduced() || Math.abs(end - start) < 1) { history.scrollTop = end; return; }
    let frame, stopped = false; const at = performance.now();
    const events = ['wheel', 'touchstart', 'pointerdown', 'keydown'];
    const stop = () => { stopped = true; cancelAnimationFrame(frame); events.forEach(type => history.removeEventListener(type, stop)); };
    history.__stopSupportScroll = stop;
    events.forEach(type => history.addEventListener(type, stop, { passive: true, once: true }));
    const step = now => {
      if (stopped || document.hidden || !history.isConnected) return stop();
      const t = Math.min(1, (now - at) / window.PageMotion.tokens.message);
      history.scrollTop = start + (end - start) * (1 - Math.pow(1 - t, 3));
      if (t < 1) frame = requestAnimationFrame(step); else stop();
    };
    frame = requestAnimationFrame(step);
  }
  function after(root, before, ready, continuation, sent) {
    if (!ready) { root.querySelector('.native-history')?.__foTail?.destroy();root.__supportContext = null; return; }
    const changed = !before.active || (before.previous !== before.key && !continuation);
    root.__supportContext = before.key;
    const history = root.querySelector('.native-history');
    const tail=FreemanComponents.historyTail(history,root.querySelector('.native-composer,.native-closed'));
    if (changed) {
      const saved=restoreDraft(root,before.key);
      if(history)history.scrollTop=!sent&&saved&&!saved.bottom?saved.top:history.scrollHeight;
      tail?.sync(!!sent);return;
    }
    if (history) {
      const maximum = Math.max(0, ...[...before.ids].map(Number));
      const added = [...history.querySelectorAll('[data-message-id]')].filter(node => !before.ids.has(node.dataset.messageId) && Number(node.dataset.messageId) > maximum);
      history.scrollTop = before.top;
      added.forEach(node => window.FreemanComponents.revealMessage(node));
      if(sent)tail?.sync(true);else if(added.length&&before.nearBottom)bottom(history);else tail?.sync();
    } else {
      root.querySelectorAll('.fo-chat-list [data-support-ticket]').forEach(node => {
        const prior = before.rows.get(node.dataset.supportTicket);
        const offset = prior === undefined ? 6 : prior - node.getBoundingClientRect().top;
        if (Math.abs(offset) > 1) animate(node, [{ opacity: prior === undefined ? 0 : 1, translate: `0 ${Math.max(-90, Math.min(90, offset))}px` }, { opacity: 1, translate: '0 0' }], window.PageMotion.tokens.panel);
      });
    }
  }
  return { capture, patch, after, bottom, rememberDraft };
})();
