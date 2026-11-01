import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { Resvg } from '@resvg/resvg-js';
import { SaxesParser } from 'saxes';
import { PNG } from 'pngjs';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
export const OUTPUT = path.join(ROOT, 'outputs');
const TAGS = new Set('svg g defs title desc rect circle ellipse line polyline polygon path text tspan linearGradient radialGradient stop clipPath use'.split(' '));
const ATTRS = new Set('xmlns xmlns:xlink id width height viewBox preserveAspectRatio x y x1 y1 x2 y2 cx cy r rx ry d points fill fill-opacity fill-rule stroke stroke-width stroke-opacity stroke-linecap stroke-linejoin stroke-dasharray opacity transform font-family font-size font-weight font-style text-anchor dominant-baseline letter-spacing dx dy gradientUnits gradientTransform offset stop-color stop-opacity clip-path clip-rule href xlink:href'.split(' '));
export function canonicalKey(key) {
  if (typeof key !== 'string' || !/^space:(biao|li)$/.test(key)) throw new Error('会话标识无效');
  return key;
}
export function validateSvg(svg) {
  if (typeof svg !== 'string' || Buffer.byteLength(svg) > 300000 || !svg.trim()) throw new Error('SVG 为空或超过 300KB');
  // No entities, processing instructions, scripts, CSS or external resources.
  if (/<!|<\?/.test(svg)) throw new Error('SVG 不允许声明、实体或处理指令');
  let count = 0, depth = 0, root = null;
  const parser = new SaxesParser({ xmlns: false });
  parser.on('error', e => { throw new Error(`SVG XML 无效: ${e.message}`); });
  parser.on('opentag', node => {
    if (++count > 5000 || ++depth > 40 || !TAGS.has(node.name)) throw new Error('SVG 元素不受支持或过于复杂');
    if (!root) {
      if (node.name !== 'svg') throw new Error('根元素必须是 svg');
      root = node;
    } else if (node.name === 'svg') throw new Error('不支持嵌套 svg');
    for (const [key, value] of Object.entries(node.attributes)) {
      if (!ATTRS.has(key) || value.length > 100000) throw new Error(`不支持的 SVG 属性: ${key}`);
      if (key.startsWith('xmlns')) {
        if (value !== (key === 'xmlns' ? 'http://www.w3.org/2000/svg' : 'http://www.w3.org/1999/xlink')) throw new Error('SVG 命名空间无效');
      } else if (key === 'href' || key === 'xlink:href') {
        if (!/^#[a-zA-Z_][\w.-]*$/.test(value)) throw new Error('SVG 只允许内部引用');
        // use is disallowed below to prevent recursive rendering via references.
      } else if (/url\s*\(/i.test(value) && !/^url\(#[a-zA-Z_][\w.-]*\)$/.test(value)) {
        throw new Error('SVG 不允许外部 URL');
      }
    }
    if (node.name === 'use') throw new Error('不支持 use 引用，请直接绘制形状');
  });
  parser.on('closetag', () => { depth--; });
  parser.write(svg).close();
  if (!root) throw new Error('缺少 SVG');
  const width = Number(root.attributes.width), height = Number(root.attributes.height);
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 64 || height < 64 || width > 2048 || height > 2048) throw new Error('SVG width/height 必须为 64–2048 的整数像素');
  return { width, height };
}
function writeArtifact(key, png, info, svg = null) {
  canonicalKey(key);
  const id = crypto.randomUUID();
  fs.mkdirSync(OUTPUT, { recursive: true });
  if (fs.readdirSync(OUTPUT).filter(x => x.endsWith('.png')).length >= 1000) throw new Error('图片库已达1000张，请管理员先归档整理');
  const meta = { id, key, created: new Date().toISOString(), sha256: crypto.createHash('sha256').update(png).digest('hex'), ...info };
  fs.writeFileSync(path.join(OUTPUT, `${id}.png`), png, { flag: 'wx' });
  if (svg) fs.writeFileSync(path.join(OUTPUT, `${id}.svg`), svg, { flag: 'wx' });
  fs.writeFileSync(path.join(OUTPUT, `${id}.json`), JSON.stringify(meta, null, 2), { flag: 'wx' });
  return { ...meta, data: png.toString('base64'), mimeType: 'image/png' };
}
export function renderSvg(key, svg) {
  const dimensions = validateSvg(svg);
  const rendered = new Resvg(svg, { font: { loadSystemFonts: false, fontFiles: ['C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/msyhbd.ttc'], defaultFontFamily: 'Microsoft YaHei' } }).render();
  const png = rendered.asPng();
  if (png.length > 8 * 1024 * 1024) throw new Error('PNG 超过 8MB');
  return writeArtifact(key, png, { ...dimensions, method: 'svg-layout', editable: true }, svg);
}
export function loadArtifact(key, id) {
  canonicalKey(key);
  if (typeof id !== 'string' || !/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(id)) throw new Error('图片编号无效');
  const meta = JSON.parse(fs.readFileSync(path.join(OUTPUT, `${id}.json`), 'utf8'));
  if (meta.key !== key || meta.id !== id) throw new Error('图片不属于当前会话');
  const png = fs.readFileSync(path.join(OUTPUT, `${id}.png`));
  if (crypto.createHash('sha256').update(png).digest('hex') !== meta.sha256) throw new Error('图片校验失败');
  return { meta, png };
}
export function providerStatus() {
  const config = JSON.parse(fs.readFileSync(path.join(ROOT, 'provider.json'), 'utf8'));
  const configured = config.enabled === true && !!config.endpoint && !!config.model;
  const keyReady = !config.apiKeyEnv || !!process.env[config.apiKeyEnv];
  return { layout: true, aiConfigured: configured && keyReady, aiEnabled: config.enabled === true, aiNote: configured && keyReady ? '已配置，仍需真实生图验收' : '未配置生图服务；可制作本机 SVG 排版图，不能冒充 AI 绘画', edits: false };
}
export async function generateAi(key, prompt) {
  canonicalKey(key);
  if (!providerStatus().aiConfigured) throw new Error('尚未配置 AI 生图服务。请管理员在本机配置 image-studio/provider.json；不要将 SVG 排版图称为 AI 生图。');
  if (typeof prompt !== 'string' || !prompt.trim() || prompt.length > 8000) throw new Error('生图描述须为 1–8000 字');
  const config = JSON.parse(fs.readFileSync(path.join(ROOT, 'provider.json'), 'utf8'));
  const endpoint = new URL(config.endpoint);
  if (endpoint.username || endpoint.password || endpoint.search || endpoint.hash || (endpoint.protocol !== 'https:' && !(endpoint.protocol === 'http:' && ['127.0.0.1','localhost','[::1]'].includes(endpoint.hostname)))) throw new Error('生图地址须为 HTTPS 或本机环回地址');
  const response = await fetch(endpoint, { method: 'POST', redirect: 'error', headers: { 'content-type': 'application/json', ...(config.apiKeyEnv ? { authorization: `Bearer ${process.env[config.apiKeyEnv]}` } : {}) }, body: JSON.stringify({ model: config.model, prompt, n: 1, size: config.size || '1024x1024', ...(config.responseFormat ? { response_format: config.responseFormat } : {}) }), signal: AbortSignal.timeout(180000) });
  if (!response.ok) throw new Error(`生图服务 HTTP ${response.status}；请求不自动重试`);
  const chunks = []; let bytes = 0;
  for await (const chunk of response.body) {
    bytes += chunk.length;
    if (bytes > 12000000) throw new Error('生图响应过大');
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const encoded = value?.data?.[0]?.b64_json;
  if (typeof encoded !== 'string' || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) throw new Error('生图服务须返回 data[0].b64_json PNG；不自动下载远程 URL');
  const png = Buffer.from(encoded, 'base64');
  if (png.length < 24 || png.length > 8 * 1024 * 1024 || png.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a') throw new Error('生图服务没有返回有效 PNG');
  const width = png.readUInt32BE(16), height = png.readUInt32BE(20);
  if (!width || !height || width > 2048 || height > 2048) throw new Error('AI 图片尺寸须不超过 2048×2048');
  PNG.sync.read(png, { checkCRC: true });
  return writeArtifact(key, png, { width, height, method: 'ai-generation', editable: false });
}
