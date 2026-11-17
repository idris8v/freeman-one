/* Decorative shared-coordinate artwork. CSS owns the idle loop; JS only tracks
   visibility and short, interruptible pointer reactions. No timers or API calls. */
window.TelegramProxyHero = (() => {
  'use strict';
  const assets = '/assets/telegram-proxy-hero/';
  const flagURL = assets + '03_flag_fabric.png?v=a82005a6ab77';
  const image = (name, version, cls = '') => `<img class="${cls}" src="${assets}${name}?v=${version}" width="2048" height="795" alt="" decoding="async" draggable="false">`;
  const cloud = '<svg viewBox="0 0 180 64" fill="currentColor" aria-hidden="true"><path d="M29 58C10 58 7 34 23 28c3-24 39-29 51-8 18-15 44-6 46 14 26-8 46 6 43 17-2 7-9 7-19 7Z"/></svg>';
  const plane = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M21.7 3.3c.5-.2.9.1.7.9l-3.5 16c-.3 1.1-.9 1.3-1.8.7l-5.4-4-2.6 2.5c-.3.3-.5.5-1 .5l.4-5.5L18.6 5.3c.4-.4-.1-.6-.6-.3L5.5 12.9.2 11.2c-1.1-.4-1.1-1.1.3-1.6Z"/></svg>';
  // Piecewise-linear fabric deformation: bands share one texture coordinate
  // system and matching edge displacements. The pole-side vertices stay fixed.
  function fabric() {
    return Array.from({length: 8}, (_, i) => {
      const left = 1107 + i * 366 / 8, width = 366 / 8 + 4, top = 40, height = 350;
      return `<span class="tg-proxy-hero-band tg-proxy-hero-band-${i}" style="left:${left / 2048 * 100}%;top:${top / 795 * 100}%;width:${width / 2048 * 100}%;height:${height / 795 * 100}%;background-size:${2048 / width * 100}% ${795 / height * 100}%;background-position:${left / (2048 - width) * 100}% ${top / (795 - height) * 100}%;background-image:url('${flagURL}')"></span>`;
    }).join('');
  }
  const blink = `<svg class="tg-proxy-hero-eyelids" viewBox="0 0 2048 795" aria-hidden="true"><defs><radialGradient id="tg-proxy-eyelid-shade"><stop offset=".72" stop-color="#061b27"/><stop offset="1" stop-color="#061b27" stop-opacity="0"/></radialGradient></defs><g transform="rotate(16.14 1627.25 253.44)"><ellipse cx="1627.25" cy="253.44" rx="38" ry="53" fill="url(#tg-proxy-eyelid-shade)"/><path d="M1612.25 253.44Q1627.25 258.44 1642.25 253.44" fill="none" stroke="#5df2ff" stroke-width="5.5" stroke-linecap="round"/></g><g transform="rotate(17.44 1740.61 281.70)"><ellipse cx="1740.61" cy="281.7" rx="38" ry="53" fill="url(#tg-proxy-eyelid-shade)"/><path d="M1725.61 281.7Q1740.61 286.7 1755.61 281.7" fill="none" stroke="#5df2ff" stroke-width="5.5" stroke-linecap="round"/></g></svg>`;
  let current = null;
  function render() {
    return `<div class="tg-proxy-promo"><section class="tg-proxy-hero" data-telegram-proxy-hero aria-labelledby="tg-proxy-hero-title">
      <div class="tg-proxy-hero-surface" aria-hidden="true"><div data-proxy-hero-background>${image('00_background.png', '076f4f29e3f1')}</div><div class="tg-proxy-hero-atmosphere"><span class="tg-proxy-hero-cloud tg-proxy-hero-cloud-a">${cloud}</span><span class="tg-proxy-hero-cloud tg-proxy-hero-cloud-b">${cloud}</span><span class="tg-proxy-hero-cloud tg-proxy-hero-cloud-c">${cloud}</span><span class="tg-proxy-hero-cloud tg-proxy-hero-cloud-d">${cloud}</span>${['a','b','c','d','e','f','g'].map(name=>`<span class="tg-proxy-hero-plane tg-proxy-hero-plane-${name}">${plane}</span>`).join('')}</div></div>
      <div class="tg-proxy-hero-canvas" aria-hidden="true">${image('01_planet.png', 'f75f3804d9f9', 'tg-proxy-hero-planet')}<div class="tg-proxy-hero-parallax" data-proxy-hero-parallax><div class="tg-proxy-hero-rig" data-proxy-hero-rig>
        <div class="tg-proxy-hero-robot">${image('02_robot_with_flagpole.png', '2c47b6f12069')}${blink}<div class="tg-proxy-hero-flag">${image('03_flag_fabric.png', 'a82005a6ab77', 'tg-proxy-hero-flag-still')}<div class="tg-proxy-hero-cloth" data-proxy-hero-cloth>${fabric()}</div></div></div>
      </div></div></div>
      <div class="tg-proxy-hero-copy"><h2 id="tg-proxy-hero-title">TELEGRAM ЗАХВАЧЕН!</h2><p>Подключайтесь к нашему прокси, и ваш Telegram будет работать без VPN.</p></div>
    </section></div>`;
  }
  function mount(root) {
    const parallax = root.querySelector('[data-proxy-hero-parallax]');
    const background = root.querySelector('[data-proxy-hero-background]');
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const fine = matchMedia('(hover: hover) and (pointer: fine)');
    const controller = new AbortController(), options = {passive: true, signal: controller.signal};
    const springs = Array.from({length: 3}, () => ({value: 0, velocity: 0, target: 0}));
    const [x, y, reaction] = springs;
    let frame = 0, last = 0, visible = false, disposed = false, rect = null, hovered = false;
    function reset() {
      cancelAnimationFrame(frame); frame = last = 0;
      springs.forEach(s => {s.value = s.velocity = s.target = 0;});
      parallax.style.removeProperty('transform'); background.style.removeProperty('transform');
    }
    function enabled() {return !disposed && visible && !document.hidden && !reduced.matches;}
    function update() {
      root.toggleAttribute('data-moving', enabled());
      if (!enabled()) {hovered = false; reset();}
    }
    function step(s, dt) {
      s.velocity += (110 * (s.target - s.value) - 21 * s.velocity) * dt;
      s.value += s.velocity * dt;
    }
    function tick(time) {
      frame = 0;
      if (!enabled()) {update(); return;}
      const dt = Math.min((time - (last || time - 16)) / 1000, .032); last = time;
      springs.forEach(s => step(s, dt));
      parallax.style.transform = `translate3d(${(x.value * .6).toFixed(3)}px,${(y.value * .6 - reaction.value * .6).toFixed(3)}px,0)`;
      background.style.transform = `translate3d(${(x.value * 1.4).toFixed(3)}px,${(y.value * .8).toFixed(3)}px,0) scale(1.012)`;
      if (springs.some(s => Math.abs(s.target - s.value) + Math.abs(s.velocity) > .002)) frame = requestAnimationFrame(tick);
      else last = 0;
    }
    function run() {if (enabled() && !frame) frame = requestAnimationFrame(tick);}
    root.addEventListener('pointerenter', e => {
      if (e.pointerType !== 'mouse' || !fine.matches || !enabled()) return;
      hovered = true; rect = root.getBoundingClientRect(); reaction.target = 1; run();
    }, options);
    root.addEventListener('pointermove', e => {
      if (e.pointerType !== 'mouse' || !fine.matches || !hovered || !rect) return;
      x.target = Math.max(-1, Math.min(1, (e.clientX - rect.left) / rect.width * 2 - 1));
      y.target = Math.max(-1, Math.min(1, (e.clientY - rect.top) / rect.height * 2 - 1)); run();
    }, options);
    root.addEventListener('pointerleave', () => {hovered = false; springs.forEach(s => s.target = 0); run();}, options);
    function reconfigure() {hovered = false; reset(); update();}
    reduced.addEventListener('change', reconfigure); fine.addEventListener('change', reconfigure);
    document.addEventListener('visibilitychange', update, {signal: controller.signal});
    const resize = new ResizeObserver(() => {if (hovered) rect = root.getBoundingClientRect();}); resize.observe(root);
    const observer = new IntersectionObserver(entries => {visible = entries[0].isIntersecting; update();}, {threshold: .15});
    Promise.all([...root.querySelectorAll('img')].map(img => img.decode())).then(() => {
      if (!disposed) {root.setAttribute('data-ready', ''); observer.observe(root);}
    }).catch(() => {root.setAttribute('data-image-error', '');});
    return {root, destroy() {disposed = true;root.removeAttribute('data-moving');reset();controller.abort();observer.disconnect();resize.disconnect();reduced.removeEventListener('change', reconfigure);fine.removeEventListener('change', reconfigure);}};
  }
  function sync(container) {
    const root = container?.querySelector('[data-telegram-proxy-hero]') || null;
    if (root === current?.root) return;
    current?.destroy(); current = root ? mount(root) : null;
  }
  return {render, sync};
})();
