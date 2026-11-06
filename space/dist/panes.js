/* Read-only reasoning reader. Uses the existing pane stream; never starts a task or connection. */
(function () {
  'use strict';
  if (window.DSHReasonReader) return;
  const labels = { biao: '表人格', li: '里人格' };
  const records = new Map(Object.keys(labels).map(id => [id, {
    sid: null, seq: 0, blocks: new Map(), revision: 0, connection: 'waiting'
  }]));
  const viewers = new Set();
  let modal, popup, lastPersona = 'biao', scheduled = false;
  let fontSize = 17;
  try { fontSize = Math.max(14, Math.min(24, Number(localStorage.getItem('reasonReaderFont')) || 17)); } catch (_) {}
  function node(doc, tag, text, cls) {
    const n = doc.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function button(doc, text, action, cls) {
    const b = node(doc, 'button', text, cls || 'rr-button');
    b.type = 'button'; b.addEventListener('click', action); return b;
  }
  function queueRender() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; for (const v of viewers) render(v); });
  }
  function render(v) {
    if (v.doc.defaultView.closed) { viewers.delete(v); return; }
    const data = records.get(v.persona);
    v.title.textContent = '小鲸鱼思考 · ' + labels[v.persona];
    for (const [id, tab] of v.tabs) tab.setAttribute('aria-pressed', String(id === v.persona));
    v.status.textContent = ({waiting: '等待思考内容', connected: '已收到思考内容', reconnecting: '观察连接待恢复', closed: '总览已关闭，请重新打开总览后查看实时内容'})[data.connection] || '等待思考内容';
    if (v.sid !== data.sid || v.renderedPersona !== v.persona) {
      v.content.replaceChildren(); v.nodes.clear(); v.follow = true;
      v.sid = data.sid; v.renderedPersona = v.persona; v.revision = -1;
    }
    v.font.textContent = fontSize + ' px';
    v.root.style.setProperty('--rr-font', fontSize + 'px');
    if (v.revision !== data.revision) {
      const oldTop = v.content.scrollTop;
      if (!data.blocks.size) {
        v.content.replaceChildren(node(v.doc, 'p', '当前会话尚无可显示的思考原文。收到模型返回后会在这里显示。', 'rr-empty'));
        v.nodes.clear();
      } else {
        if (!v.nodes.size) v.content.replaceChildren();
        for (const [key, block] of data.blocks) {
          let item = v.nodes.get(key);
          if (!item) {
            const article = node(v.doc, 'section', null, 'rr-block');
            const meta = node(v.doc, 'small', block.label, 'rr-meta');
            const body = node(v.doc, 'div', '', 'rr-body');
            article.append(meta, body); v.content.append(article);
            item = { body, text: null }; v.nodes.set(key, item);
          }
          if (item.text !== block.text) { item.body.textContent = block.text; item.text = block.text; }
        }
      }
      v.content.scrollTop = v.follow ? v.content.scrollHeight : oldTop;
      v.revision = data.revision;
    }
    v.jump.hidden = v.follow;
  }
  function select(v, id) {
    v.persona = id; lastPersona = id; render(v);
  }
  function makeViewer(doc, root, isPopup) {
    root.classList.add('rr-reader');
    const head = node(doc, 'header', null, 'rr-header');
    const title = node(doc, 'h2', '小鲸鱼思考');
    const actions = node(doc, 'div', null, 'rr-actions');
    const v = {doc, root, title, persona: lastPersona, sid: undefined, renderedPersona: null, nodes: new Map(), tabs: new Map(), revision: -1, follow: true};
    const close = button(doc, isPopup ? '关闭窗口' : '收起', () => {
      if (isPopup) doc.defaultView.close();
      else root.close();
    });
    if (!isPopup) actions.append(button(doc, '独立窗口 ↗', () => openPopup(v.persona)));
    if (!isPopup) {
      const full = button(doc, '铺满视图', () => {
        const expanded = root.classList.toggle('rr-maximized');
        full.textContent = expanded ? '还原大小' : '铺满视图';
      });
      actions.append(full);
    }
    actions.append(close); head.append(title, actions);
    const toolbar = node(doc, 'div', null, 'rr-toolbar');
    const tabs = node(doc, 'div', null, 'rr-tabs'); tabs.setAttribute('aria-label', '查看人格');
    for (const id of Object.keys(labels)) {
      const tab = button(doc, labels[id], () => select(v, id));
      tab.setAttribute('aria-pressed', String(id === v.persona)); v.tabs.set(id, tab); tabs.append(tab);
    }
    const fonts = node(doc, 'div', null, 'rr-fonts');
    v.font = node(doc, 'output', fontSize + ' px'); v.font.setAttribute('aria-label', '当前字号');
    function resize(delta) {
      fontSize = Math.max(14, Math.min(24, fontSize + delta));
      try { localStorage.setItem('reasonReaderFont', String(fontSize)); } catch (_) {}
      for (const other of viewers) render(other);
    }
    const minus = button(doc, 'A−', () => resize(-1)); minus.setAttribute('aria-label', '缩小字号');
    const plus = button(doc, 'A＋', () => resize(1)); plus.setAttribute('aria-label', '放大字号');
    fonts.append(minus, v.font, plus); toolbar.append(tabs, fonts);
    const info = node(doc, 'p', '查看当前会话已返回的思考内容。向上翻阅时停留原处，点击“回到最新”继续跟随。', 'rr-info');
    v.status = node(doc, 'p', '', 'rr-status'); v.status.setAttribute('role', 'status');
    v.content = node(doc, 'div', null, 'rr-content'); v.content.tabIndex = 0;
    v.content.setAttribute('aria-label', '思考原文阅读区');
    v.content.addEventListener('scroll', () => {
      v.follow = v.content.scrollHeight - v.content.scrollTop - v.content.clientHeight < 36;
      v.jump.hidden = v.follow;
    }, {passive: true});
    v.jump = button(doc, '回到最新 ↓', () => {
      v.follow = true; v.content.scrollTop = v.content.scrollHeight; v.jump.hidden = true;
    }, 'rr-button rr-jump'); v.jump.hidden = true;
    root.append(head, toolbar, info, v.status, v.content, v.jump);
    viewers.add(v); render(v); return v;
  }
  function open(id) {
    lastPersona = labels[id] ? id : lastPersona;
    if (!modal) {
      const dialog = document.createElement('dialog'); dialog.id = 'reasonReader';
      dialog.setAttribute('aria-label', '小鲸鱼思考放大阅读'); document.body.append(dialog);
      modal = makeViewer(document, dialog, false);
    }
    select(modal, lastPersona);
    if (!modal.root.open) modal.root.showModal();
    modal.content.focus();
  }
  function openPopup(id) {
    lastPersona = labels[id] ? id : lastPersona;
    if (popup && !popup.doc.defaultView.closed) { select(popup, lastPersona); popup.doc.defaultView.focus(); return; }
    const win = window.open('', 'DSHReasonReader-' + Date.now(), 'popup,width=1100,height=820,resizable=yes,scrollbars=yes');
    if (!win) { if (modal) modal.status.textContent = '独立窗口被浏览器拦截，请允许此页面弹窗，或使用“铺满视图”。'; return; }
    const doc = win.document;
    doc.title = '小鲸鱼 · 思考阅读'; doc.documentElement.lang = 'zh-CN';
    const meta = doc.createElement('meta'); meta.name = 'viewport'; meta.content = 'width=device-width,initial-scale=1';
    const sheet = doc.createElement('link'); sheet.rel = 'stylesheet'; sheet.href = new URL('/panes.css', location.href).href;
    doc.head.append(meta, sheet); doc.body.className = 'rr-popup-page';
    const root = node(doc, 'main'); doc.body.append(root); popup = makeViewer(doc, root, true);
    win.addEventListener('pagehide', () => { viewers.delete(popup); popup = null; });
    if (modal?.root.open) modal.root.close();
  }
  function addEntrances() {
    document.querySelectorAll('.dual-pane').forEach((pane, index) => {
      const id = pane.dataset.persona || ['biao', 'li'][index];
      const head = pane.querySelector('.dual-head');
      if (!labels[id] || !head || head.querySelector('.rr-open')) return;
      const b = button(document, '放大思考 ↗', () => open(id), 'dual-btn rr-open');
      b.setAttribute('aria-label', '放大查看' + labels[id] + '思考'); head.append(b);
    });
    const toggle = document.getElementById('reasonToggle');
    if (toggle && !toggle.dataset.readerBound) {
      toggle.dataset.readerBound = '1'; toggle.textContent = '放大查看思考 ↗';
      toggle.addEventListener('click', e => { e.preventDefault(); e.stopImmediatePropagation(); open(lastPersona); }, true);
    }
  }
  window.DSHReasonReader = Object.freeze({
    open,
    session(id, sid) {
      const data = records.get(id); if (!data || data.sid === (sid || null)) return;
      data.sid = sid || null; data.seq = 0; data.blocks.clear(); data.connection = 'waiting'; data.revision++;
      queueRender();
    },
    ingest(id, value) {
      const data = records.get(id);
      if (!data || !data.sid || value.sessionId !== data.sid) return;
      if (value.kind !== 'reasoning.delta' && value.kind !== 'reasoning.snapshot') return;
      // 模型名会随设置变化（如 auto/deepseek-v4.1-flash）；只按会话绑定校验，
      // 不能把非 deepseek-flash 的真实思考流全部丢掉。
      const seq = Number(value.seq); if (!Number.isFinite(seq) || seq <= data.seq) return;
      data.seq = seq;
      const key = (value.attemptId || 'unknown') + ':' + (value.blockIndex ?? 0);
      let block = data.blocks.get(key);
      if (!block) {
        const date = new Date(value.receivedAt || value.time);
        const stamp = Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN') + ' · ';
        block = {text: '', label: stamp + (value.origin === 'native-live' ? '模型原文' : '已记录原文')}; data.blocks.set(key, block);
      }
      const text = typeof value.text === 'string' ? value.text : '';
      block.text = value.kind === 'reasoning.snapshot' ? text : block.text + text;
      data.connection = 'connected'; data.revision++; queueRender();
    },
    connection(id, status) {
      const data = records.get(id); if (!data) return;
      data.connection = status; queueRender();
    }
  });
  window.addEventListener('dsh:thinking-session', e => {
    const d = e.detail; if (d) window.DSHReasonReader.session(d.persona, d.sessionId);
  });
  window.addEventListener('dsh:thinking-event', e => {
    const d = e.detail; if (d?.value) window.DSHReasonReader.ingest(d.persona, d.value);
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addEntrances);
  else addEntrances();
  new MutationObserver(addEntrances).observe(document.body, {childList: true, subtree: true});
  window.addEventListener('pagehide', () => {
    for (const data of records.values()) data.connection = 'closed';
    for (const v of viewers) if (v !== modal) render(v);
  });
})();

/* 表里工作台：两个独立对话框，同时和表人格、里人格说话。
 *
 * 后端契约（本机 127.0.0.1:19991）：
 *   GET  /api/status                         -> csrf / active_persona / parallel_enabled /
 *                                               paused / dispatch_enabled / persona_panes{}
 *   GET  /api/events?persona=&after=          -> SSE {seq,time,kind,persona,payload}
 *   GET  /api/thinking/stream?sessionId=&after= -> SSE {sessionId,seq,kind,text,...}
 *   POST /api/control/{chat,persona,pause,resume}  需要 X-CSRF-Token
 *
 * 只依赖 #dualPanes 容器；不读写 app-v02.js 的全局变量。加载失败时旧版
 * 单对话框保持可见，不会出现空白页。
 */
"use strict";
(function () {
  const host = document.getElementById("dualPanes");
  if (!host) return;

  const PERSONAS = [
    { id: "biao", label: "表人格" },
    { id: "li", label: "里人格" },
  ];
  const TARGETS = [
    ["assistant", "手机优先 · 按需电脑"],
    ["dialogue", "只对话与记忆"],
    ["phone", "她的手机"],
    ["desktop", "专属 Windows"],
  ];
  const STATUS = {
    queued: ["排队中", "warn"],
    claimed: ["执行中", "running"],
    running: ["执行中", "running"],
    uncertain: ["待核对", "warn"],
    "needs-review": ["待你核对", "warn"],
    completed: ["已完成", "ok"],
    reviewed: ["已复核", "ok"],
    stopped: ["已停止", "warn"],
    failed: ["已停止", "warn"],
  };
  const STOP_REASON = {
    AUTONOMY_WINDOW_ENDED: "自主时段结束",
    BUDGET_BLOCKED: "预算检查未通过",
    OWNER_MESSAGE_FIRST: "主人新消息优先",
    HOST_ACTIVE: "主人回来使用电脑",
    OWNER_QUESTION_TIMEOUT: "等回答超时",
    OWNER_QUESTION_ANSWERED: "回答已接手",
    OBSERVATION_UNAVAILABLE: "观察链路不可用",
    STEP_LIMIT: "步数保护",
    REPEATED_ACTION_LIMIT: "重复动作保护",
    CONTEXT_LIMIT: "上下文已满",
    PROVIDER_BALANCE_EMPTY: "账户余额不足",
    CONNECTION_INTERRUPTED: "连接中断",
    RUNTIME_STOPPED: "执行未完成",
    DEVICE_UNCERTAIN: "设备结果不确定",
    CANCELLED: "停止条件触发",
    EMPTY_REPLY: "没有最终回答",
  };

  const state = { csrf: null, status: null, timer: null, booted: false, refreshPromise: null, refreshAgain: false };
  const panes = new Map();

  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  let toastTimer = null;
  function toast(text) {
    let box = document.querySelector(".dual-toast");
    if (!box) {
      box = el("div", "", "dual-toast");
      box.setAttribute("role", "status");
      box.setAttribute("aria-live", "polite");
      document.body.append(box);
    }
    box.textContent = text;
    box.style.display = "block";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.style.display = "none"; }, 6000);
  }

  function stamp(value) {
    const at = new Date(value || Date.now());
    if (Number.isNaN(at.getTime())) return "";
    return at.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function clip(text, limit) {
    const flat = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
    return flat.length > limit ? flat.slice(0, limit) + "…" : flat;
  }

  async function api(path, body) {
    const init = body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf || "" },
      body: JSON.stringify(body),
    };
    const response = await fetch(path, init);
    const value = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(value.error || "操作未完成");
    return value;
  }

  /* ------------------------------------------------------------------ pane */

  function buildPane(spec) {
    const root = el("section", "", "dual-pane");
    root.setAttribute("aria-label", spec.label + "对话框");

    const head = el("div", "", "dual-head");
    const dot = el("i", "", "dual-dot");
    const name = el("strong", spec.label, "dual-name");
    const stateText = el("span", "读取中", "dual-state");
    const spacer = el("span", "", "dual-spacer");
    const meta = el("span", "", "dual-meta");
    const frontButton = el("button", "切到前台", "dual-btn");
    frontButton.type = "button";
    frontButton.title = "只把这一栏切到前台，另一栏的任务继续运行";
    head.append(dot, name, stateText, spacer, meta, frontButton);

    const notice = el("div", "", "dual-notice");
    const noticeText = el("span", "这一轮没发出去，记录留着，不会自动重试。", "dual-notice-text");
    const noticeRetry = el("button", "再发一次", "dual-btn");
    noticeRetry.type = "button";
    noticeRetry.title = "把同一条内容重新发给她（会占用她的额度）";
    notice.append(noticeText, noticeRetry);
    const noticeReview = el("button", "已核对，关闭", "dual-btn");
    noticeReview.type = "button";
    noticeReview.title = "核对回执后关闭这条任务；关闭后不会重发";
    notice.insertBefore(noticeReview, noticeRetry);
    notice.hidden = true;

    const think = el("div", "", "dual-think");
    think.append(el("b", "思考："), document.createTextNode("尚未接通真实思考流"));

    const messages = el("div", "", "dual-messages");
    messages.setAttribute("role", "log");
    messages.setAttribute("aria-live", "polite");
    const empty = el("div", "先留一句话吧。两边各自保存独立会话。", "dual-empty");
    messages.append(empty);

    const jump = el("button", "回到最新 ↓", "dual-jump");
    jump.type = "button";
    jump.hidden = true;

    const compose = el("div", "", "dual-compose");
    const input = document.createElement("textarea");
    input.rows = 2;
    input.maxLength = 24000;
    input.placeholder = "想对" + spec.label + "说的话、一起做的事…";
    input.setAttribute("aria-label", "给" + spec.label + "的留言");
    const tools = el("div", "", "dual-tools");
    const target = document.createElement("select");
    target.setAttribute("aria-label", spec.label + "的任务执行端");
    for (const pair of TARGETS) {
      const option = el("option", pair[1]);
      option.value = pair[0];
      target.append(option);
    }
    const hint = el("span", "每条新任务使用新上下文", "dual-hint");
    const send = el("button", "发送 ↑", "dual-btn dual-primary");
    send.type = "button";
    send.title = "发送给" + spec.label;
    tools.append(target, hint, send);
    compose.append(input, tools);

    root.append(head, notice, think, messages, jump, compose);
    host.append(root);

    const pane = {
      id: spec.id, label: spec.label, root: root, dot: dot, state: stateText, meta: meta,
      front: frontButton, notice: notice, noticeText: noticeText, noticeReview: noticeReview,
      noticeRetry: noticeRetry,
      latest: null, think: think, messages: messages, empty: empty,
      jump: jump, input: input, target: target, send: send, hint: hint,
      lastSeq: 0, thinkSeq: 0, thinkText: "", source: null, thinkSource: null,
      sid: null, bindingSeq: 0, autoScroll: true, sending: false, count: 0,
    };

    messages.addEventListener("scroll", function () {
      const gap = messages.scrollHeight - messages.scrollTop - messages.clientHeight;
      pane.autoScroll = gap < 48;
      jump.hidden = pane.autoScroll;
    });
    jump.addEventListener("click", function () {
      pane.autoScroll = true;
      messages.scrollTop = messages.scrollHeight;
      jump.hidden = true;
    });
    frontButton.addEventListener("click", function () { switchFront(pane); });
    noticeReview.addEventListener("click", function () { reviewLatest(pane); });
    noticeRetry.addEventListener("click", function () { retryLatest(pane); });
    send.addEventListener("click", function () { sendText(pane); });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendText(pane);
      }
    });
    return pane;
  }

  function pushBubble(pane, role, text, note, failed) {
    if (pane.empty && pane.empty.parentNode) pane.empty.remove();
    const bubble = el("div", "", "dual-msg " + (role === "me" ? "dual-me" : "dual-her") + (failed ? " dual-failed" : ""));
    bubble.append(el("span", text));
    if (note) bubble.append(el("small", note));
    pane.messages.append(bubble);
    pane.count += 1;
    while (pane.messages.children.length > 200) pane.messages.firstElementChild.remove();
    if (pane.autoScroll) pane.messages.scrollTop = pane.messages.scrollHeight;
  }

  /* ----------------------------------------------------------------- events */

  function onEvent(pane, event) {
    if (!event || event.persona !== pane.id) return;
    const seq = Number(event.seq) || 0;
    if (seq <= pane.lastSeq) return;
    pane.lastSeq = seq;
    window.dispatchEvent(new CustomEvent("dsh:space-event", { detail: event }));
    const payload = event.payload || {};
    if (event.kind === "context.continued" && payload.new_session_id && seq > pane.bindingSeq) {
      pane.bindingSeq = seq;
      if (pane.sid !== payload.new_session_id) {
        pane.sid = payload.new_session_id;
        connectThinking(pane);
      }
      refresh();
    }
    if (["owner.message", "task.dispatch", "task.accepted", "task.result", "control.hold"].includes(event.kind)) refresh();
    const when = stamp(event.time);

    if (event.kind === "owner.message") {
      pushBubble(pane, "me", payload.text || "", when);
      return;
    }
    if (event.kind === "task.result") {
      const status = payload.status || "";
      const note = (when + " · " + (STATUS[status] ? STATUS[status][0] : status || "回执"));
      const body = payload.text || (payload.failure && payload.failure.message) || "本轮没有最终回复，执行回执已保留。";
      pushBubble(pane, "her", body, note, status !== "completed");
      return;
    }
    if (event.kind === "task.failure") {
      const message = (payload.failure && payload.failure.message) || payload.message || "本轮已停止。";
      pushBubble(pane, "her", message, when + " · 本轮已停止 · 旧任务不会自动重发", true);
      return;
    }
    if (event.kind === "reasoning.delta" || event.kind === "reply.delta") {
      if (payload.text) {
        pane.thinkText = (pane.thinkText + payload.text).slice(-600);
        renderThink(pane);
      }
    }
  }

  function renderThink(pane) {
    pane.think.replaceChildren();
    pane.think.append(el("b", "思考："), document.createTextNode(pane.thinkText ? "…" + clip(pane.thinkText, 150) : "尚未接通真实思考流"));
  }

  function connectEvents(pane, after) {
    pane.lastSeq = Math.max(0, after || 0);
    if (pane.source) pane.source.close();
    const source = new EventSource("/api/events?persona=" + encodeURIComponent(pane.id) + "&after=" + pane.lastSeq);
    source.onopen = function () { pane.dot.title = "事件流已连接"; window.dispatchEvent(new CustomEvent("dsh:stream-open", { detail: { persona: pane.id } })); };
    source.onmessage = function (message) {
      try { onEvent(pane, JSON.parse(message.data)); } catch (error) { /* 单条坏事件不打断流 */ }
    };
    source.onerror = function () { pane.dot.title = "事件流重连中"; };
    pane.source = source;
  }

  function connectThinking(pane) {
    if (pane.thinkSource) pane.thinkSource.close();
    pane.thinkSource = null;
    window.dispatchEvent(new CustomEvent("dsh:thinking-session", { detail: { persona: pane.id, sessionId: pane.sid } }));
    if (!pane.sid) {
      pane.thinkText = "";
      renderThink(pane);
      return;
    }
    pane.thinkSeq = 0;
    pane.thinkText = "";
    renderThink(pane);
    const sid = pane.sid;
    const source = new EventSource("/api/thinking/stream?sessionId=" + encodeURIComponent(sid) + "&after=0");
    const handle = function (message) {
      try {
        const value = JSON.parse(message.data);
        if (pane.thinkSource !== source || sid !== pane.sid || value.sessionId !== sid) return;
        const seq = Number(value.seq) || 0;
        if (seq <= pane.thinkSeq) return;
        pane.thinkSeq = seq;
        window.dispatchEvent(new CustomEvent("dsh:thinking-event", { detail: { persona: pane.id, sessionId: sid, value: value } }));
        if (value.kind === "reasoning.snapshot") pane.thinkText = String(value.text || "").slice(-600);
        else if (value.kind === "reasoning.delta") pane.thinkText = (pane.thinkText + String(value.text || "")).slice(-600);
        else return;
        renderThink(pane);
      } catch (error) { /* 忽略坏帧 */ }
    };
    source.onmessage = handle;
    source.addEventListener("reasoning", handle);
    source.onerror = function () {
      if (pane.thinkSource !== source || pane.sid !== sid) return;
      pane.think.replaceChildren(el("b", "思考："), document.createTextNode("观察连接待恢复（原文不补写）"));
    };
    pane.thinkSource = source;
  }

  /* ---------------------------------------------------------------- actions */

  async function sendText(pane) {
    const text = pane.input.value.trim();
    if (!text || pane.sending) return;
    if (state.status && !state.status.parallel_enabled && state.status.active_persona !== pane.id) {
      toast("表里并发未开启：目前只能给前台人格发消息。");
      return;
    }
    pane.sending = true;
    pane.send.disabled = true;
    pane.hint.textContent = "正在发送…";
    const key = "pane-" + pane.id + "-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
    try {
      await api("/api/control/chat", { text: text, request_key: key, target: pane.target.value, persona: pane.id });
      pane.input.value = "";
      pane.hint.textContent = "已保存 · 回执见下方";
    } catch (error) {
      toast(pane.label + "：" + error.message);
    } finally {
      pane.sending = false;
      pane.send.disabled = false;
      refresh();
    }
  }

  /**
   * 上一轮没发出去（needs-review）时，主人手上原来只有一句"不会自动重发"，没有出路。
   * 这里把同一条原文原样再发一次——新请求键、不覆盖旧记录，旧任务仍在记录里。
   */
  async function retryLatest(pane) {
    const task = pane.latest;
    if (!task || !task.text) { toast(pane.label + "：这一轮没有可重发的内容。"); return; }
    if (pane.sending) return;
    if (state.status && !state.status.parallel_enabled && state.status.active_persona !== pane.id) {
      toast("表里并发未开启：先切到" + pane.label + "为前台，才能重发。");
      return;
    }
    pane.sending = true;
    pane.noticeRetry.disabled = true;
    pane.noticeText.textContent = "正在重新发出…";
    const key = "retry-" + pane.id + "-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
    try {
      await api("/api/control/chat", {
        text: task.text, request_key: key, target: task.target || "assistant", persona: pane.id
      });
      pushBubble(pane, "me", task.text, "重新发出 · 等她的回执");
      toast(pane.label + "：同一条内容已重新发出，回执会出现在这一栏下面。");
    } catch (error) {
      toast(pane.label + "：" + error.message);
    } finally {
      pane.sending = false;
      pane.noticeRetry.disabled = false;
      refresh();
    }
  }

  /** 待核对/待复核的出口：先核对设备回执，再明确关闭；关闭后不会重发。 */
  async function reviewLatest(pane) {
    const task = pane.latest;
    if (!task) return;
    pane.noticeReview.disabled = true;
    try {
      if (task.status === "uncertain") {
        const result = await api("/api/control/reconcile", { id: task.id, persona: pane.id });
        toast(pane.label + "：" + (result.message || "已核对回执，状态更新为待复核。"));
      } else {
        await api("/api/control/review_task", { id: task.id, persona: pane.id });
        toast(pane.label + "：已标记复核并关闭，这条不会重发。");
      }
    } catch (error) {
      toast(pane.label + "：" + error.message);
    } finally {
      pane.noticeReview.disabled = false;
      refresh();
    }
  }

  async function switchFront(pane) {
    try {
      await api("/api/control/persona", { persona: pane.id });
      toast("已切到" + pane.label + "为前台，另一栏继续运行。");
      refresh();
    } catch (error) {
      toast(error.message);
    }
  }

  async function control(action) {
    const persona = state.status ? state.status.active_persona : "biao";
    try {
      await api("/api/control/" + action, { persona: persona });
      refresh();
    } catch (error) {
      toast(error.message);
    }
  }

  /* ------------------------------------------------------------------- boot */

  const topbar = el("div", "", "dual-topbar");
  const topDot = el("i", "", "dual-todot");
  const topText = el("span", "正在读取表里状态…", "dual-topnote");
  const topSpacer = el("span", "", "dual-spacer");
  const pauseButton = el("button", "暂停两边", "dual-btn");
  pauseButton.type = "button";
  const resumeButton = el("button", "允许继续", "dual-btn dual-primary");
  resumeButton.type = "button";
  const refreshButton = el("button", "刷新 ↻", "dual-btn");
  refreshButton.type = "button";
  topbar.append(topDot, topText, topSpacer, pauseButton, resumeButton, refreshButton);
  host.append(topbar);

  pauseButton.addEventListener("click", function () { control("pause"); });
  resumeButton.addEventListener("click", function () { control("resume"); });
  refreshButton.addEventListener("click", function () { refresh(); });
  host.style.gridTemplateRows = "auto minmax(0,1fr)";

  for (const spec of PERSONAS) panes.set(spec.id, buildPane(spec));

  function renderTopbar(status) {
    const parallel = Boolean(status.parallel_enabled);
    topDot.className = "dual-todot" + (parallel ? " dual-on" : "");
    const parts = [];
    parts.push("表里并发 · " + (parallel ? "已开启" : "未开启"));
    parts.push(status.paused ? "活动已暂停" : "活动可执行");
    parts.push("前台：" + (status.active_persona === "li" ? "里人格" : "表人格"));
    if (!status.dispatch_enabled) parts.push("任务只保存，不派发");
    topText.textContent = parts.join(" · ");
    pauseButton.disabled = Boolean(status.paused);
    resumeButton.disabled = !status.paused;
  }

  function renderPaneHeader(pane, status) {
    const info = (status.persona_panes && status.persona_panes[pane.id]) || {};
    const latest = info.latest_task || null;
    const known = latest && STATUS[latest.status] ? STATUS[latest.status] : null;
    const failure = latest && latest.failure ? latest.failure : null;
    const shortReason = failure && STOP_REASON[failure.code] ? STOP_REASON[failure.code] : "";
    pane.dot.className = "dual-dot" + (known ? " dual-" + known[1] : "");
    pane.state.textContent = known ? known[0] + (shortReason ? " · " + shortReason : "") : "空闲";
    pane.state.title = failure ? String(failure.message || shortReason || "") : "";
    const pending = Number(info.pending) || 0;
    pane.meta.textContent = (pending ? pending + " 项未完成 · " : "") + "会话 " + String(info.session_id || "待登记").slice(-6);
    const parallel = Boolean(status.parallel_enabled);
    const isFront = status.active_persona === pane.id;
    pane.front.classList.toggle("dual-front", isFront);
    pane.front.textContent = isFront ? "当前前台" : "切到前台";
    pane.front.disabled = isFront;
    pane.root.classList.toggle("dual-front", isFront);
    const waiting = latest && (latest.status === "needs-review" || latest.status === "uncertain");
    pane.notice.hidden = !waiting;
    if (waiting) {
      pane.latest = latest;
      const uncertain = latest.status === "uncertain";
      const failure = latest.failure && latest.failure.message;
      if (uncertain) {
        pane.noticeText.textContent = "这一轮有设备动作结果待核对，先核对回执再关闭；不会自动重发。";
      } else {
        const full = String(latest.text || "").replace(/\s+/g, " ").trim();
        const preview = full.length > 40 ? full.slice(0, 40) + "…" : full;
        pane.noticeText.textContent = failure
          ? "停止原因：" + failure
          : preview ? "「" + preview + "」这一轮没发出去，记录还留着。"
          : "上一轮没发出去，记录还留着。";
      }
      pane.noticeReview.textContent = uncertain ? "核对回执" : "已核对，关闭";
      pane.noticeReview.hidden = false;
      pane.noticeRetry.hidden = uncertain;
      pane.noticeRetry.disabled = !(parallel || isFront) || pane.sending;
      pane.noticeRetry.title = (parallel || isFront)
        ? "把同一条内容重新发给" + pane.label + "（会用她今天的额度）"
        : "并发没开：先点右上角切到" + pane.label + "为前台，再重发";
    } else {
      pane.latest = null;
    }
    const allowed = parallel || isFront;
    pane.input.disabled = !allowed;
    pane.send.disabled = !allowed || pane.sending;
    pane.target.disabled = !allowed;
    pane.hint.textContent = pane.sending ? "正在发送…" : allowed ? "每条新任务使用新上下文" : "并发未开启：只能给前台人格发消息";
    if (status.parallel_enabled) pane.notice.hidden = !waiting;
    if ((Number(status.latest_seq) || 0) >= pane.bindingSeq) {
      pane.bindingSeq = Number(status.latest_seq) || 0;
      if (pane.sid !== (info.session_id || null)) {
        pane.sid = info.session_id || null;
        connectThinking(pane);
      }
    }
  }

  function refresh() {
    if (state.refreshPromise) { state.refreshAgain = true; return state.refreshPromise; }
    state.refreshPromise = refreshOnce().finally(function () {
      state.refreshPromise = null;
      if (state.refreshAgain) { state.refreshAgain = false; refresh(); }
    });
    return state.refreshPromise;
  }

  async function refreshOnce() {
    let status;
    try {
      status = await api("/api/status");
    } catch (error) {
      topText.textContent = "控制台暂时断开：" + error.message;
      topDot.className = "dual-todot";
      return;
    }
    state.status = status;
    state.csrf = status.csrf;
    renderTopbar(status);
    for (const pane of panes.values()) renderPaneHeader(pane, status);
    if (!state.booted) {
      state.booted = true;
      window.dshDualStreamsReady = true;
      window.dispatchEvent(new CustomEvent("dsh:dual-ready"));
      const recent = Math.max(0, (Number(status.latest_seq) || 0) - 120);
      for (const pane of panes.values()) {
        const info = (status.persona_panes && status.persona_panes[pane.id]) || {};
        pane.sid = info.session_id || null;
        connectEvents(pane, recent);
        // renderPaneHeader already connected this session; do not reopen it.
      }
      document.body.dataset.dual = "on";
    }
  }

  refresh();
  state.timer = setInterval(refresh, 5000);
  window.addEventListener("beforeunload", function () {
    clearInterval(state.timer);
    for (const pane of panes.values()) {
      if (pane.source) pane.source.close();
      if (pane.thinkSource) pane.thinkSource.close();
    }
  });
})();
