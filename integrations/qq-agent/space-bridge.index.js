// 小鲸鱼空间委托技能：把 QQ 侧的任务交给空间执行，并取回结果。
// 只调两个接口：POST /api/ext/task（建任务）与 GET /api/ext/task/:id（取结果）。
// 空间那边由她调用全部工具（记忆、桌面、手机、文件等），这里不复制工具实现。
//
// 关键约束：宿主单次工具执行上限 30 秒，所以 delegate 只等一小段；
// 没跑完就登记成 pending，由后台轮询在结果出来后直接发到群里，
// 即使 QQ 这轮已经结束也不会丢结果。

const MAX_INSTRUCTION = 3000;
const HARD_WAIT_CAP = 20; // delegate 自身的等待上限（宿主管 30 秒）
const STATUS_WAIT_CAP = 20; // status 每次最多等多久
const TICK_MS = 4000;

let host = null;
let log = () => {};
let ticking = false;
const pending = new Map(); // taskId -> { chatKey, send, instruction, createdAt, delivered, sending }
const pendingByChat = new Map(); // chatKey -> taskId

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function setup(ctx) {
  host = ctx;
  log = (message) => ctx.log?.warn?.(message);

  ctx.registerTool({
    id: 'delegate',
    name: '委托小鲸鱼空间',
    description:
      '把一个任务交给小鲸鱼空间执行（她有长期记忆、专属桌面、安卓真机、文件与工作区工具），然后返回结果。' +
      '这是远程服务，不是本机工具：群友问手机/电脑/屏幕/截图/安卓版本/记忆/文件时，先用它去问空间。' +
      'instruction 要写清目标与边界；支付、主号登录、删数据这类高风险动作不要委托。',
    category: 'automation',
    icon: '🐋',
    params: {
      type: 'object',
      properties: {
        instruction: { type: 'string', description: '要她做的事，尽量具体：目标、对象、边界' },
        wait_seconds: { type: 'number', description: '可选：最多等几秒（最多 20）' }
      },
      required: ['instruction']
    },
    async execute(args, execution) {
      return delegate(args, execution);
    }
  });

  ctx.registerTool({
    id: 'status',
    name: '查询空间委托结果',
    description:
      '查询之前交给小鲸鱼空间的任务，会先等一段时间再返回。delegate 返回「还在做」时用它，把任务编号原样传进来；' +
      '没出结果可以再查一次，别只查一次就说还在排队。',
    category: 'automation',
    icon: '🐋',
    params: {
      type: 'object',
      properties: {
        id: { type: 'string', description: 'delegate 返回的任务编号' },
        wait_seconds: { type: 'number', description: '可选：这次最多等几秒（默认 15，最多 20）' }
      },
      required: ['id']
    },
    async execute(args) {
      return status(args);
    }
  });

  // 后台投递：delegate 没等到结果就结束的委托，完成后直接发到群里。
  if (ctx.scheduler?.every) {
    ctx.scheduler.every(TICK_MS, () => {
      void tick();
    });
  }
}

export function available() {
  return true;
}

function settings() {
  const read = (key, fallback) => {
    try {
      const value = host?.config?.get?.(key);
      return value === undefined || value === null || value === '' ? fallback : value;
    } catch {
      return fallback;
    }
  };
  return {
    baseUrl: String(read('baseUrl', '')).replace(/\/+$/, ''),
    token: String(read('token', '')).trim(),
    persona: String(read('persona', 'li')),
    waitSeconds: Number(read('waitSeconds', 120))
  };
}

async function call(method, path, body, timeoutMs = 15000) {
  const s = settings();
  if (!s.baseUrl || !s.token) throw new Error('还没配置：请在控制台设置页「小鲸鱼空间」填好地址与委托密钥');
  const options = {
    method,
    headers: { Authorization: 'Bearer ' + s.token },
    signal: AbortSignal.timeout(timeoutMs)
  };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(s.baseUrl + path, options);
  const text = await response.text();
  let value = null;
  try { value = JSON.parse(text); } catch { /* 非 JSON 时按原文报错 */ }
  if (!response.ok) throw new Error((value && value.error) || text.slice(0, 200) || ('HTTP ' + response.status));
  return value || {};
}

function plain(text) {
  return String(text)
    .replace(/```[\s\S]*?```/g, '')
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
    .replace(/^#{1,6}\s*/gm, '')
    .trim();
}

function resultText(value) {
  const result = value.result || {};
  const failure = result.failure || {};
  return plain(String(result.text || failure.message || ''));
}

function isTerminal(value) {
  return ['completed', 'stopped', 'needs-review', 'uncertain'].indexOf(value.status) >= 0;
}

function finishPending(taskId) {
  const item = pending.get(taskId);
  if (!item) return;
  pending.delete(taskId);
  if (pendingByChat.get(item.chatKey) === taskId) pendingByChat.delete(item.chatKey);
}

function pendingForChat(chatKey) {
  const taskId = pendingByChat.get(chatKey);
  if (!taskId) return null;
  const item = pending.get(taskId);
  if (!item) { pendingByChat.delete(chatKey); return null; }
  // 超过 10 分钟的陈旧登记自动作废，避免永远堵着这个会话。
  if (Date.now() - item.createdAt > 10 * 60 * 1000) { finishPending(taskId); return null; }
  return { taskId, item };
}

async function deliver(item, text) {
  if (!item.chatKey || !item.send?.sendTextBatch) return false;
  item.sending = true;
  try {
    const result = await item.send.sendTextBatch(item.chatKey, [text], { source: 'space-bridge' });
    if (result?.ok === false && (result.sent || []).length === 0) throw new Error('发送失败');
    item.delivered = true;
    return true;
  } catch (error) {
    log(`空间委托结果发送失败：${(error && error.message) || error}`);
    return false;
  } finally {
    item.sending = false;
  }
}

async function tick() {
  if (ticking) return;
  ticking = true;
  try {
    for (const [taskId, item] of [...pending]) {
      if (item.delivered || item.sending) continue;
      let value;
      try {
        value = await call('GET', '/api/ext/task/' + encodeURIComponent(taskId), undefined, 10000);
      } catch {
        continue;
      }
      if (!isTerminal(value)) continue;
      const text = resultText(value) || '已完成，但没有文字回复。';
      const message = value.status === 'completed' ? text : '空间那边没跑完：' + text;
      item.attempts = (item.attempts || 0) + 1;
      const sent = await deliver(item, message);
      if (sent || value.status !== 'completed' || item.attempts >= 5) {
        if (!sent) log('空间委托结果多次发送失败，已放弃：' + taskId);
        finishPending(taskId);
      }
    }
  } finally {
    ticking = false;
  }
}

async function delegate(args, execution) {
  const instruction = String((args && args.instruction) || '').trim();
  if (!instruction) return { ok: false, error: '缺少 instruction：请写清要她做什么' };
  if (instruction.length > MAX_INSTRUCTION) return { ok: false, error: '指令太长，请压到 ' + MAX_INSTRUCTION + ' 字以内' };

  const chatKey = String((execution && execution.chatKey) || '');
  const existing = chatKey ? pendingForChat(chatKey) : null;
  if (existing) {
    return '这个会话已经有一个委托在跑（任务编号 ' + existing.taskId + '，内容：' +
      existing.item.instruction.slice(0, 80) + '）。等它出结果再发新的，不要重复委托。';
  }

  const s = settings();
  const requested = Number((args && args.wait_seconds) || s.waitSeconds || HARD_WAIT_CAP);
  const wait = Math.max(5, Math.min(HARD_WAIT_CAP, requested));

  let created;
  try {
    created = await call('POST', '/api/ext/task', { text: instruction, persona: s.persona, source: 'qq-agent' });
  } catch (error) {
    return { ok: false, error: '交给空间时失败：' + String((error && error.message) || error) };
  }
  if (!created.ok || !created.id) return { ok: false, error: created.error || '空间没有接受这次委托' };

  const taskId = created.id;
  const deadline = Date.now() + wait * 1000;
  while (Date.now() < deadline) {
    await sleep(3000);
    let value;
    try {
      value = await call('GET', '/api/ext/task/' + encodeURIComponent(taskId));
    } catch (error) {
      return { ok: false, error: '读取任务结果失败：' + String((error && error.message) || error) };
    }
    if (isTerminal(value)) {
      const text = resultText(value) || '已完成，但没有文字回复；执行回执在空间活动记录里。';
      if (value.status === 'completed') return text;
      return { ok: false, error: text };
    }
  }

  if (chatKey) {
    pending.set(taskId, {
      chatKey,
      send: execution && execution.send,
      instruction,
      createdAt: Date.now(),
      delivered: false,
      sending: false
    });
    pendingByChat.set(chatKey, taskId);
  }
  return '已经交给空间，还在做（任务编号 ' + taskId + '）。结果出来后会直接发到群里，你先回一句在查就行；' +
    '也可以用 status 传这个编号继续等。';
}

async function status(args) {
  const id = String((args && args.id) || '').trim();
  if (!id) return { ok: false, error: '缺少 id：请传任务编号' };
  const requested = Number((args && args.wait_seconds) ?? 15);
  const wait = Math.max(0, Math.min(STATUS_WAIT_CAP, requested));
  const deadline = Date.now() + wait * 1000;

  let value = null;
  while (true) {
    try {
      value = await call('GET', '/api/ext/task/' + encodeURIComponent(id));
    } catch (error) {
      return { ok: false, error: '读取任务结果失败：' + String((error && error.message) || error) };
    }
    if (isTerminal(value)) break;
    if (Date.now() >= deadline) break;
    await sleep(3000);
  }

  if (isTerminal(value)) {
    const text = resultText(value) || '已完成，但没有文字回复。';
    const item = pending.get(id);
    if (item) {
      if (item.delivered) {
        return '（这个任务的结果已经自动发到群里了，不用再发。原文：）\n' + text;
      }
      // 模型拿到了结果，会自己发；取消后台投递，避免重复。
      finishPending(id);
    }
    if (value.status === 'completed') return text;
    return { ok: false, error: text };
  }
  return '还在做（任务编号 ' + id + '，状态 ' + value.status + '），过几秒再查。';
}
