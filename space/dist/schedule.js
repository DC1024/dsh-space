/* 定时任务页：配置汇报邮箱与时段，查看最近执行记录。只读本空间记录，不触发任务。 */
(function () {
  'use strict';
  var built = false;
  var csrf = '';
  var data = null;
  var section, mailForm, taskForm, runList, badge, result;

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  async function token() {
    if (csrf) return csrf;
    try {
      var response = await fetch('/api/status');
      var value = await response.json();
      csrf = value.csrf || '';
    } catch (error) { csrf = ''; }
    return csrf;
  }

  async function post(path, body) {
    var at = await token();
    var response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': at},
      body: JSON.stringify(body || {})
    });
    var value = await response.json().catch(function () { return {}; });
    if (!response.ok) throw Error(value.error || '操作未完成');
    return value;
  }

  function stamp(iso) {
    var at = new Date(iso);
    if (isNaN(at.getTime())) return iso || '';
    return at.toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
  }

  function field(label, input) {
    var wrap = el('label');
    wrap.append(el('span', label));
    wrap.append(input);
    return wrap;
  }

  function build() {
    if (built || document.getElementById('schedule')) { built = true; return; }
    section = el('section', '', 'panel');
    section.id = 'schedule';

    var head = el('div', '', 'page-head');
    var titleBox = el('div');
    titleBox.append(el('span', 'PERIOD REPORT', 'eyebrow'));
    titleBox.append(el('h2', '定时任务 · 时段汇总'));
    titleBox.append(el('p', '每个时段结束后，把这段时间的任务、费用、记忆和关键事件汇总成一封邮件发到你的邮箱。只读取本空间记录，不会触发新任务，也不含设备截图。', 'page-note'));
    badge = el('span', '读取中', 'pill');
    head.append(titleBox, badge);
    section.append(head);

    mailForm = el('form', '', 'memory-form');
    mailForm.id = 'schMailForm';
    mailForm.autocomplete = 'off';
    var grid = el('div', '', 'sch-grid');
    var host = el('input'); host.id = 'schHost'; host.placeholder = 'smtp.qq.com'; host.required = true;
    var port = el('input'); port.id = 'schPort'; port.type = 'number'; port.min = '1'; port.max = '65535'; port.placeholder = '465';
    var user = el('input'); user.id = 'schUser'; user.type = 'email'; user.placeholder = '你用来发信的邮箱'; user.required = true;
    var pass = el('input'); pass.id = 'schPass'; pass.type = 'password'; pass.placeholder = 'SMTP 授权码（不是登录密码）';
    var to = el('input'); to.id = 'schTo'; to.type = 'email'; to.placeholder = '收件邮箱'; to.required = true;
    var fromName = el('input'); fromName.id = 'schFromName'; fromName.placeholder = '小鲸鱼'; fromName.maxLength = 40;
    grid.append(field('SMTP 服务器', host), field('端口', port), field('发信账号', user),
                field('授权码', pass), field('收件邮箱', to), field('发件人显示名', fromName));
    mailForm.append(grid);

    var sslWrap = el('label', '', 'sch-check');
    var ssl = el('input'); ssl.id = 'schSsl'; ssl.type = 'checkbox';
    sslWrap.append(ssl, el('span', 'SSL 直连（QQ、163 等常用 465；取消勾选将尝试 STARTTLS）'));
    mailForm.append(sslWrap);

    taskForm = el('form', '', 'memory-form');
    taskForm.id = 'schTaskForm';
    taskForm.append(el('strong', '时段汇总任务', ''));
    var enableWrap = el('label', '', 'sch-check');
    var enabled = el('input'); enabled.id = 'schEnabled'; enabled.type = 'checkbox';
    enableWrap.append(enabled, el('span', '启用：每个时段结束后自动发送'));
    taskForm.append(enableWrap);
    taskForm.append(el('span', '时段（结束点触发；跨夜时段如 21:00–04:00 也支持）', 'sch-note'));
    var segments = el('div', '', 'sch-segments');
    segments.id = 'schSegments';
    taskForm.append(segments);
    var addSegment = el('button', '＋ 添加时段');
    addSegment.type = 'button';
    addSegment.className = 'drop';
    addSegment.addEventListener('click', function () { addSegmentRow('06:00', '12:00'); });
    taskForm.append(addSegment);

    var actions = el('div', '', 'sch-actions');
    var save = el('button', '保存配置与时段');
    save.type = 'button'; save.className = 'primary';
    var test = el('button', '发送测试邮件');
    test.type = 'button';
    var once = el('button', '立即汇总最近 12 小时并发送');
    once.type = 'button';
    actions.append(save, test, once);
    section.append(mailForm, taskForm, actions);

    result = el('div', '', 'sch-note');
    result.id = 'schResult';
    section.append(result);

    section.append(el('h3', '最近执行'));
    runList = el('div', '', 'sch-runs');
    runList.id = 'schRuns';
    section.append(runList);
    section.append(el('p', '定时线程随控制台运行；控制台重启后会补发 15 分钟内到点的时段。邮件失败也保留记录并写进活动记录。', 'sch-note'));

    save.addEventListener('click', function () { submit(save, 'save'); });
    test.addEventListener('click', function () { submit(test, 'test'); });
    once.addEventListener('click', function () { submit(once, 'run'); });

    var pageHost = document.getElementById('pageHost');
    if (pageHost) pageHost.appendChild(section);
    else document.getElementById('home').appendChild(section);
    built = true;
  }

  function addSegmentRow(start, end) {
    var box = document.getElementById('schSegments');
    var row = el('div', '', 'sch-segment');
    var from = el('input'); from.type = 'time'; from.value = start || '06:00';
    var to = el('input'); to.type = 'time'; to.value = end || '12:00';
    var drop = el('button', '删除'); drop.type = 'button'; drop.className = 'drop';
    drop.addEventListener('click', function () { row.remove(); });
    row.append(from, el('span', '→'), to, drop);
    box.append(row);
  }

  function collect() {
    var segments = [];
    var rows = document.querySelectorAll('#schSegments .sch-segment');
    for (var i = 0; i < rows.length; i++) {
      var inputs = rows[i].querySelectorAll('input[type=time]');
      if (inputs.length === 2 && inputs[0].value && inputs[1].value) {
        segments.push({start: inputs[0].value, end: inputs[1].value});
      }
    }
    return {
      mail: {
        host: document.getElementById('schHost').value.trim(),
        port: Number(document.getElementById('schPort').value) || 465,
        ssl: document.getElementById('schSsl').checked,
        user: document.getElementById('schUser').value.trim(),
        password: document.getElementById('schPass').value,
        to: document.getElementById('schTo').value.trim(),
        from_name: document.getElementById('schFromName').value.trim()
      },
      task: {enabled: document.getElementById('schEnabled').checked, segments: segments}
    };
  }

  async function submit(button, action) {
    if (button) button.disabled = true;
    result.textContent = '';
    try {
      if (action !== 'save') {
        await post('/api/schedule/save', collect());
        if (action === 'test') {
          var tested = await post('/api/schedule/test', {});
          result.textContent = tested.ok ? '测试邮件已发出：' + (tested.subject || '') : '测试失败：' + tested.error;
        } else {
          var ran = await post('/api/schedule/run', {});
          result.textContent = ran.ok ? '汇总已发出：' + (ran.subject || '') : '汇总失败：' + ran.error;
        }
      } else {
        await post('/api/schedule/save', collect());
        result.textContent = '已保存。时段结束后会自动发信。';
      }
      await load();
    } catch (error) {
      result.textContent = error.message;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function render() {
    if (!data) return;
    var mail = data.mail || {};
    document.getElementById('schHost').value = mail.host || 'smtp.qq.com';
    document.getElementById('schPort').value = mail.port || 465;
    document.getElementById('schSsl').checked = mail.ssl !== false;
    document.getElementById('schUser').value = mail.user || '';
    document.getElementById('schTo').value = mail.to || '';
    document.getElementById('schFromName').value = mail.from_name || '';
    var pass = document.getElementById('schPass');
    pass.value = '';
    pass.placeholder = mail.password_set ? '已保存（留空表示不改）' : 'SMTP 授权码（不是登录密码）';
    document.getElementById('schEnabled').checked = Boolean((data.task || {}).enabled);
    var box = document.getElementById('schSegments');
    box.replaceChildren();
    var segments = (data.task && data.task.segments) || data.default_segments || [];
    segments.forEach(function (item) { addSegmentRow(item.start, item.end); });
    var ready = mail.host && mail.user && mail.password_set && mail.to;
    badge.textContent = ready ? ((data.task || {}).enabled ? '已启用' : '已暂停') : '待填邮箱';

    runList.replaceChildren();
    var runs = data.runs || [];
    if (!runs.length) {
      runList.append(el('div', '还没有执行记录。配好邮箱后可先发一封测试邮件。', 'sch-empty'));
    } else {
      runs.slice(0, 10).forEach(function (run) {
        var row = el('div', '', 'sch-run');
        row.append(el('span', stamp(run.time), 'time'), el('span', run.segment || '—', 'time'));
        var state = el('span', run.ok ? '已发送' : '未发送', 'state ' + (run.ok ? 'ok' : 'bad'));
        row.append(state);
        if (run.subject) row.append(el('div', run.subject, 'why'));
        if (run.error) row.append(el('div', '原因：' + run.error, 'why'));
        runList.append(row);
      });
    }
  }

  async function load() {
    try {
      var response = await fetch('/api/schedule');
      var value = await response.json();
      if (!response.ok) throw Error(value.error || '读取失败');
      data = value;
      render();
    } catch (error) {
      if (badge) badge.textContent = '读取失败';
      if (result) result.textContent = error.message;
    }
  }

  function activate() {
    build();
    load();
  }

  window.DshSchedule = {build: build, activate: activate, load: load};
})();
