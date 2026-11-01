/* 专属空间设置面板：权限分档 / API 接口 / 人设提示词。
   与 DSH 的设置页同一套行为：草稿先攒着、底部保存条、逐行「恢复默认」、
   危险项二次确认、关掉面板丢弃草稿。所有请求都是同源，写入带 X-CSRF-Token。 */
(function () {
  'use strict';

  var SECTIONS = [
    {id: 'guide', icon: '\u2726', label: '新手教程', sub: '怎么开权限、接口，怎么回来'},
    {id: 'permission', icon: '\u2699', label: '权限', sub: '分档与限制栏位'},
    {id: 'api', icon: '\u21c4', label: 'API 接口', sub: '端点、鉴权与自测'},
    {id: 'persona', icon: '\u270e', label: '人设提示词', sub: '表 / 里 的系统提示'}
  ];
  var RISK_ORDER = { publish: 1, outbound_message: 1, purchase: 1, delete_irreversible: 1 };

  // 第一次打开设置先停在「新手教程」；看过一次就不再挡路，随时能从左侧再打开。
  var GUIDE_KEY = 'dsh-space-guide-v1';

  function guideSeen() {
    try { return window.localStorage.getItem(GUIDE_KEY) === '1'; } catch (error) { return true; }
  }

  function markGuideSeen() {
    try { window.localStorage.setItem(GUIDE_KEY, '1'); } catch (error) { /* 隐私模式：只影响下次是否自动弹出 */ }
  }

  var state = {
    section: 'permission', data: null, persona: null, csrf: '',
    permDraft: null, apiDraft: null, personaDraft: null, personaBase: null,
    error: '', note: '', busy: false, confirmResolve: null
  };

  function q(sel, root) { return (root || document).querySelector(sel); }

  // 已接线的开关（自主探索）：生效值在实时状态 live 里，存档里只有「策略记录」那些行。
  // 只读存档会把这一栏显示成关闭，拨上去再读回又是关闭，看着就像改不了。
  var LIVE_SWITCH = {autonomy: 'autonomy_enabled'};
  function switchValue(row) {
    var liveKey = LIVE_SWITCH[row.id];
    if (liveKey && state.data && state.data.live && typeof state.data.live[liveKey] === 'boolean') {
      return !!state.data.live[liveKey];
    }
    return !!row.value;
  }

  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.slice(0, 2) === 'on') node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    });
    (kids || []).forEach(function (kid) {
      if (kid === null || kid === undefined || kid === false) return;
      node.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid);
    });
    return node;
  }

  var shell = null;

  function build() {
    if (shell) return shell;
    var nav = el('nav', {class: 'sp-nav'}, [el('div', {class: 'sp-nav-label', text: '设置'})]);
    SECTIONS.forEach(function (item) {
      nav.appendChild(el('button', {'data-section': item.id, type: 'button'}, [
        el('i', {text: item.icon}),
        el('span', {}, [el('span', {text: item.label}), el('small', {text: item.sub})]),
        el('span', {class: 'sp-dirty-dot'})
      ]));
    });
    nav.appendChild(el('div', {class: 'sp-nav-foot', html:
      '只改本空间主人的设置与人格正文：不扩大模型权限、<br>不碰设备、不替主人重启运行时。'}));

    var overlay = el('div', {class: 'sp-overlay', hidden: true, role: 'dialog', 'aria-modal': 'true',
      'aria-label': '专属空间设置'}, [
      el('header', {class: 'sp-head'}, [
        el('div', {class: 'sp-title'}, [
          el('strong', {text: '专属空间设置'}),
          el('span', {class: 'sp-sub', 'data-ref': 'sub'})
        ]),
        el('div', {class: 'sp-actions'}, [
          el('button', {class: 'sp-self', 'data-ref': 'reload', type: 'button', text: '重新读取'}),
          el('a', {class: 'sp-self sp-link', href: '/permissions', target: '_blank', rel: 'noreferrer',
            text: '权限约束原文'}),
          el('button', {class: 'sp-close', 'data-ref': 'close', type: 'button', title: '关闭（Esc）',
            'aria-label': '关闭', text: '\u00d7'})
        ])
      ]),
      el('div', {class: 'sp-body'}, [nav, el('main', {class: 'sp-main'}, [
        el('div', {class: 'sp-wrap', 'data-ref': 'view'})
      ])])
    ]);

    var savebar = el('div', {class: 'sp-savebar', 'data-ref': 'savebar'}, [
      el('span', {class: 'sp-err', 'data-ref': 'err'}),
      el('span', {class: 'sp-save-hint', 'data-ref': 'hint'}),
      el('div', {class: 'sp-save-actions'}, [
        el('button', {class: 'sp-self', 'data-ref': 'discard', type: 'button', text: '放弃改动'}),
        el('button', {class: 'sp-self sp-btn-primary', 'data-ref': 'save', type: 'button',
          text: '保存'})
      ])
    ]);

    var dialog = el('dialog', {class: 'sp-confirm', 'data-ref': 'confirm'}, [
      el('form', {method: 'dialog'}, [
        el('h2', {'data-ref': 'ctitle'}),
        el('div', {'data-ref': 'cbody'}),
        el('div', {class: 'sp-confirm-actions'}, [
          el('button', {class: 'sp-self', value: 'cancel', type: 'button', 'data-ref': 'cno', text: '取消'}),
          el('button', {class: 'sp-danger', value: 'ok', type: 'submit', 'data-ref': 'cyes', text: '确认'})
        ])
      ])
    ]);

    var toastNode = el('div', {class: 'sp-toast', role: 'status', 'aria-live': 'polite'});

    document.body.appendChild(overlay);
    document.body.appendChild(savebar);
    document.body.appendChild(dialog);
    document.body.appendChild(toastNode);

    overlay.addEventListener('click', onNav);
    q('[data-ref=close]', overlay).addEventListener('click', close);
    q('[data-ref=reload]', overlay).addEventListener('click', function () { load(true); });
    q('[data-ref=discard]', savebar).addEventListener('click', function () { discardDraft(); });
    q('[data-ref=save]', savebar).addEventListener('click', function () { saveDraft(); });
    q('[data-ref=cno]', dialog).addEventListener('click', function () { settleConfirm(false); });
    q('[data-ref=cyes]', dialog).addEventListener('click', function (event) {
      event.preventDefault();
      settleConfirm(true);
    });
    dialog.addEventListener('cancel', function (event) { event.preventDefault(); settleConfirm(false); });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !overlay.hidden && !dialog.open) close();
    });

    shell = {overlay: overlay, savebar: savebar, dialog: dialog, toast: toastNode, view: q('[data-ref=view]', overlay)};
    return shell;
  }

  function toast(message, bad) {
    var node = build().toast;
    node.textContent = message;
    node.style.background = bad ? '#8a4a3f' : '';
    node.classList.add('show');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { node.classList.remove('show'); }, bad ? 5200 : 2600);
  }

  function api(path, method, body) {
    var headers = {'Accept': 'application/json'};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
    return fetch(path, {
      method: method || 'GET', credentials: 'same-origin', headers: headers,
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (value) {
        if (!response.ok) {
          var error = new Error((value && value.error) || ('HTTP ' + response.status));
          error.code = (value && value.code) || 'HTTP_' + response.status;
          throw error;
        }
        return value;
      });
    });
  }

  function confirmDialog(title, bodyNode, okText, danger) {
    var shellNow = build();
    q('[data-ref=ctitle]', shellNow.dialog).textContent = title;
    var holder = q('[data-ref=cbody]', shellNow.dialog);
    holder.textContent = '';
    holder.appendChild(bodyNode);
    var yes = q('[data-ref=cyes]', shellNow.dialog);
    yes.textContent = okText || '确认';
    yes.classList.toggle('sp-danger', !!danger);
    shellNow.dialog.showModal();
    return new Promise(function (resolve) { state.confirmResolve = resolve; });
  }

  function settleConfirm(value) {
    var resolve = state.confirmResolve;
    state.confirmResolve = null;
    var dialog = build().dialog;
    if (dialog.open) dialog.close();
    if (resolve) resolve(value);
  }

  function fmtBytes(value) {
    if (value === null || value === undefined) return '\u2014';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var index = 0;
    while (value >= 1024 && index < units.length - 1) { value = value / 1024; index += 1; }
    return (index === 0 ? value : value.toFixed(1)) + ' ' + units[index];
  }

  function same(one, two) { return JSON.stringify(one) === JSON.stringify(two); }

  /* ---------- 新手教程 ---------- */

  function guideStep(index, title, lines) {
    return el('div', {class: 'sp-card sp-guide-step'}, [
      el('header', {}, [
        el('div', {}, [el('h3', {}, [
          el('span', {class: 'sp-guide-num', text: String(index)}),
          el('span', {text: title})
        ])])
      ]),
      el('ul', {class: 'sp-guide-list'}, lines.map(function (line) {
        return el('li', {text: line});
      }))
    ]);
  }

  function renderGuide() {
    var first = !guideSeen();
    var wrap = el('div', {});
    wrap.appendChild(sectionHead('新手教程', '这里是本机控制台（127.0.0.1:19991）。设置只改这个空间自己的权限栏位、接口备注和人格正文，'
      + '不修改 DSH 安装目录，也不会替你重启运行时。'));

    wrap.appendChild(el('div', {class: 'sp-constraint'}, [
      el('div', {}, [
        el('strong', {text: first ? '第一次打开设置：照着下面四步走一遍就会了' : '教程回看：四步和第一次一样'}),
        el('div', {text: '左侧四个分页各有分工——新手教程讲怎么用，权限拨开关，API 接口看端点与鉴权，'
          + '人设提示词改表 / 里的系统提示。看完随时可以点左上「◈ 空间总览」回到工作台。'})
      ])
    ]));

    wrap.appendChild(guideStep(1, '先开权限：左侧「权限」页', [
      '第一张卡是「权限分档」：本空间只放开「工作区内修改」这一档，另外三档是灰的，点一下就能看到不可选的原因（来自权限约束.md，不是占位）。',
      '往下按分组列出每一款权限：左边写它是什么、来源是哪一条，右边是开关。只有标「可改」的行能拨；标「固定」的来自约束文件，界面改不了。',
      '标「策略记录」的开关只把主人的选择记进 config.json，执行程序在真正动设备前仍会逐次拦下来要你当场确认；标「已接线」的才会真的改变派发行为。',
      '拨完不会立刻生效：点底部「保存」才写入磁盘，想反悔点「放弃改动」或逐行「恢复默认」。'
    ]));

    wrap.appendChild(guideStep(2, '再开接口：左侧「API 接口」页', [
      '这一页是只读清单：鉴权三条（同源页面 + 主人 Cookie + X-CSRF-Token）、本机三个端口、专属运行时的会话与额度。',
      '三个端口：19991 控制台（就是现在这个页面）、19992 专属运行时、19993 思考观察服务。观察服务没起来时，新任务会被挡下并显示「本轮连接或接续未完成」，保留记录但不偷偷重发。',
      '页面上的写入令牌可以一键复制。外部脚本要写数据，就在请求头带上 X-CSRF-Token；其余接口只读。',
      '这一页唯一能改的是给主人自己看的一句备注，保存后写在设置里，不影响任何通路。'
    ]));

    wrap.appendChild(guideStep(3, '改人设：左侧「人设提示词」页', [
      '表、里两段完整的系统提示，都能编辑，也能「恢复上一篇」；保存前旧版会先留档到 data/persona-archive/，可以回滚。',
      '保存只写了磁盘，正在跑的运行时用的还是旧提示词；要生效得重启专属运行时。重启会打断正在跑的对话，所以这里不代劳，由你决定什么时候重启。',
      '重启之后回到这一页，状态会自己变成「已载入」；没变就点一下「我已重启」核对。'
    ]));

    wrap.appendChild(guideStep(4, '出去以后怎么回来', [
      '随时点顶部导航最右边的「⚙ 设置」；也可以在地址栏直接打开 http://127.0.0.1:19991/#settings。',
      '面板里按 Esc 或右上角 × 关闭。有没保存的改动时会先问一句，丢了就是丢了，磁盘上的值不受影响。',
      '想重看这页教程：再打开设置，点左侧「新手教程」即可。'
    ]));

    var foot = el('div', {class: 'sp-guide-foot'});
    foot.appendChild(el('span', {class: 'sp-dim sp-tiny', text: first
      ? '记住这次之后，设置会直接停在「权限」页，不再自动打开教程。'
      : '教程已看过，设置默认停在「权限」页。'}));
    if (first) foot.appendChild(el('button', {class: 'sp-self sp-btn-primary', type: 'button',
      text: '我知道了，不再自动弹出', onclick: function () {
        markGuideSeen();
        toast('好，之后点顶部「⚙ 设置」就直接进权限页。');
        state.section = 'permission';
        state.note = '';
        render();
        build().view.parentNode.scrollTop = 0;
      }}));
    wrap.appendChild(foot);
    return wrap;
  }

  /* ---------- 载入 ---------- */

  function load(reload) {
    var shellNow = build();
    q('[data-ref=sub]', shellNow.overlay).textContent = reload ? '正在重新读取…' : '正在读取…';
    return Promise.all([api('/api/owner/settings'), api('/api/owner/persona').catch(function () { return null; })])
      .then(function (values) {
        var data = values[0];
        state.data = data;
        state.csrf = data.csrf || state.csrf;
        state.persona = values[1] || null;
        state.error = '';
        // 全新读取时草稿回到服务器值；只有明确放弃才丢草稿
        // 后端把 restrictions 作为「整行对象数组」返回，草稿只保留可改开关的布尔值
        var permValues = {};
        ((data.permissions && data.permissions.restrictions) || []).forEach(function (row) {
          if (row && row.kind === 'switch' && row.editable) permValues[row.id] = switchValue(row);
        });
        state.permDraft = {preset: data.permissions.preset, values: permValues};
        state.permBase = JSON.parse(JSON.stringify(state.permDraft));
        state.apiDraft = {notes: (data.api && data.api.notes) || ''};
        state.apiBase = JSON.parse(JSON.stringify(state.apiDraft));
        if (state.persona && state.persona.ok) {
          state.personaDraft = {表: state.persona.personas['表'] || '', 里: state.persona.personas['里'] || ''};
          state.personaBase = JSON.parse(JSON.stringify(state.personaDraft));
        }
        q('[data-ref=sub]', shellNow.overlay).textContent =
          '控制台 127.0.0.1:' + ((data.api.server && data.api.server.control_console) || '19991') +
          ' \u00b7 读取于 ' + (data.generated_at || '').replace('T', ' ').slice(0, 19);
        render();
        refreshDirty();
      })
      .catch(function (error) {
        q('[data-ref=sub]', shellNow.overlay).textContent = '读取失败：' + error.message;
        renderError(error);
      });
  }

  function renderError(error) {
    var view = build().view;
    view.textContent = '';
    view.appendChild(el('div', {class: 'sp-constraint'}, [
      el('div', {}, [el('strong', {text: '读取不到主人设置'}),
        el('div', {text: error.message + '（' + (error.code || '') + '）—— 可能是主人会话已过期，'
          + '请从桌面快捷方式重新进入空间。'})])
    ]));
    view.appendChild(el('button', {class: 'sp-self', type: 'button', text: '重试',
      onclick: function () { load(true); }}));
  }

  /* ---------- 渲染 ---------- */

  function render() {
    var view = build().view;
    view.textContent = '';
    var panel = state.section;
    Array.prototype.forEach.call(build().overlay.querySelectorAll('.sp-nav button'), function (button) {
      button.setAttribute('aria-current', String(button.getAttribute('data-section') === panel));
    });
    if (panel === 'guide') { view.appendChild(renderGuide()); refreshDirty(); return; }
    if (!state.data) return;
    if (panel === 'permission') view.appendChild(renderPermission());
    else if (panel === 'api') view.appendChild(renderApi());
    else view.appendChild(renderPersona());
    refreshDirty();
  }

  function sectionHead(title, text) {
    return el('div', {class: 'sp-section-head'}, [
      el('h2', {text: title}),
      el('p', {text: text})
    ]);
  }

  function liveChip(label, ok, message) {
    return el('span', {class: 'sp-tag', title: message || ''}, [
      el('span', {class: ok ? 'sp-ok' : 'sp-bad', text: (ok ? '\u25cf ' : '\u25cb ') + label})
    ]);
  }

  function renderPermission() {
    var data = state.data;
    var live = data.live || {};
    var wrap = el('div', {});
    wrap.appendChild(sectionHead('权限', '权限分档照搬 DSH 的四档形状，但这里的档位、模型、额度和宿主边界由「权限约束.md」定死；'
      + '能改的栏位只有标了「可改」的开关，改完点底部保存条才写入。'));

    wrap.appendChild(el('div', {class: 'sp-constraint'}, [
      el('div', {}, [
        el('strong', {text: '当前生效的边界'}),
        el('div', {text: '模型只跑 ' + (live.model || 'deepseek-flash') + '；宿主文件与终端工具不开放；'
          + '手机与电脑全局互斥、操作串行；越界扩大访问须改控制程序，不是界面开关。'})
      ]),
      el('a', {class: 'sp-self sp-link', href: '/permissions', target: '_blank', rel: 'noreferrer',
        text: '读原文'})
    ]));

    var presetCard = el('div', {class: 'sp-card'}, [
      el('header', {}, [
        el('div', {}, [el('h3', {text: '权限分档'}),
          el('p', {text: '对应 DSH 设置 → 权限 → 默认权限档位。由你选择四个档位，保存后在下一项任务开始前应用。'})]),
        liveChip('空间' + (live.paused ? '已暂停' : '运行中'), !live.paused,
          live.paused ? '暂停时所有授权立即失效；恢复在「自主与执行」的暂停那一行' : '空间正在运行')
      ])
    ]);
    var grid = el('div', {class: 'sp-preset-grid'});
    (data.permissions.presets || []).forEach(function (item) {
      var current = item.id === state.permDraft.preset;
      var button = el('button', {class: 'sp-preset', type: 'button', disabled: !item.available,
        'aria-pressed': String(current)}, [
        el('strong', {text: item.label + (current ? ' \u00b7 当前' : '')}),
        item.exp ? el('em', {text: 'EXP'}) : null,
        el('small', {text: item.desc})
      ]);
      button.addEventListener('click', function () {
        if (!item.available) {
          state.note = item.reason || '这一档在本空间不可选。';
          render();
          toast(item.reason || '这一档在本空间不可选。', true);
          return;
        }
        if (state.busy) return;
        state.permDraft.preset = item.id;
        state.note = '已选择「' + item.label + '」，点击保存应用。';
        render();
      });
      grid.appendChild(button);
    });
    presetCard.appendChild(grid);
    presetCard.appendChild(el('div', {class: 'sp-preset-hint', text: state.note
      || '四个档位均可选择。自动审查提供完整工具，由审查模型检查实际动作；普通任务不逐步申请。'}));
    wrap.appendChild(presetCard);

    var groups = [];
    (data.permissions.restrictions || []).forEach(function (item) {
      var bucket = groups.filter(function (entry) { return entry.name === item.group; })[0];
      if (!bucket) { bucket = {name: item.group, rows: []}; groups.push(bucket); }
      bucket.rows.push(item);
    });

    groups.forEach(function (bucket) {
      var card = el('div', {class: 'sp-card'}, [
        el('header', {}, [
          el('div', {}, [el('h3', {text: bucket.name}),
            el('p', {text: bucket.name === '设备' ? '设备连接与互斥规则' :
              bucket.name === '自主与执行' ? '自主开关、派发与暂停' :
              bucket.name === '对外动作与不可逆' ? '需要主人逐次确认的动作（这里是策略记录）' :
              bucket.name === '宿主与凭据' ? '控制程序层的硬边界' :
              bucket.name === '模型与额度' ? '模型、额度、存储与观察门' : '人格身份与事后核对'})]),
          el('span', {class: 'sp-tag', text: bucket.rows.filter(function (row) { return row.editable; }).length + ' 项可改'})
        ])
      ]);
      bucket.rows.forEach(function (row) { card.appendChild(renderRestriction(row)); });
      wrap.appendChild(card);
    });
    return wrap;
  }

  function renderRestriction(row) {
    var draft = state.permDraft;
    var ident = row.id;
    var changed = row.kind === 'switch' && !!draft.values[ident] !== switchValue(row);
    var label = el('div', {class: 'sp-label'}, [el('span', {text: row.label})]);
    if (changed) label.appendChild(el('span', {class: 'sp-overridden', text: '待保存'}));
    if (row.editable && !row.wired && row.kind === 'switch') label.appendChild(el('span', {class: 'sp-locked', text: '策略记录'}));
    if (row.wired && row.kind === 'switch') label.appendChild(el('span', {class: 'sp-wired', text: '已接线'}));
    if (!row.editable) label.appendChild(el('span', {class: 'sp-locked', text: '固定'}));

    var source = el('div', {class: 'sp-src'}, [
      el('span', {text: '来源 '}),
      el('a', {href: '/permissions', target: '_blank', rel: 'noreferrer', text: row.source})
    ]);
    if (row.kind === 'switch' && changed) {
      source.appendChild(el('button', {class: 'sp-reset', type: 'button', text: '恢复默认', onclick: function () {
        draft.values[ident] = !!row.default;
        render();
      }}));
    }
    var left = el('div', {}, [label, el('div', {class: 'sp-desc', text: row.desc})]);
    if (row.note) left.appendChild(el('div', {class: 'sp-desc sp-note-warn', text: '\u26a0 ' + row.note}));
    if (row.detail && row.detail.length) row.detail.forEach(function (text) {
      left.appendChild(el('div', {class: 'sp-desc', text: text}));
    });
    left.appendChild(source);

    var right;
    if (row.kind === 'switch') {
      var input = el('input', {type: 'checkbox', role: 'switch', 'aria-label': row.label});
      input.checked = !!draft.values[ident];
      input.addEventListener('change', function () {
        var value = input.checked;
        if (value && RISK_ORDER[ident]) {
          confirmDialog('打开「' + row.label + '」？', el('div', {}, [
            el('p', {text: '这是需要主人逐次确认的动作类别。打开后它只记录为「主人的选择」，'
              + '执行程序仍会按权限约束.md 逐次拦截并要你当场确认。'}),
            el('ul', {}, [el('li', {text: '来源：' + row.source}),
              el('li', {text: row.desc})])
          ]), '我确认', true).then(function (ok) {
            if (!ok) { input.checked = false; return; }
            draft.values[ident] = true;
            render();
          });
          return;
        }
        draft.values[ident] = value;
        render();
      });
      right = el('div', {class: 'sp-ctrl'}, [el('label', {class: 'sp-switch'}, [input, el('span', {})])]);
      if (row.wired) right.appendChild(el('span', {class: 'sp-dim sp-tiny', text: '写 config.json'}));
    } else if (row.kind === 'state') {
      var liveValue = !!row.value;
      var liveInput = el('input', {type: 'checkbox', role: 'switch', 'aria-label': row.label});
      liveInput.checked = liveValue;
      liveInput.addEventListener('change', function () {
        var target = liveInput.checked;
        liveInput.disabled = true;
        api('/api/control/' + (target ? 'resume' : 'pause'), 'POST', {csrf: state.csrf}).then(function () {
          toast(target ? '已恢复：只接受新任务，旧队列保留待核对。' : '已暂停：当前授权立即失效。');
          load(true);
        }).catch(function (error) {
          liveInput.checked = !target;
          liveInput.disabled = false;
          toast('操作失败：' + error.message, true);
        });
      });
      right = el('div', {class: 'sp-ctrl'}, [
        el('span', {class: 'sp-value', text: row.value_text || ''}),
        el('label', {class: 'sp-switch'}, [liveInput, el('span', {})])
      ]);
      left.insertBefore(el('div', {class: 'sp-desc sp-note-warn',
        text: '这一项点下去立即生效，不进草稿。'}), left.firstChild);
    } else {
      right = el('div', {class: 'sp-ctrl'}, [el('span', {class: 'sp-value', text: row.value_text || '\u2014'})]);
    }

    var node = el('div', {class: 'sp-row' + (row.editable ? '' : ' sp-row-locked'), 'data-id': ident}, [left, right]);
    return node;
  }

  function renderApi() {
    var data = state.data;
    var info = data.api || {};
    var wrap = el('div', {});
    wrap.appendChild(sectionHead('API 接口', '空间对外的接口全部落在 127.0.0.1 上：浏览器侧走主人会话 + CSRF，'
      + '模型与设备侧走 Bearer 桥凭据。这里只读展示清单，唯一能改的是给主人看的一句备注。'));

    var auth = info.auth || {};
    var authCard = el('div', {class: 'sp-card'}, [
      el('header', {}, [el('div', {}, [el('h3', {text: '鉴权与入口'}),
        el('p', {text: '写入类接口要同时满足：同源页面 + 主人 Cookie + CSRF 令牌。'})])])
    ]);
    [['主人会话', auth.owner_session], ['写入令牌', auth.csrf], ['来源校验', auth.origin],
      ['模型 / 设备', auth.bridge], ['引导凭据', auth.capability]].forEach(function (pair) {
      if (!pair[1]) return;
      authCard.appendChild(el('div', {class: 'sp-row'}, [
        el('div', {}, [el('div', {class: 'sp-label'}, [el('span', {text: pair[0]})]),
          el('div', {class: 'sp-desc', text: pair[1]})]),
        el('div', {class: 'sp-ctrl'}, [el('span', {class: 'sp-value', text: pair[0] === '主人会话' ? '同源' : ''})])
      ]));
    });
    var server = info.server || {};
    authCard.appendChild(el('div', {class: 'sp-row'}, [
      el('div', {}, [el('div', {class: 'sp-label'}, [el('span', {text: '本机端口'})]),
        el('div', {class: 'sp-desc', text: '控制台 ' + (server.control_console || '19991')
          + ' \u00b7 专属运行时 ' + (server.runtime || 19992) + ' \u00b7 观察服务 ' + (server.observer || 19993)})]),
      el('div', {class: 'sp-ctrl'}, [el('span', {class: 'sp-code', text: server.base || 'http://127.0.0.1:19991'})])
    ]));
    var runtime = info.runtime || {};
    authCard.appendChild(el('div', {class: 'sp-row'}, [
      el('div', {}, [el('div', {class: 'sp-label'}, [el('span', {text: '专属运行时'})]),
        el('div', {class: 'sp-desc', text: '会话 ' + (runtime.session_id || '（未连接）')
          + ' \u00b7 人格 ' + (runtime.persona_version || '\u2014')})]),
      el('div', {class: 'sp-ctrl'}, [liveChip(runtime.ready ? '已连接' : '未连接', !!runtime.ready,
        runtime.chat_ready ? '可对话' : '尚未可对话')])
    ]));
    var budget = info.budget || {};
    authCard.appendChild(el('div', {class: 'sp-row'}, [
      el('div', {}, [el('div', {class: 'sp-label'}, [el('span', {text: '额度'})]),
        el('div', {class: 'sp-desc', text: '日额度 ' + budget.daily_budget_cny + ' 元 \u00b7 停止线 '
          + budget.stop_at_cny + ' 元 \u00b7 今日已记 ' + budget.local_recorded_cny + ' 元'})]),
      el('div', {class: 'sp-ctrl'}, [el('span', {class: 'sp-value',
        text: (budget.blockers || []).length ? (budget.blockers || []).length + ' 项阻塞' : '无阻塞'})])
    ]));
    wrap.appendChild(authCard);

    var rows = info.endpoints || [];
    var groups = [];
    rows.forEach(function (row) {
      var bucket = groups.filter(function (entry) { return entry.name === row.group; })[0];
      if (!bucket) { bucket = {name: row.group, rows: []}; groups.push(bucket); }
      bucket.rows.push(row);
    });
    groups.forEach(function (bucket) {
      var card = el('div', {class: 'sp-card'}, [
        el('header', {}, [el('div', {}, [el('h3', {text: bucket.name}),
          el('p', {text: bucket.rows.length + ' 个端点'})])])
      ]);
      var table = el('table', {class: 'sp-table'}, [
        el('thead', {}, [el('tr', {}, [el('th', {text: '方法'}), el('th', {text: '路径'}),
          el('th', {text: '用途'}), el('th', {text: '鉴权'}), el('th', {text: '自测'})])])
      ]);
      var body = el('tbody');
      bucket.rows.forEach(function (row) {
        var method = el('span', {class: 'sp-method ' + row.method.toLowerCase(), text: row.method});
        var pathCell = el('td', {}, [
          el('span', {class: 'sp-code', text: row.path})
        ]);
        var copy = el('button', {class: 'sp-copy sp-ml', type: 'button', text: '复制'});
        copy.addEventListener('click', function () {
          var text = (server.base || 'http://127.0.0.1:19991') + row.path.replace('*', '');
          if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { toast('已复制接口地址'); });
          else toast(text);
        });
        pathCell.appendChild(copy);
        var testCell = el('td', {});
        if (row.test) {
          var button = el('button', {class: 'sp-self', type: 'button', text: '自测'});
          button.addEventListener('click', function () {
            button.disabled = true;
            button.textContent = '请求中…';
            fetch(row.path + (row.path.indexOf('?') < 0 && row.path === '/api/memory' ? '?q=' : ''),
              {credentials: 'same-origin', headers: {'Accept': 'application/json'}})
              .then(function (response) {
                var ok = response.ok;
                button.textContent = 'HTTP ' + response.status;
                button.classList.toggle('sp-ok', ok);
                button.classList.toggle('sp-bad', !ok);
                button.title = ok ? '接口可用' : '接口返回了非 2xx（可能是当前状态不允许）';
              })
              .catch(function (error) {
                button.textContent = '失败';
                button.classList.add('sp-bad');
                button.title = error.message;
              })
              .then(function () { button.disabled = false; });
          });
          testCell.appendChild(button);
        } else {
          testCell.appendChild(el('span', {class: 'sp-dim', text: '写入类' }));
        }
        body.appendChild(el('tr', {}, [el('td', {}, [method]), pathCell,
          el('td', {text: row.purpose}), el('td', {}, [el('span', {class: 'sp-dim', text: row.auth})]), testCell]));
      });
      table.appendChild(body);
      card.appendChild(table);
      wrap.appendChild(card);
    });

    var noteCard = el('div', {class: 'sp-card'}, [
      el('header', {}, [el('div', {}, [el('h3', {text: '接口备注'}),
        el('p', {text: '给主人自己看的备忘，只存在 data/owner-settings.json，不进入任何提示词。'})])])
    ]);
    var field = el('div', {class: 'sp-field'}, [
      el('label', {for: 'sp-api-notes', text: '备注'}),
      el('textarea', {id: 'sp-api-notes', rows: '4', placeholder: '例如：手机画面接口需要先连接模拟器；MCP 入口只能被运行时的桥凭据调用。'})
    ]);
    field.appendChild(el('div', {class: 'sp-help', text: '最多 2000 字。'}));
    noteCard.appendChild(field);
    q('#sp-api-notes', noteCard).value = state.apiDraft.notes;
    q('#sp-api-notes', noteCard).addEventListener('input', function (event) {
      state.apiDraft.notes = event.target.value;
      refreshDirty();
    });
    wrap.appendChild(noteCard);
    return wrap;
  }

  function renderPersona() {
    var wrap = el('div', {});
    var persona = state.persona;
    if (!persona || !persona.ok) {
      wrap.appendChild(sectionHead('人设提示词', '人格快照读不到。'));
      wrap.appendChild(el('div', {class: 'sp-constraint'}, [
        el('div', {}, [el('strong', {text: '不可用'}),
          el('div', {text: (persona && persona.message) || '人格入口缺失或格式有误。'})])
      ]));
      return wrap;
    }
    var pending = !!persona.pending_restart;
    wrap.appendChild(sectionHead('人设提示词', '真源只有一份：' + (persona.package || '').split(/[\\/]/).pop()
      + ' 里「表」「里」两段 system_prompt。保存会重算固定哈希并重新生成运行时补丁，'
      + '但不会自动重启运行时 —— 那会把正在跑的任务砍掉。'));

    var status = el('div', {class: 'sp-constraint'}, [
      el('div', {}, [
        el('strong', {text: '版本 ' + (persona.version || '\u2014') + ' \u00b7 '
          + ((persona.raw_bytes || 0) / 1024).toFixed(1) + ' KB \u00b7 包哈希 '
          + (persona.package_sha256 || '').slice(0, 12)}),
        el('div', {text: pending
          ? '有改动还没载入：正在跑的运行时仍是旧提示词。确认可以打断当前任务后，再重启专属运行时；重启后这一栏会自己变成「已载入」。'
          : '磁盘上的补丁与这份正文一致，运行时已按它启动。'})
      ]),
      el('div', {class: 'sp-ctrl'}, [
        liveChip(pending ? '待重启' : '已载入', !pending),
        pending ? el('button', {class: 'sp-self', type: 'button', text: '我已重启', onclick: function () {
          api('/api/owner/persona', 'POST', {action: 'ack_restart'}).then(function () {
            toast('已确认载入。'); load(true);
          }).catch(function (error) { toast('确认失败：' + error.message, true); });
        }}) : null
      ])
    ]);
    wrap.appendChild(status);

    var labels = ['表', '里'];
    var card = el('div', {class: 'sp-card'}, [
      el('header', {}, [
        el('div', {}, [el('h3', {text: '人格正文'}),
          el('p', {text: '提示词前面还会自动拼上「权限约束.md」全文，不需要在这里重复权限内容。'})]),
        el('span', {class: 'sp-tag', text: '两段都是完整系统提示'})
      ])
    ]);
    var toolbar = el('div', {class: 'sp-prompt-toolbar'});
    var meta = el('div', {class: 'sp-meta'});
    var active = '表';
    var area = el('textarea', {spellcheck: 'false', 'aria-label': '人格正文'});
    var foot = el('div', {class: 'sp-prompt-foot'});
    var counter = el('span', {class: 'sp-count'});
    var warn = el('span', {class: 'sp-warn'});

    function syncArea() {
      area.value = state.personaDraft[active] || '';
      var changed = area.value !== state.personaBase[active];
      counter.textContent = area.value.length + ' / ' + (persona.max_chars || 12000) + ' 字'
        + (changed ? ' \u00b7 有改动' : '');
      warn.textContent = area.value.trim().length < (persona.min_chars || 200)
        ? '太短（至少 ' + (persona.min_chars || 200) + ' 字）' : '';
      meta.textContent = '真源 ' + (persona.package || '').split(/[\\/]/).pop()
        + (persona.bom ? ' \u00b7 带 BOM' : '') + (persona.crlf ? ' \u00b7 含 CRLF' : ' \u00b7 UTF-8/LF');
      refreshDirty();
    }

    labels.forEach(function (label) {
      var button = el('button', {class: 'sp-self', type: 'button', text: label + '人格'});
      button.addEventListener('click', function () {
        active = label;
        Array.prototype.forEach.call(toolbar.querySelectorAll('button[data-tab]'), function (other) {
          other.classList.toggle('sp-ok', other.getAttribute('data-tab') === active);
          other.setAttribute('aria-pressed', String(other.getAttribute('data-tab') === active));
        });
        syncArea();
      });
      button.setAttribute('data-tab', label);
      if (label === active) button.classList.add('sp-ok');
      toolbar.appendChild(button);
    });
    toolbar.appendChild(el('button', {class: 'sp-reset', type: 'button', text: '恢复上一篇', onclick: function () {
      state.personaDraft[active] = state.personaBase[active];
      syncArea();
      toast('已回到上一次从磁盘读到的正文（还没保存）。');
    }}));
    toolbar.appendChild(meta);
    card.appendChild(toolbar);
    var promptBox = el('div', {class: 'sp-prompt'}, [area]);
    area.addEventListener('input', function () {
      state.personaDraft[active] = area.value;
      var changed = area.value !== state.personaBase[active];
      counter.textContent = area.value.length + ' / ' + (persona.max_chars || 12000) + ' 字'
        + (changed ? ' \u00b7 有改动' : '');
      warn.textContent = area.value.trim().length < (persona.min_chars || 200)
        ? '太短（至少 ' + (persona.min_chars || 200) + ' 字）' : '';
      refreshDirty();
    });
    card.appendChild(promptBox);
    foot.appendChild(el('span', {text: '保存后旧版会留档在 data/persona-archive/，可回滚。'}));
    foot.appendChild(warn);
    foot.appendChild(counter);
    card.appendChild(foot);
    card.appendChild(el('div', {class: 'sp-note', text: '注意：不重启就只改了磁盘，运行时内存里仍是旧提示词；'
      + '重启会打断正在跑的任务，所以要不要重启由你决定，这里不代劳。'}));
    wrap.appendChild(card);
    syncArea();

    if ((persona.history || []).length) {
      var historyCard = el('div', {class: 'sp-card'}, [
        el('header', {}, [el('div', {}, [el('h3', {text: '留档'}),
          el('p', {text: '每次保存前的原始字节，按时间倒序。'})])])
      ]);
      var table = el('table', {class: 'sp-table'}, [
        el('thead', {}, [el('tr', {}, [el('th', {text: '文件'}), el('th', {text: '大小'}), el('th', {text: '时间'})])])
      ]);
      var body = el('tbody');
      persona.history.forEach(function (row) {
        body.appendChild(el('tr', {}, [el('td', {}, [el('span', {class: 'sp-code', text: row.file})]),
          el('td', {text: fmtBytes(row.bytes)}), el('td', {class: 'sp-dim', text: row.mtime})]));
      });
      table.appendChild(body);
      historyCard.appendChild(table);
      wrap.appendChild(historyCard);
    }
    return wrap;
  }

  /* ---------- 草稿与保存 ---------- */

  function dirtySections() {
    var dirty = [];
    if (state.permDraft && state.permBase && !same(state.permDraft, state.permBase)) dirty.push('permission');
    if (state.apiDraft && state.apiBase && !same(state.apiDraft, state.apiBase)) dirty.push('api');
    if (state.personaDraft && state.personaBase && !same(state.personaDraft, state.personaBase)) dirty.push('persona');
    return dirty;
  }

  function refreshDirty() {
    var dirty = dirtySections();
    var shellNow = build();
    var bar = shellNow.savebar;
    bar.classList.toggle('show', dirty.length > 0);
    q('[data-ref=hint]', bar).textContent = dirty.length
      ? '未保存：' + dirty.map(function (id) {
        return (SECTIONS.filter(function (item) { return item.id === id; })[0] || {}).label || id;
      }).join('、')
      : '';
    Array.prototype.forEach.call(shellNow.overlay.querySelectorAll('.sp-nav button'), function (button) {
      var id = button.getAttribute('data-section');
      button.classList.toggle('dirty', dirty.indexOf(id) >= 0);
    });
  }

  function discardDraft() {
    var dirty = dirtySections();
    if (!dirty.length) return;
    confirmDialog('放弃这些改动？', el('div', {}, [
      el('p', {text: '还没保存的部分：' + dirty.length + ' 处。放弃后回到磁盘上的值，草稿直接丢掉。'})
    ]), '放弃改动', true).then(function (ok) {
      if (!ok) return;
      state.permDraft = JSON.parse(JSON.stringify(state.permBase));
      state.apiDraft = JSON.parse(JSON.stringify(state.apiBase));
      if (state.personaBase) state.personaDraft = JSON.parse(JSON.stringify(state.personaBase));
      state.note = '';
      render();
      toast('已放弃改动。');
    });
  }

  function saveDraft() {
    if (state.busy) return;
    var dirty = dirtySections();
    if (!dirty.length) return;
    var jobs = [];
    if (dirty.indexOf('permission') >= 0) {
      // 只提交真正改过的开关：后端只接受 {id: bool} 这种形状
      var changed = {};
      Object.keys(state.permDraft.values).forEach(function (ident) {
        if (state.permDraft.values[ident] !== state.permBase.values[ident]) {
          changed[ident] = state.permDraft.values[ident];
        }
      });
      jobs.push(api('/api/owner/settings', 'POST', {section: 'permissions', csrf: state.csrf,
        values: {preset: state.permDraft.preset, restrictions: changed}}));
    }
    if (dirty.indexOf('api') >= 0) {
      jobs.push(api('/api/owner/settings', 'POST', {section: 'api', csrf: state.csrf,
        values: {notes: state.apiDraft.notes}}));
    }
    if (dirty.indexOf('persona') >= 0) {
      jobs.push(api('/api/owner/persona', 'POST', {personas: collectPersonaChanges(), csrf: state.csrf}));
    }
    var shellNow = build();
    var save = q('[data-ref=save]', shellNow.savebar);
    save.disabled = true;
    save.textContent = '保存中…';
    q('[data-ref=err]', shellNow.savebar).textContent = '';
    state.busy = true;
    Promise.all(jobs).then(function (results) {
      var note = '已保存。';
      (results || []).forEach(function (item) {
        if (item && item.applied && item.applied.length) note = '已保存，自主探索开关已写进 config.json（立即生效）。';
        if (item && item.restart_required && item.changed) note = item.message || note;
      });
      toast(note);
      state.busy = false;
      save.disabled = false;
      save.textContent = '保存';
      return load(true);
    }).catch(function (error) {
      state.busy = false;
      save.disabled = false;
      save.textContent = '保存';
      q('[data-ref=err]', shellNow.savebar).textContent = error.message + '（没有写入任何文件，或已回滚）';
      toast('保存失败：' + error.message, true);
    });
  }

  function collectPersonaChanges() {
    var changed = {};
    ['表', '里'].forEach(function (label) {
      if (state.personaDraft[label] !== state.personaBase[label]) changed[label] = state.personaDraft[label];
    });
    return changed;
  }

  function close() {
    if (dirtySections().length) {
      confirmDialog('关掉设置面板？', el('div', {}, [
        el('p', {text: '还有没保存的改动，关掉就丢了（磁盘上的值不受影响）。'})
      ]), '丢弃并关闭', true).then(function (ok) {
        if (ok) hide();
      });
      return;
    }
    hide();
  }

  function hide() {
    shell.overlay.hidden = true;
    shell.savebar.classList.remove('show');
    document.body.style.overflow = '';
    if (location.hash === '#/settings' || location.hash === '#settings') {
      history.replaceState(null, '', location.pathname + location.search);
    }
  }

  function open(section) {
    var shellNow = build();
    // 第一次打开设置：先给新手教程，不直接落进权限页的开关堆里。
    if (!guideSeen()) state.section = 'guide';
    else if (section && SECTIONS.some(function (item) { return item.id === section; })) state.section = section;
    shellNow.overlay.hidden = false;
    document.body.style.overflow = 'hidden';
    return load(false);
  }

  function onNav(event) {
    var button = event.target.closest ? event.target.closest('.sp-nav button[data-section]') : null;
    if (!button) return;
    var next = button.getAttribute('data-section');
    if (next === state.section) return;
    if (state.section === 'guide') markGuideSeen();
    state.section = next;
    state.note = '';
    render();
    build().view.parentNode.scrollTop = 0;
  }

  window.SpaceSettings = {open: open, close: close, reload: function () { return load(true); }};

  document.addEventListener('click', function (event) {
    var opener = event.target.closest ? event.target.closest('[data-settings]') : null;
    if (!opener) return;
    event.preventDefault();
    open(opener.getAttribute('data-settings') || null);
  });

  // 侧栏锚点是 #settings，地址栏里直接敲 #/settings 也要打开；
  // 同一文档内的 hash 变化不会重新执行脚本，所以这里也听 hashchange。
  function isSettingsHash(hash) {
    return hash === '#/settings' || hash === '#settings';
  }

  window.addEventListener('hashchange', function () {
    if (isSettingsHash(location.hash)) {
      if (!shell || shell.overlay.hidden) open(null);
    } else if (shell && !shell.overlay.hidden) {
      hide();
    }
  });

  if (isSettingsHash(location.hash)) open(null);
})();
