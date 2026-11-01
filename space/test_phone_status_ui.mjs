import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('./dist/app-v02.js',import.meta.url),'utf8');
const helper=source.slice(source.indexOf('function describePhoneStatus('));
const describe=vm.runInNewContext(helper+'\ndescribePhoneStatus;',{stamp:value=>value});
const now=Date.parse('2026-10-22T09:40:40Z');

test('fresh device readiness does not claim a running phone bridge',()=>{
  const value=describe({ready:true,at:'2026-10-22T09:40:33Z'},now);
  assert.match(value.text,/手机设备：已就绪/);
  assert.doesNotMatch(value.text,/桥.*已连接/);
  assert.match(value.title,/不代表旧手机桥或自主活动已经启动/);
});

test('old positive readiness becomes unknown rather than staying normal',()=>{
  const value=describe({ready:true,at:'2026-10-22T09:39:00Z'},now);
  assert.match(value.text,/状态待确认/);
  assert.doesNotMatch(value.text,/已就绪/);
});

test('missing or invalid verification time cannot produce ready state',()=>{
  for(const at of [undefined,'invalid','2026-10-22T09:45:00Z']){
    assert.match(describe({ready:true,at},now).text,/状态待确认/);
  }
});

test('fresh negative device result is shown independently',()=>{
  const value=describe({ready:false,error:'固定模拟器未连接',at:'2026-10-22T09:40:39Z'},now);
  assert.match(value.text,/未就绪.*固定模拟器未连接/);
});

test('truthy string cannot impersonate confirmed boolean readiness',()=>{
  assert.match(describe({ready:'false',at:'2026-10-22T09:40:39Z'},now).text,/未就绪/);
});

test('status request failure clears an already displayed positive phone label',async()=>{
  const refresh=source.slice(source.indexOf('async function refresh(){'),source.indexOf('\nfunction renderStatus(){'));
  const elements={connection:{textContent:'ok'},liveDot:{className:'live'},phoneState:{textContent:'手机设备：已就绪'}};
  const context={fetch:async()=>{throw new Error('offline');},$:id=>elements[id]};
  vm.runInNewContext(refresh+'\nthis.runRefresh=refresh;',context);
  await context.runRefresh();
  assert.match(elements.phoneState.textContent,/状态待确认/);
  assert.doesNotMatch(elements.phoneState.textContent,/已就绪/);
});
