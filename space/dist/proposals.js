/* 方案制运作：展示方案、实施/不实施/修改意见、停滞放行或终止。 */
(function () {
  'use strict';
  var STATUS = {
    draft: '草稿中', pending_review: '待你决定', revising: '待修订', approved: '已批准待执行',
    executing: '执行中', completed: '已完成', rejected: '已选择不实施', stalled: '执行停滞待处理'
  };
  var PERSONA = { biao: '表人格', li: '里人格' };
  var TARGET = { assistant: '按需选用设备', dialogue: '只对话与记忆', phone: '她的手机', desktop: '专属 Windows' };
  var csrf = '';
  var proposals = [];
  var busy = false;
  var loaded = false;

  function byId(id) { return document.getElementById(id); }
  function node(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function esc(text) { return String(text == null ? '' : text); }
  function when(value) {
    var at = new Date(value || 0);
    if (Number.isNaN(at.getTime())) return '—';
    return at.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  }
  function toast(msg) { if (typeof window.toast === 'function') window.toast(msg); else alert(msg); }

  async function request(path, options) {
    var r = await fetch(path, options || {});
    var text = await r.text();
    var data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
    if (!r.ok) throw new Error(data.error || data.code || ('HTTP ' + r.status));
    return data;
  }
  async function post(body) {
    return request('/api/proposals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify(body)
    });
  }

  function pendingCount() {
    var n = 0;
    for (var i = 0; i < proposals.length; i++) {
      if (['pending_review', 'revising', 'stalled'].indexOf(proposals[i].status) >= 0) n++;
    }
    return n;
  }

  function section(title, body) {
    var wrap = node('div', 'proposal-section');
    wrap.append(node('strong', '', title));
    var pre = node('pre', 'proposal-text', esc(body));
    pre.style.whiteSpace = 'pre-wrap';
    pre.style.margin = '6px 0 0';
    pre.style.font = 'inherit';
    wrap.append(pre);
    return wrap;
  }

  async function act(action, id, extra) {
    if (busy) return;
    busy = true;
    try {
      await post(Object.assign({ action: action, id: id }, extra || {}));
      busy = false;
      await load();
      if (typeof window.refresh === 'function') window.refresh();
      toast(action === 'approve' ? '已同意实施。' : action === 'reject' ? '已选择不实施。' : action === 'release' ? '已手动放行继续。' : action === 'terminate' ? '已终止该方案。' : '修改意见已提交，等待修订。');
    } catch (e) {
      busy = false;
      toast('操作失败：' + e.message);
    }
  }

  function card(p) {
    var root = node('article', 'proposal-card approval');
    var head = node('div', 'approval-heading goal-head');
    head.append(node('h3', '', (p.title || '未命名方案') + '（v' + (p.version || 1) + '）'));
    var badge = node('span', 'pill goal-badge goal-badge-' + p.status, STATUS[p.status] || p.status);
    head.append(badge);
    root.append(head);

    var meta = [PERSONA[p.persona] || p.persona, TARGET[p.target] || p.target, '更新 ' + when(p.updated_at)].filter(Boolean).join(' · ');
    root.append(node('p', 'muted goal-meta', meta));
    if (p.mail_sent_at) root.append(node('p', 'muted', '邮件已发送：' + when(p.mail_sent_at)));
    if (p.mail_error) root.append(node('p', 'muted', '邮件发送失败：' + p.mail_error + '（方案已保存，可在控制台决定）'));

    root.append(section('为什么', p.why));
    root.append(section('怎么做', p.how));
    root.append(section('有什么用', p.benefit));
    if (p.owner_note) root.append(section('主人修改意见 / 备注', p.owner_note));

    if (p.status === 'stalled') {
      root.append(node('p', 'muted', '连续失败/停滞 ' + (p.failure_streak || 0) + ' 次（阈值 ' + (p.stall_threshold || 3) + '）。系统已暂停自动推进，请手动放行或终止。'));
    }
    if (p.last_task) {
      var task = p.last_task;
      var line = '关联任务：' + (task.status || '—') + ' · ' + when(task.time);
      if (task.failure && task.failure.message) line += ' · ' + task.failure.message;
      root.append(node('p', 'muted', line));
    }

    var actions = node('div', 'approval-actions goal-actions');
    if (['pending_review', 'revising', 'stalled', 'approved', 'executing'].indexOf(p.status) >= 0) {
      if (p.status !== 'approved' && p.status !== 'executing') {
        actions.append(btn('实施', function () { act('approve', p.id); }, 'primary goal-primary'));
      }
      actions.append(btn('不实施', function () { if (confirm('确定不实施该方案？')) act('reject', p.id); }));
      if (p.status === 'stalled') {
        actions.append(btn('手动放行继续', function () { act('release', p.id); }, 'primary'));
        actions.append(btn('终止方案', function () { if (confirm('确定终止该方案？')) act('terminate', p.id); }));
      }
      var reviseWrap = node('div', 'proposal-revise', '');
      reviseWrap.style.marginTop = '8px';
      reviseWrap.style.width = '100%';
      var ta = document.createElement('textarea');
      ta.className = 'goal-edit-text';
      ta.rows = 3;
      ta.placeholder = '填写修改意见（将发送给空间分析并修订方案，再邮件发给你）';
      ta.style.width = '100%';
      ta.style.boxSizing = 'border-box';
      var submitRev = btn('提交修改意见', function () {
        var note = (ta.value || '').trim();
        if (!note) { toast('请先填写修改意见。'); return; }
        act('revise', p.id, { note: note });
      });
      reviseWrap.append(ta, submitRev);
      root.append(actions, reviseWrap);
      return root;
    }
    if (!actions.childNodes.length) actions.append(node('span', 'muted', '该方案已结束。'));
    root.append(actions);
    return root;
  }

  function btn(label, handler, cls) {
    var b = node('button', cls || '', label);
    b.type = 'button';
    b.onclick = handler;
    return b;
  }

  function render() {
    var box = byId('proposalList');
    if (!box) return;
    box.textContent = '';
    if (!proposals.length) {
      box.append(node('p', 'empty', '目前没有方案。开启自主探索后，空间会先制定方案并邮件通知你。'));
    } else {
      for (var i = 0; i < proposals.length; i++) box.append(card(proposals[i]));
    }
    // 触发徽标更新（与审批一起计数）
    try {
      var pendingApprovals = (window.state && window.state.approvals) ? window.state.approvals.filter(function(x){return x.status==='pending';}).length : 0;
      var rc = byId('requestCount');
      if (rc) rc.textContent = pendingApprovals + pendingCount();
    } catch (e) {}
  }

  async function load() {
    try {
      var data = await request('/api/proposals');
      csrf = data.csrf || csrf;
      proposals = data.proposals || [];
      loaded = true;
      render();
    } catch (e) {
      var box = byId('proposalList');
      if (box && !loaded) {
        box.textContent = '';
        box.append(node('p', 'empty', '方案读取失败：' + e.message));
      }
    }
  }

  window.DshProposals = { load: load, activate: load, pendingCount: pendingCount, proposals: function(){return proposals;} };
})();
