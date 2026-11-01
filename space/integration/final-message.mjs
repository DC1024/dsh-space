import {AsyncLocalStorage} from 'node:async_hooks';

export const ISOLATION_VERSION='1.0.0';
const canonical=key=>/^(group|private):[1-9]\d*$/.test(String(key||''))?String(key):null;
function denied(message){const error=new Error(message);error.statusCode=403;return error;}

// Reject structured internal material, including models which put reasoning
// tags in a text block. Never strip a prefix and risk sending its continuation.
export function assertPublicText(value) {
  const text=String(value??'');
  if(/<\/?(?:think|thinking|analysis|reasoning|tool_call|tool_result|function_calls|antml:thinking)\b|\[(?:im_start|im_end)\]|<\|(?:im_start|im_end|start_header_id|end_header_id|channel)\|>/i.test(text)
    || /(?:^|\n)\s*(?:system\s*:|developer\s*:|assistant\s+to\s*=|analysis\s*:|tool_result\s*:)|【(?:内部推理|主人独立任务|模型连接自动恢复|长期记忆会话|唤醒)】/i.test(text)
    || /(?:^|\n)\s*(?:思考过程|内部推理|推理过程|我的思考过程|系统提示词|隐藏提示词)\s*[：:]/.test(text)
    || /(?:^|\n)\s*(?:用户|主人|user)\s*[：:][^\n]+\n\s*(?:助手|机器人|assistant)\s*[：:]/i.test(text)) {
    throw denied('发送已拦截：内容含内部思考、指令或会话转录');
  }
  return text;
}

export function finalAssistantText(message) {
  if(message?.role && message.role!=='assistant')return '';
  const content=message?.content||[];
  if(content.some(b=>b?.type==='tool-call'))return '';
  const text=content.filter(b=>b?.type==='text'&&typeof b.text==='string').map(b=>b.text).join('');
  try{return assertPublicText(text);}catch{return '';}
}

