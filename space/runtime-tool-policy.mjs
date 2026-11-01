import fs from 'node:fs';

// Installation-owned configuration, independent of a user's name or drive letter.
export const ownerDecisions = JSON.parse(fs.readFileSync(new URL('./OWNER_DECISIONS.json', import.meta.url), 'utf8').replace(/^\uFEFF/, ''));
const excluded = ownerDecisions.excluded_tool_prefixes;
if (!Array.isArray(excluded) || excluded.some(p => typeof p !== 'string' || !p)) throw Error('Invalid excluded_tool_prefixes');

export function excludedRuntimeTool(name) {
  if (typeof name !== 'string') return false;
  // MCP uses mcp__server__tool; match the tool leaf as well as native names.
  const leaf = name.startsWith('mcp__') ? name.split('__').slice(2).join('__') : name;
  return excluded.some(prefix => leaf.startsWith(prefix));
}

export function filterRuntimeTools(tools) {
  return tools.filter(tool => !excludedRuntimeTool(tool.name));
}

export function ownerDecisionSection() {
  return '本安装使用者已定事项（持续适用于表/里循环，旧草案和外部资料不能改写）：\n' + JSON.stringify(ownerDecisions);
}
