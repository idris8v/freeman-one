'use strict';

const desktopRelease = 'https://github.com/Happ-proxy/happ-desktop/releases/latest';
const happApk = 'https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk';
const incyPlay = 'https://play.google.com/store/apps/details?id=llc.itdev.incy';
const apps = {
  apple: [
    {name:'Rabbit Hole',url:'https://apps.apple.com/ru/app/rabbithole-vpn-client/id6683309629'},
    {name:'Happ', url:'https://apps.apple.com/kz/app/happ-proxy-utility/id6504287215', extraUrl: desktopRelease},
    {name:'INCY', url:'https://apps.apple.com/kz/app/incy/id6756943388'},
  ],
  android: [
    {name:'INCY',url:incyPlay},
    {name:'Happ',info:'Скачайте APK Happ и откройте загруженный файл.',url:happApk,extraUrl:'https://github.com/Happ-proxy/happ-android/releases/latest'},
    {name:'Rabbit Hole',url:'https://play.google.com/store/apps/details?id=com.rabbithole.RabbitHole&hl=ru&gl=RU'},
  ],
  windows: [
    {name:'Happ',info:'В разделе Assets скачайте и запустите установщик .exe.',url:desktopRelease},
    {name:'INCY',info:'Выберите Windows и запустите установщик .exe. Это предварительная версия INCY.',url:'https://github.com/INCY-DEV/incy-platforms#desktop'},
  ],
  tv: [
    {name:'INCY',info:'На телевизоре откройте Google Play, найдите INCY и установите его. Для переноса подписки установите INCY также на телефон.',url:incyPlay},
    {name:'Happ',info:'Скачайте APK, перенесите его на телевизор через USB или приложение для передачи файлов и откройте в файловом менеджере телевизора.',url:happApk,extraUrl:'https://github.com/Happ-proxy/happ-android/releases/latest'},
  ],
};
const presentation = {
  INCY: {icon:'incy', theme:'incy', menu:'Добавить подписку', from:'Из буфера обмена', connect:'В INCY откройте «Подключение». Выберите сервер в списке и нажмите круглую кнопку питания над ним.'},
  Happ: {icon:'happ', theme:'happ', menu:'Добавить', from:'Из буфера обмена', connect:'В Happ выберите сервер в списке подписки и нажмите круглую кнопку подключения на главном экране.'},
  'Rabbit Hole': {icon:'rabbit', theme:'rabbit', menu:'Добавить профиль', from:'Импорт из буфера обмена', connect:'В Rabbit Hole откройте импортированный профиль, выберите сервер и нажмите «Подключиться» внизу экрана.'},
};
const params = new URLSearchParams(location.search);
const macHapp = params.get('os') === 'macos';
const get = id => document.getElementById(id);
const platformButtons = [...document.querySelectorAll('[data-os]')];
const choices = get('app-choices');
const importFigure = get('import-figure');
const connectFigure = get('connect-diagram').parentElement;
const selection = new Map();
const pointerObserver = new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (entry.intersectionRatio < .6) continue;
    entry.target.classList.add('guide-pointer-visible');
    pointerObserver.unobserve(entry.target);
  }
}, {threshold: .6});
function writeSteps(id, steps, theme) {
  get(id).replaceChildren(...steps.map(value => {
    const [instruction, control] = Array.isArray(value) ? value : [value];
    const item = document.createElement('li');
    item.textContent = instruction;
    if (control) {
      const example = document.createElement('span');
      example.className = 'guide-control ' + theme + (control === '⏻' ? ' power-symbol' : '');
      example.textContent = control;
      example.setAttribute('aria-hidden', 'true');
      item.append(example);
    }
    return item;
  }));
}
function renderStores(id, app, options) {
  const appName = app === 'Rabbit Hole' ? 'RabbitHole' : app;
  const stores = options.map(({url, label, context, tv}) => {
    const store = url.startsWith('https://apps.apple.com/') ? 'apple'
      : url.startsWith('https://play.google.com/') ? 'play' : url.endsWith('.apk') ? 'apk' : 'files';
    const names = {apple: 'App Store', play: 'Google Play', apk: 'Файл APK', files: 'GitHub'};
    const row = document.createElement('div');
    row.className = 'guide-store guide-store-' + store;
    const icon = document.createElement('img');
    icon.className = 'guide-store-icon';
    icon.src = '/assets/guide-' + presentation[app].icon + '-icon.jpg';
    icon.width = 30;
    icon.height = 30;
    icon.alt = '';
    const meta = document.createElement('div');
    meta.className = 'guide-store-source';
    const name = document.createElement('strong');
    name.textContent = appName;
    const brand = document.createElement('span');
    brand.className = 'guide-store-brand';
    brand.textContent = context === 'Для Mac' ? names[store] + ' · Mac' : names[store];
    meta.append(name, brand);
    const action = document.createElement(tv ? 'span' : 'a');
    action.className = 'guide-store-action';
    action.textContent = label || (store === 'apple' ? 'Загрузить' : store === 'play' ? 'Установить' : 'Скачать');
    if (!tv) {
      action.href = url;
      action.target = '_blank';
      action.rel = 'noopener noreferrer';
      action.setAttribute('aria-label', [action.textContent + ' ' + appName, names[store], context].filter(Boolean).join(' · '));
    }
    row.append(icon, meta, action);
    return row;
  });
  get(id).replaceChildren(...stores);
}

function rabbitFragment(stage) {
  function add(parent, className, text = '') {
    const node = document.createElement('div');
    node.className = className;
    node.textContent = text;
    parent.append(node);
    return node;
  }

  const figure = document.createElement('figure');
  figure.className = 'guide-diagram rabbit-guide-figure';
  const fragment = add(figure, 'rabbit-guide-fragment rabbit-guide-' + stage);
  fragment.setAttribute('aria-hidden', 'true');
  if (stage === 'add') {
    add(fragment, 'rabbit-guide-circle rabbit-guide-filter');
    add(fragment, 'rabbit-guide-pill', 'Профиль VPN не установлен');
    add(fragment, 'rabbit-guide-circle rabbit-guide-plus mock-target');
  } else if (stage === 'clipboard') {
    add(fragment, 'rabbit-guide-grabber');
    const heading = add(fragment, 'rabbit-guide-sheet-header');
    add(heading, 'rabbit-guide-cancel', 'Отмена');
    add(heading, 'rabbit-guide-sheet-title', 'Добавить конфиг');
    const options = add(fragment, 'rabbit-guide-options');
    for (const [label, icon] of [
      ['QR-код', 'qr'], ['Буфер обмена', 'copy'], ['Добавить по URL', 'link'], ['Загрузить YAML', 'file'],
    ]) {
      const option = add(options, 'rabbit-guide-option' + (icon === 'copy' ? ' mock-target' : ''));
      add(option, 'rabbit-guide-option-icon rabbit-guide-' + icon, icon === 'file' ? '↓' : '');
      add(option, '', label);
    }
    add(fragment, 'rabbit-guide-sheet-help', 'ⓘ Не знаете, что такое конфигурации?');
  } else {
    const servers = add(fragment, 'rabbit-guide-servers');
    add(servers, 'rabbit-guide-subtitle', 'FreemanOne');
    const row = add(servers, 'rabbit-guide-server');
    add(row, '', '⚡ Авто');
    add(row, 'rabbit-guide-tag', 'VLESS');
    add(row, 'rabbit-guide-tag', '—');

    const links = add(fragment, 'rabbit-guide-links');
    add(links, '', '↗ Ссылка');
    add(links, '', '✉ Поддержка');
    add(fragment, 'rabbit-guide-primary mock-target', 'Подключиться');

    const tabs = add(fragment, 'rabbit-guide-tabs');
    for (const [label, icon] of [['Конфиги', 'configs'], ['Настройки', 'settings']]) {
      const tab = add(tabs, 'rabbit-guide-tab');
      add(tab, 'rabbit-guide-tab-icon rabbit-guide-' + icon, icon === 'settings' ? '⚙' : '');
      add(tab, '', label);
    }
  }
  return figure;
}

const happIconPaths = {
  gear: 'M9.7 3.3h4.6l.7 2.3 1.6 1 2.4-.5 2.3 4-1.7 1.7v1.8l1.7 1.8-2.3 4-2.4-.6-1.6 1-.7 2.2H9.7L9 19.8l-1.6-1-2.4.6-2.3-4 1.7-1.8v-1.8l-1.7-1.7 2.3-4 2.4.5 1.6-1z M15.5 12.5a3.5 3.5 0 1 1-7 0 3.5 3.5 0 0 1 7 0z',
  plus: 'M12 4v16M4 12h16',
  qr: 'M8 3H5a2 2 0 0 0-2 2v3m13-5h3a2 2 0 0 1 2 2v3m0 8v3a2 2 0 0 1-2 2h-3m-8 0H5a2 2 0 0 1-2-2v-3 M7 7h3v3H7zm7 0h3v3h-3zm-7 7h3v3H7zm7 0h1m2 0v3h-3',
  link: 'm10 8 3.1-3.1a4 4 0 0 1 5.7 5.7l-2.1 2.1m-2.7-.7-4.5 4.5a4 4 0 0 1-5.7-5.7L7 8m10 10h5m-2.5-2.5v5',
  paste: 'M7 13V5a2 2 0 0 1 2-2h6l5 5v11a2 2 0 0 1-2 2h-5 M15 3v5h5 M11 17a4 4 0 1 1-8 0 4 4 0 0 1 8 0z m-4-1v3m-1.5-1.5L7 16l1.5 1.5',
  edit: 'M15.5 5.5h-1a2.5 2.5 0 0 0-5 0H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5m-10-5h7m-7 4h4m-4 4h2 M13 16l1-3 5-5a1.4 1.4 0 0 1 2 2l-5 5z',
  power: 'M12 2v10 M6.1 5.5a9 9 0 1 0 11.8 0',
  refresh: 'M4 10a8 8 0 0 1 13.7-4.8L20 8m0-5v5h-5m5 6a8 8 0 0 1-13.7 4.8L4 16m0 5v-5h5',
  gauge: 'M4.3 18a9 9 0 1 1 15.4 0M12 13l4-4',
  more: 'M4 12h.1m7.9 0h.1m7.9 0h.1',
};

function addHappIcon(parent, name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.classList.add('happ-guide-icon');
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', happIconPaths[name]);
  svg.append(path);
  parent.append(svg);
}

function happFragment(stage) {
  function add(parent, className, text = '') {
    const node = document.createElement('div');
    node.className = className;
    node.textContent = text;
    parent.append(node);
    return node;
  }

  const fragment = document.createElement('div');
  fragment.className = 'happ-guide-fragment happ-guide-' + stage;
  fragment.setAttribute('aria-hidden', 'true');
  if (stage === 'menu') {
    const toolbar = add(fragment, 'happ-guide-toolbar');
    addHappIcon(toolbar, 'gear');
    addHappIcon(add(toolbar, 'happ-guide-plus mock-target'), 'plus');
    const menu = add(fragment, 'happ-guide-popover');
    for (const [icon, label] of [
      ['link', 'URL подписки'], ['paste', 'Вставить из буфера обмена'],
      ['qr', 'QR-код'], ['edit', 'Ручной ввод'],
    ]) {
      const row = add(menu, 'happ-guide-menu-row' + (icon === 'paste' ? ' mock-target' : ''));
      addHappIcon(row, icon);
      add(row, '', label);
    }
  } else {
    const power = add(fragment, 'happ-guide-power mock-target');
    addHappIcon(power, 'power');
    const list = add(fragment, 'happ-guide-subscription');
    const header = add(list, 'happ-guide-subscription-header diagram-subscription');
    add(header, 'diagram-chevron');
    add(header, 'happ-guide-subscription-name', 'FreemanOne');
    const tools = add(header, 'happ-guide-tools');
    for (const icon of ['refresh', 'gauge', 'more']) addHappIcon(tools, icon);
    const server = add(list, 'happ-guide-server');
    add(server, '', '⚡ Авто');
    add(server, 'happ-guide-server-details', 'VLESS | TCP | Reality | JSON');
  }
  return fragment;
}

function drawDiagram(host, os, app, theme, mode) {
  const tv = os === 'tv';
  const desktop = os === 'windows' || (os === 'apple' && macHapp && app === 'Happ');
  const wide = tv || desktop;
  const happDesktop = desktop && app === 'Happ';
  const window = document.createElement('div');
  window.className = 'diagram diagram-' + theme + (wide ? ' diagram-wide' : '') +
    (tv ? ' diagram-tv-screen' : desktop ? ' diagram-pc-screen' : '');
  function add(parent, text, css = '') {
    const node = document.createElement('div');
    node.className = css;
    node.textContent = text;
    parent.append(node);
    return node;
  }
  function subscriptionHeading(parent, cls) {
    const heading = add(parent, '', cls + ' diagram-subscription');
    add(heading, '', 'diagram-chevron');
    add(heading, 'Freeman ONE');
  }
  const bar = add(window, '', 'diagram-bar');
  const mobileMock = !wide && (mode === 'import' || mode === 'connect');
  if (mobileMock) window.classList.add('diagram-app-screen');
  add(bar, app + (tv ? ' · Android TV' : ''));
  if (mobileMock) {
    const power = add(window, '⏻', 'diagram-power mock-app-power');
    if (mode === 'connect') power.classList.add('mock-target');
    add(window, 'Нажмите для подключения', 'mock-caption');
    const group = add(window, '', 'mock-app-group');
    subscriptionHeading(group, 'mock-app-group-header');
    add(group, '●  Авто', 'mock-app-server selected' + (mode === 'connect' ? ' diagram-point' : ''));
    add(group, '○  Другой сервер', 'mock-app-server');
    const tools = add(window, '', 'mock-app-tools');
    add(tools, '+ Добавить');
    add(tools, 'Вставить', mode === 'import' ? 'mock-target' : '');
    add(tools, '⌗');
    add(window, 'Подключение · Серверы · Настройки', 'mock-app-tabs');
    host.replaceChildren(window);
    return;
  }
  if (desktop) add(bar, '−  □  ×', 'diagram-window-controls');
  if (mode === 'tv-add') {
    const body = add(window, '', 'diagram-panel');
    add(bar, '+ Добавить', 'diagram-tap diagram-point diagram-tv-focus');
    add(body, 'Подписки', 'diagram-title');
    add(body, 'Здесь появится Freeman ONE', 'diagram-label');
    add(window, 'Выберите «+ Добавить» пультом и нажмите ОК', 'diagram-label');
  } else if (mode === 'phone-share') {
    const body = add(window, '', 'diagram-panel');
    add(body, 'Freeman ONE', 'diagram-title');
    if (app === 'INCY') add(body, '⋮  Меню подписки', 'diagram-field diagram-point');
    add(body, app === 'INCY' ? 'Отправить на ТВ' : 'Сканер QR-кода', 'diagram-tap diagram-point');
  } else if (mode === 'tv-import') {
    const body = add(window, '', 'diagram-tv-body');
    const panel = add(body, '', 'diagram-tv-transfer');
    const qr = add(panel, '', 'diagram-qr diagram-point');
    for (let i = 0; i < 3; i++) add(qr, '', 'diagram-qr-corner');
    const instructions = add(panel, '', 'diagram-tv-transfer-copy');
    add(instructions, 'QR-код на телевизоре', 'diagram-title');
    add(instructions, 'Откройте сканер в ' + app + ' на телефоне', 'diagram-label');
    if (app === 'INCY') add(instructions, 'Рядом с QR-кодом будет 8-значный код', 'diagram-label');
    else add(instructions, 'Импорт через сайт', 'diagram-tap diagram-point');
  } else if (mode === 'import') {
    const body = add(window, '', 'diagram-panel');
    if (happDesktop) {
      window.classList.add('diagram-happ-desktop');
      body.classList.add('diagram-workspace');
      const dialog = add(body, '', 'diagram-desktop-dialog');
      add(dialog, os === 'apple' ? '+ Добавить подписку' : 'Вставить', 'diagram-title');
      add(dialog, 'Ссылка подписки', 'diagram-field diagram-point');
      add(dialog, os === 'apple' ? 'Импорт из буфера обмена' : 'Поехали', 'diagram-tap diagram-point');
    } else if (desktop) {
      body.classList.add('diagram-desktop-layout');
      const sidebar = add(body, '', 'diagram-sidebar');
      add(sidebar, 'Подключение');
      add(sidebar, 'Подписки', 'diagram-title');
      add(sidebar, 'Настройки');
      const dialog = add(body, '', 'diagram-desktop-dialog');
      add(dialog, 'Добавить подписку', 'diagram-title');
      add(dialog, 'Вставить ссылку', 'diagram-field diagram-point');
    }
  } else {
    const content = add(window, '', 'diagram-wide-connect');
    const servers = add(content, '', 'diagram-servers');
    subscriptionHeading(servers, 'diagram-title');
    add(servers, '●  Авто', 'diagram-server diagram-point diagram-selected');
    add(servers, '○  Другой сервер', 'diagram-server');
    const controls = add(content, '', 'diagram-connect-controls');
    add(controls, '⏻', 'diagram-power diagram-point' + (tv ? ' diagram-tv-focus' : ''));
    add(controls, tv ? 'Нажмите ОК на пульте' : 'Подключиться', 'diagram-label');
  }
  host.replaceChildren(window);
}
function renderApp(os, index) {
  pointerObserver.disconnect();
  const app = apps[os][index];
  selection.set(os, index);
  for (const button of choices.children) button.setAttribute('aria-pressed', String(Number(button.dataset.index) === index));
  get('standard-import').hidden = os === 'tv';
  get('tv-import').hidden = os !== 'tv' || app.name !== 'Happ';
  get('incy-tv-import').hidden = os !== 'tv' || app.name !== 'INCY';
  const tv = os === 'tv';
  const details = presentation[app.name];
  get('download').textContent = tv ? 'Установите на телефон' : 'Скачайте приложение';
  const mac = os === 'apple' && macHapp && app.name === 'Happ';
  const installInfo = mac ? 'Откройте файл .dmg и перетащите Happ в «Программы».' : app.info;
  get('download-info').hidden = tv || !installInfo;
  get('download-info').textContent = installInfo || '';
  get('tv-start').hidden = !tv;
  get('tv-phone-import').hidden = !tv;
  get('tv-install').hidden = !tv;
  get('platform-downloads').hidden = tv;
  get('import-number').textContent = tv ? '4' : '3';
  get('connect-number').textContent = tv ? '5' : '4';
  get('import').textContent = tv ? 'Передайте подписку на ТВ' : 'Добавьте подписку';
  const rabbit = app.name === 'Rabbit Hole';
  get('permission').hidden = tv || os === 'windows' || rabbit;
  if (tv) {
    const incy = app.name === 'INCY';
    renderStores('tv-phone-stores', app.name, [
      {url: apps.apple.find(item => item.name === app.name).url, context: 'Для iPhone'},
      {url: incy ? incyPlay : happApk, context: 'Для Android'},
    ]);
    get('tv-install-info').textContent = incy
      ? 'В Google Play на ТВ найдите INCY → «Установить».'
      : 'Перенесите APK на ТВ через USB и откройте в файловом менеджере.';
    renderStores('tv-install-cards', app.name, [
      {url: app.url, tv: incy, label: incy ? 'Откройте на ТВ' : 'Скачать APK'},
    ]);
    drawDiagram(get('incy-tv-add-diagram'), os, 'INCY', 'incy', 'tv-add');
    drawDiagram(get(incy ? 'incy-phone-share-diagram' : 'happ-phone-share-diagram'),
      'android', app.name, details.theme, 'phone-share');
    writeSteps('tv-phone-steps', [
      incy ? 'В INCY нажмите «Вставить» внизу.' : 'В Happ нажмите «+» справа вверху.',
      incy ? 'Нет кнопки? «Добавить» → «Из буфера обмена». Ищите подписку во вкладке «Серверы».'
        : 'В меню нажмите «Вставить из буфера обмена».',
    ], details.theme);
    const phoneSteps = get('tv-phone-steps').children;
    if (incy) {
      const diagram = document.createElement('div');
      phoneSteps[0].append(diagram);
      drawDiagram(diagram, 'android', app.name, details.theme, 'import');
    } else {
      phoneSteps[1].append(happFragment('menu'));
    }
  }
  if (!tv) {
    const url = mac ? desktopRelease : app.url;
    const primaryLabel = mac || os === 'windows' ? 'Открыть' :
      url.endsWith('.apk') ? 'Скачать APK' : undefined;
    const stores = [{url, label: primaryLabel, context: mac ? 'Для Mac' : ''}];
    if (app.extraUrl && !mac) stores.push({url: app.extraUrl,
      label: 'Открыть', context: os === 'apple' ? 'Для Mac' : 'Другие версии APK',
    });
    renderStores('download-cards', app.name, stores);
  }
  get('region').hidden = os !== 'apple' || mac;
  const happDesktop = os === 'windows' && app.name === 'Happ';
  const desktop = os === 'windows' || mac;
  const incyMobile = app.name === 'INCY' && !desktop;
  const happMobile = app.name === 'Happ' && !desktop && !tv;
  const firstImport = desktop
    ? 'Откройте ' + app.name + '. Найдите «+» в окне программы или команду добавления в разделе подписок.'
    : 'Откройте ' + app.name + '. Нажмите «+» в правом верхнем углу.';
  writeSteps('import-info', rabbit ? [
    'Скопировали ссылку? В «Конфигах» нажмите «+» справа вверху.',
    '«Добавить конфиг» → «Буфер обмена». Разрешите вставку, если появится запрос.',
  ] : happMobile ? [
    'Скопировали ссылку? Откройте Happ и нажмите «+» справа вверху.',
    'В меню выберите «Вставить из буфера обмена».',
  ] : happDesktop ? [
    ['В окне «Вставить» нажмите Ctrl + V.', 'Вставить · ссылка подписки'],
    ['Нажмите «Поехали». Нет окна? Нажмите «+» и вставьте ссылку.', 'Поехали'],
    'Подождите, пока появятся серверы Freeman ONE.',
  ] : mac ? [
    ['Откройте Happ на Mac и нажмите «+».', '+'],
    ['Импортируйте из буфера обмена. Если открылось поле, нажмите ⌘ + V и подтвердите.', '⌘ + V'],
    'Найдите Freeman ONE в списке.',
  ] : [
    incyMobile ? 'Откройте экран «Подключение» в INCY.' : [firstImport, '+'],
    incyMobile ? ['Нажмите «Вставить» внизу или «Добавить» → «Из буфера обмена».', 'Вставить']
      : ['Выберите «Из буфера обмена». При вводе по ссылке вставьте её и подтвердите.', details.from],
    incyMobile ? 'Не видно Freeman ONE? Откройте «Серверы».'
      : 'Подождите, пока появятся серверы Freeman ONE.',
  ], details.theme);
  const screen = !tv && !desktop && app.name === 'INCY' ? 'Внизу откройте «Подключение». ' : '';
  const connect = tv
    ? ['Выберите кнопку питания и нажмите «ОК» на пульте.', '⏻']
    : desktop
      ? ['В окне ' + app.name + ' нажмите кнопку подключения.', '⏻ Подключиться']
      : ['Нажмите кнопку питания над серверами.', '⏻'];
  writeSteps('connect-info', rabbit ? [
    'В первый раз: «Настроить VPN» → «Разрешить». Если iOS попросит, введите код разблокировки.',
    'Выберите «Авто» и нажмите «Подключиться» внизу.',
    'Проверьте статус «Подключено» вверху.',
  ] : [
    [screen + 'Выберите «Авто» или другой доступный сервер.', '● Авто'],
    connect,
    'Дождитесь подключения.',
  ], details.theme);
  if (rabbit) {
    get('import-info').children[0].append(rabbitFragment('add'));
    get('import-info').children[1].append(rabbitFragment('clipboard'));
    get('connect-info').children[1].append(rabbitFragment('connect'));
  } else if (happMobile) {
    get('import-info').children[1].append(happFragment('menu'));
    get('connect-info').children[1].append(happFragment('connect'));
  } else {
    if (tv) drawDiagram(get(app.name === 'INCY' ? 'incy-tv-diagram' : 'happ-tv-diagram'), os, app.name, details.theme, 'tv-import');
    else {
      get('import-info').children[1].append(importFigure);
      drawDiagram(get('import-diagram'), os, app.name, details.theme, 'import');
    }
    get('connect-info').children[1].append(connectFigure);
    drawDiagram(get('connect-diagram'), os, app.name, details.theme, 'connect');
  }
  document.querySelectorAll('.diagram-point, .mock-target').forEach(target => {
    target.classList.remove('guide-pointer-visible');
    if (!target.closest('[hidden]')) pointerObserver.observe(target);
  });
}
function choose(os) {
  for (const button of platformButtons) button.setAttribute('aria-pressed', String(button.dataset.os === os));
  choices.replaceChildren(...apps[os].map((app, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    const mark = document.createElement('img');
    mark.className = 'app-choice-icon';
    mark.alt = '';
    mark.src = '/assets/guide-' + presentation[app.name].icon + '-icon.jpg';
    const label = document.createElement('span');
    label.textContent = app.name;
    const glyph = document.createElement('span');
    glyph.className = 'app-choice-glyph';
    glyph.append(mark);
    button.append(glyph, label);
    button.dataset.index = index;
    button.addEventListener('click', () => renderApp(os, index));
    return button;
  }));
  get('app-picker').hidden = apps[os].length < 2;
  const defaultApp = os === 'apple' && macHapp ? apps.apple.findIndex(app => app.name === 'Happ') : 0;
  renderApp(os, selection.get(os) ?? defaultApp);
}
for (const button of platformButtons) button.addEventListener('click', () => choose(button.dataset.os));
const ua = navigator.userAgent;
const platform = /Android.*(?:TV|BRAVIA|SHIELD)/i.test(ua) ? 'tv' : /Android/i.test(ua) ? 'android' : /Windows/i.test(ua) ? 'windows' : 'apple';
const requested = params.get('os');
const requestedPlatform = ['ios','macos'].includes(requested) ? 'apple' : requested;
const initialOS = Object.hasOwn(apps, requestedPlatform) ? requestedPlatform : platform;
const requestedApp = params.get('app');
const initialApp = apps[initialOS].findIndex(app => presentation[app.name].icon === requestedApp);
if (initialApp >= 0) selection.set(initialOS, initialApp);
choose(initialOS);

const receipt = params.get('receipt');
const keyStatus = get('key-status');
function status(text) {
  keyStatus.textContent = text;
  keyStatus.hidden = false;
}
async function loadKey(order) {
  status('Проверяем доступ к подписке…');
  try {
    const response = await fetch('/api/buy/' + order + '/connection-key', {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) {
      status('Скопируйте ключ из письма или откройте инструкцию из браузера, где совершили покупку.');
      return;
    }
    const data = await response.json();
    if (typeof data.key !== 'string' || !data.key) throw Error('Missing subscription');
    get('key-sources').hidden = true;
    keyStatus.hidden = true;
    const copy = get('copy');
    copy.hidden = false;
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(data.key);
        copy.textContent = 'Ключ скопирован ✓';
      } catch {
        status('Не удалось скопировать автоматически. Выделите и скопируйте ссылку вручную.');
        const example = get('example');
        example.textContent = data.key;
        get('example-label').textContent = 'Ваш ключ';
        get('key-sources').querySelector('.guide-key-question').hidden = true;
        get('key-sources').querySelector('dl').hidden = true;
        get('key-sources').hidden = false;
      }
    });
  } catch {
    status('Не удалось загрузить ключ. Можно скопировать его из письма и продолжить подключение.');
  }
}
if (/^[1-9][0-9]{0,9}$/.test(receipt || '')) void loadKey(receipt);


const stepHelp = get('step-help');
const stepHelpLog = get('step-help-log');
const stepHelpTrigger = get('open-step-help');
const stepHelpInput = get('step-help-input');
const stepHelpSend = get('step-help-send');
const stepHelpStatus = get('step-help-status');
const guideChatKey = 'freeman.connect-guide.chat.v1';
const guideBadge = get('step-help-badge');
const chat = {history: [], draft: '', unread: 0, target: null};
try {
  const saved = JSON.parse(sessionStorage.getItem(guideChatKey));
  if (Array.isArray(saved?.history)) {
    chat.history = saved.history.slice(-24).filter(turn =>
      ['user', 'assistant'].includes(turn?.role) && typeof turn.text === 'string');
  }
  if (Number.isInteger(saved?.unread)) chat.unread = Math.max(0, Math.min(24, saved.unread));
} catch {
  // The conversation still works when browser storage is disabled.
}

function updateGuideBadge() {
  guideBadge.hidden = chat.unread === 0;
  guideBadge.textContent = String(chat.unread);
  stepHelpTrigger.setAttribute('aria-label', chat.unread
    ? 'Спросить помощника. Непрочитанных ответов: ' + chat.unread : 'Спросить помощника');
}

function saveGuideChat() {
  try {
    const history = chat.history.map(turn => ({
      ...turn,
      text: turn.text.replace(/(?:https?:\/\/|happ:\/\/|vless:\/\/|vmess:\/\/|trojan:\/\/|ss:\/\/)\S+/gi, '[ссылка]')
        .replace(/[^\s@]+@[^\s@]+\.[^\s@]+/g, '[email]')
        .replace(/(?:пароль|password|api[_ -]?key|токен|token)\s*[:=]\s*\S+/gi, '[секрет]')
        .replace(/(?:\+?\d[\d ()-]{8,}\d)/g, '[номер]'),
    }));
    sessionStorage.setItem(guideChatKey, JSON.stringify({history, unread: chat.unread}));
  } catch {
    // Keep the in-memory conversation as a fallback.
  }
  updateGuideBadge();
}
updateGuideBadge();

function showHelpMessage(text, role, actions = {}) {
  const message = document.createElement('div');
  message.className = 'step-help-message ' + role;
  if (role === 'assistant' && /^\/assets\/setup-guide-[a-z_]+\.png$/.test(actions.setup_image || '')) {
    const link = document.createElement('a');
    link.href = actions.setup_image;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    const image = document.createElement('img');
    image.className = 'step-help-guide-image';
    image.src = actions.setup_image;
    image.alt = 'Схема подключения';
    image.loading = 'lazy';
    link.append(image);
    message.append(link);
  }
  for (const block of text.split(/\n{2,}/)) {
    const paragraph = document.createElement('p');
    block.split(/(\*\*[^*\n]+\*\*)/).forEach((part, index) => {
      if (index % 2 === 0) paragraph.append(part);
      else {
        const emphasis = document.createElement('strong');
        emphasis.textContent = part.slice(2, -2);
        paragraph.append(emphasis);
      }
    });
    message.append(paragraph);
  }
  if (actions.desktop_choice) {
    const choices = document.createElement('div');
    choices.className = 'step-help-links';
    for (const [os, label] of [['windows', 'Windows'], ['macos', 'macOS']]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'secondary';
      button.textContent = label;
      button.disabled = Boolean(chat.pending);
      button.addEventListener('click', () => {
        const draft = stepHelpInput.value;
        if (draft.trim()) {
          stepHelpStatus.textContent = 'Сначала отправьте или очистите черновик.';
          stepHelpInput.focus();
          return;
        }
        chat.target = {os, source: getPagePlatform()};
            void sendGuideQuestion('Как настроить Freeman ONE на ' + label + '?', 'Не удалось загрузить инструкцию. Нажмите «' + label + '» ещё раз.');
      });
      choices.append(button);
    }
    message.append(choices);
  }
  if (actions.purchase || actions.desktop_download) {
    const links = document.createElement('div');
    links.className = 'step-help-links';
    const destinations = [];
    if (actions.purchase) destinations.push(['/buy', 'Купить без регистрации'],
      ['https://t.me/FreemanOneBot', 'Открыть Telegram-бот']);
    if (actions.desktop_download) destinations.push([desktopRelease, 'Скачать Happ для компьютера']);
    for (const [href, label] of destinations) {
      const link = document.createElement('a');
      link.className = 'secondary';
      link.href = href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = label;
      links.append(link);
    }
    message.append(links);
  }
  stepHelpLog.append(message);
  stepHelpLog.scrollTop = stepHelpLog.scrollHeight;
}

function showChat() {
  stepHelpLog.replaceChildren();
  showHelpMessage('Привет! 👋 Где вы остановились? Напишите, что видите на экране, и я подскажу, куда нажать дальше.', 'assistant');
  for (const turn of chat.history) showHelpMessage(turn.text, turn.role, turn.actions);
  if (chat.pending) showHelpMessage(chat.pending, 'user');
  stepHelpStatus.textContent = chat.pending ? 'Помощник готовит ответ…' : chat.error || '';
  stepHelpSend.disabled = Boolean(chat.pending);
  stepHelpInput.readOnly = Boolean(chat.pending);
}

function getPagePlatform() {
  return platformButtons.find(button => button.getAttribute('aria-pressed') === 'true').dataset.os;
}

function getHelpContext() {
  const page = getPagePlatform();
  const os = chat.target?.source === page ? chat.target.os : page;
  const app = chat.target?.source === page ? 'Happ' : apps[page][selection.get(page) || 0].name;
  if (chat.target?.source === page) return {os, app, instructions: ''};

  const listText = id => [...get(id).querySelectorAll(':scope > li')].map(li =>
    [...li.childNodes].filter(node => node.nodeType === Node.TEXT_NODE || node.nodeName === 'STRONG')
      .map(node => node.textContent).join('').trim()
  ).join('\n');
  const instructions = [
    ['Установка', os === 'tv' ? get('tv-install-info').textContent : get('download-info').textContent],
    ['Добавление подписки', os === 'tv'
      ? listText('tv-phone-steps') + '\n' + listText(app === 'INCY' ? 'incy-tv-transfer-steps' : 'happ-tv-transfer-steps')
      : listText('import-info')],
    ['Подключение', listText('connect-info')],
  ].map(([title, content]) => title + ':\n' + content).join('\n\n');
  return {os, app, instructions};
}

stepHelpTrigger.addEventListener('click', () => {
  stepHelpInput.value = chat.draft;
  showChat();
  stepHelp.showModal();
  chat.unread = 0;
  saveGuideChat();
  stepHelpLog.scrollTop = stepHelpLog.scrollHeight;
});
let backdropPress = false;
function clickedBackdrop(event) {
  if (event.target !== stepHelp) return false;
  const rect = stepHelp.getBoundingClientRect();
  return event.clientX < rect.left || event.clientX >= rect.right ||
    event.clientY < rect.top || event.clientY >= rect.bottom;
}
stepHelp.addEventListener('pointerdown', event => { backdropPress = clickedBackdrop(event); });
stepHelp.addEventListener('click', event => {
  if (backdropPress && clickedBackdrop(event)) stepHelp.close();
  backdropPress = false;
});
stepHelp.addEventListener('close', () => {
  backdropPress = false;
  stepHelpTrigger.focus({preventScroll: true});
});
document.addEventListener('visibilitychange', () => {
  if (stepHelp.open && !document.hidden && chat.unread) {
    chat.unread = 0;
    saveGuideChat();
  }
});
for (const button of platformButtons) {
  button.addEventListener('click', () => { chat.target = null; });
}
stepHelpInput.addEventListener('input', () => { chat.draft = stepHelpInput.value; });
get('step-help-form').addEventListener('submit', event => {
  event.preventDefault();
  void sendGuideQuestion(stepHelpInput.value.trim());
});

async function sendGuideQuestion(text, actionError) {
  if (!text || chat.pending) return;
  const context = getHelpContext();
  chat.pending = text;
  chat.error = '';
  if (!actionError) chat.draft = text;
  stepHelpInput.value = '';
  showChat();
  try {
    const response = await fetch('/api/support-agent/guide-message', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      signal: AbortSignal.timeout(65000),
      body: JSON.stringify({
        platform: context.os, app: context.app, instructions: context.instructions, text,
        desktop_selection: Boolean(actionError),
        history: chat.history.slice(-8).map(({role, text}) => ({role, text})),
      }),
    });
    const result = await response.json();
    if (!response.ok || typeof result.text !== 'string') {
      throw new Error(typeof result.detail === 'string' ? result.detail : 'Попробуйте отправить вопрос ещё раз.');
    }
    const actions = {
      purchase: result.purchase === true,
      desktop_choice: result.desktop_choice === true,
      desktop_download: result.desktop_download === true,
      setup_image: result.setup_image,
    };
    chat.history.push({role: 'user', text}, {role: 'assistant', text: result.text, actions});
    if (!stepHelp.open || document.hidden) chat.unread = Math.min(24, chat.unread + 1);
    saveGuideChat();
    chat.history = chat.history.slice(-24);
    chat.draft = '';
  } catch (error) {
    chat.error = actionError || (error.name === 'TimeoutError' || error instanceof TypeError || error instanceof SyntaxError
      ? 'Ответ не загрузился. Попробуйте отправить вопрос ещё раз.' : error.message);
  } finally {
    chat.pending = '';
    stepHelpInput.value = chat.draft;
    if (stepHelp.open) showChat();
  }
}
