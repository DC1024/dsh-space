/* 我的目标页：新增、编辑、暂停、完成、排序、交办与查看回执。 */
(function () {
  'use strict';

  var STATUS = { active: '进行中', paused: '已暂停', done: '已完成' };
  var TASK_STATUS = { queued: '排队中', claimed: '执行中', running: '执行中', completed: '已完成', reviewed: '已复核',
    stopped: '已停止', 'needs-review': '待核对', uncertain: '待核对' };
  var PERSONA = { biao: '表人格', li: '里人格', both: '表 + 里共同' };
  var TARGET = { assistant: '按需选用设备', dialogue: '只对话与记忆', phone: '她的手机', desktop: '专属 Windows' };
  var csrf = '';
  var goals = [];
  var editing = null;
  var openReceipt = null;
  var busy = false;
  var loaded = false;
  var bound = false;

  function byId(id) { return document.getElementById(id); }

  function node(tag, cls, text) {
    var item = document.createElement(tag);
    if (cls) item.className = cls;
    if (text !== undefined && text !== null) item.textContent = text;
    return item;
  }

  function message(text, bad) {
    var box = byId('goalMessage');
    if (!box) return;
    box.textContent = text || '';
    box.className = 'goal-message' + (bad ? ' bad' : '');
  }

  function when(value) {
    var at = new Date(value || 0);
    if (Number.isNaN(at.getTime())) return '—';
    return at.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  }

  async function request(path, options) {
    var response = await fetch(path, options || {});
    var text = await response.text();
    var data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
    if (!response.ok) throw new Error(data.error || data.code || ('HTTP ' + response.status));
    return data;
  }

  async function post(body) {
    return request('/api/goals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify(body)
    });
  }

  async function load() {
    if (busy) return;
    busy = true;
    try {
      var data = await request('/api/goals');
      csrf = data.csrf || csrf;
      goals = data.goals || [];
      loaded = true;
      render();
    } catch (error) {
      message('目标读取失败：' + error.message, true);
    } finally {
      busy = false;
    }
    if (!loaded) {
      var list = byId('goalList');
      if (list && !list.childNodes.length) list.append(node('div', 'goal-empty', '读取中…'));
    }
  }

  function button(label, handler, cls) {
    var item = node('button', cls || '', label);
    item.type = 'button';
    item.onclick = function () { handler(item); };
    return item;
  }

  function run(action, payload) {
    if (busy) return;
    busy = true;
    post(Object.assign({ action: action }, payload || {}))
      .then(function (result) {
        if (action === 'dispatch') {
          var status = (result.task || {}).status;
          message(status === 'queued' ? '已交办，任务已排队。'
            : status === 'needs-review' ? '已交办，但活动暂停或未派发，任务已保存待处理。'
            : '已交办。');
        }
        editing = null;
        busy = false;
        return load();
      })
      .catch(function (error) { message('操作失败：' + error.message, true); busy = false; });
  }

  function render() {
    var list = byId('goalList');
    if (!list) return;
    list.textContent = '';
    if (!goals.length) {
      list.append(node('div', 'goal-empty', '还没有添加目标。添加后，自主轮会按顺序继续。'));
      return;
    }
    goals.forEach(function (goal, index) { list.append(card(goal, index, goals.length)); });
  }

  function card(goal, index, total) {
    var root = node('article', 'goal-card' + (goal.status === 'done' ? ' goal-card-done' : ''));
    var head = node('div', 'goal-head');
    head.append(node('span', 'goal-badge goal-badge-' + goal.status, STATUS[goal.status] || goal.status));
    var title = node('div', 'goal-title');
    head.append(title);
    root.append(head);

    if (editing === goal.id) {
      var area = node('textarea', 'goal-edit-text');
      area.rows = 4;
      area.maxLength = 12000;
      area.value = goal.text;
      var row = node('div', 'goal-edit-row');
      var persona = node('select', '');
      [['biao', '表人格'], ['li', '里人格'], ['both', '表 + 里共同']].forEach(function (pair) {
        var option = node('option', '', pair[1]);
        option.value = pair[0];
        persona.append(option);
      });
      persona.value = goal.persona;
      var target = node('select', '');
      Object.keys(TARGET).forEach(function (key) {
        var option = node('option', '', TARGET[key]);
        option.value = key;
        target.append(option);
      });
      target.value = goal.target;
      row.append(node('label', 'goal-mini', '人格'), persona, node('label', 'goal-mini', '执行端'), target);
      var tools = node('div', 'goal-actions');
      tools.append(button('保存', function () {
        run('edit', { id: goal.id, text: area.value, persona: persona.value, target: target.value });
      }, 'goal-primary'));
      tools.append(button('取消', function () { editing = null; render(); }));
      root.append(area, row, tools);
      return root;
    }

    title.append(node('p', 'goal-text', goal.text));
    root.append(node('div', 'goal-meta',
      (PERSONA[goal.persona] || goal.persona) + ' · ' + (TARGET[goal.target] || goal.target)
      + ' · 更新 ' + when(goal.updated_at)));

    var actions = node('div', 'goal-actions');
    if (goal.status !== 'done') {
      actions.append(button('交办', function () { run('dispatch', { id: goal.id }); }, 'goal-primary'));
    }
    actions.append(button('编辑', function () { editing = goal.id; openReceipt = null; render(); }));
    if (goal.status === 'active') {
      actions.append(button('暂停', function () { run('status', { id: goal.id, status: 'paused' }); }));
      actions.append(button('完成', function () { run('status', { id: goal.id, status: 'done' }); }));
    } else {
      actions.append(button(goal.status === 'done' ? '重新启用' : '启用', function () {
        run('status', { id: goal.id, status: 'active' });
      }));
      if (goal.status === 'paused') {
        actions.append(button('完成', function () { run('status', { id: goal.id, status: 'done' }); }));
      }
    }
    var up = button('↑', function () { move(goal.id, -1); }, 'goal-move');
    var down = button('↓', function () { move(goal.id, 1); }, 'goal-move');
    up.disabled = index === 0;
    down.disabled = index === total - 1;
    actions.append(up, down);
    if (goal.last_task) {
      actions.append(button(openReceipt === goal.id ? '收起回执' : '查看回执', function () {
        openReceipt = openReceipt === goal.id ? null : goal.id;
        render();
      }));
    }
    root.append(actions);

    if (goal.last_task) {
      root.append(node('div', 'goal-last',
        '最近任务：' + (TASK_STATUS[goal.last_task.status] || goal.last_task.status || '—')
        + ' · ' + when(goal.last_task.time)));
      if (openReceipt === goal.id) {
        var receipt = node('div', 'goal-receipt');
        var failure = goal.last_task.failure || {};
        if (failure.message) receipt.append(node('p', 'goal-failure', '停止原因：' + failure.message));
        receipt.append(node('pre', 'goal-receipt-text',
          goal.last_task.text || (failure.message ? '' : '这条任务还没有最终回执。')));
        root.append(receipt);
      }
    }
    return root;
  }

  function move(goalId, delta) {
    var index = goals.findIndex(function (item) { return item.id === goalId; });
    var next = index + delta;
    if (index < 0 || next < 0 || next >= goals.length) return;
    var ids = goals.map(function (item) { return item.id; });
    ids.splice(index, 1);
    ids.splice(next, 0, goalId);
    run('reorder', { ids: ids });
  }

  function bind() {
    if (bound) return;
    var form = byId('goalForm');
    if (!form) return;
    bound = true;
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      if (busy) return;
      var text = String(byId('goalText').value || '').trim();
      if (!text) { message('目标内容不能为空。', true); return; }
      busy = true;
      post({ action: 'add', text: text, persona: byId('goalPersona').value, target: byId('goalTarget').value })
        .then(function () { byId('goalText').value = ''; message('目标已添加。'); busy = false; return load(); })
        .catch(function (error) { message('添加失败：' + error.message, true); busy = false; });
    });
  }

  window.DshGoals = { activate: function () { bind(); message(''); return load(); } };
})();
