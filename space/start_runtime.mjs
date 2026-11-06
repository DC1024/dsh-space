// Keys are read into memory only. Never copied into the new home or argv/logs.
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=path.dirname(fileURLToPath(import.meta.url));
const installed=process.env.DSH_RUNTIME||'/usr/local/lib/node_modules/@deepseek-ai/dsh';
const require=createRequire(path.join(installed,'package.json'));
const YAML=require('js-yaml');
for(const method of ['log','warn','error']){const original=console[method].bind(console);console[method]=(...args)=>original(...args.map(v=>typeof v==='string'?v.replace(/([?&]token=)[^&\s]+/g,'$1[hidden]'):v));}
// Owner-managed model service credential (0600, container data dir). It takes
// precedence over the deployment env so the WebUI settings page can rotate it.
try{const saved=JSON.parse(fs.readFileSync(path.join(root,'data','model-settings.json'),'utf8'));
 if(typeof saved.api_key==='string'&&saved.api_key.trim())process.env.DEEPSEEK_API_KEY=saved.api_key.trim();}catch{}
for(const source of [process.env.DSH_CREDENTIALS_FILE,
  path.join(process.env.HOME||'/home/node','.dsh','.credentials.yaml'),
  path.join(root,'data','runtime-home','.credentials.yaml')].filter(Boolean)){
 try{const doc=YAML.load(fs.readFileSync(source,'utf8'));const key=doc?.refs?.DEEPSEEK_API_KEY;if(typeof key==='string'&&key){process.env.DEEPSEEK_API_KEY=key;break;}}catch{}
}
if(!process.env.DEEPSEEK_API_KEY)throw Error('Existing DeepSeek API credential unavailable; no secret copied');
process.env.DSH_HOME=path.join(root,'data','runtime-home');
// Pin the deployment permission default so an inherited environment (the owner's
// own shell may run with danger-full-access) can never change what the space
// profile composes. "自动审查" is applied per session by runtime-policy.mjs.
process.env.DSH_PERMISSION_MODE='workspace-write';
process.env.NO_PROXY=[process.env.NO_PROXY,'api.deepseek.com','platform.deepseek.com','127.0.0.1','localhost'].filter(Boolean).join(',');
process.env.no_proxy=process.env.NO_PROXY;
// Record the runtime patch this process actually boots from. The owner settings
// page compares it with the current patch to tell "saved, needs restart" apart
// from "already loaded".
try{const patch=path.join(root,'data','runtime-home','profiles','space','cordis.patch.yml');
 const sha=createHash('sha256').update(fs.readFileSync(patch)).digest('hex');
 fs.writeFileSync(path.join(root,'data','runtime-applied.json'),JSON.stringify({patch_sha256:sha,at:new Date().toISOString()})+'\n');}catch{}
const binPath=fs.existsSync(path.join(installed,'lib/bin.js'))
 ?path.join(installed,'lib/bin.js')
 :path.join(installed,'node_modules/@deepseek-ai/dsh/lib/bin.js');
process.argv=[process.execPath,binPath,'--profile','space','--host','127.0.0.1','--port','19992','--no-open'];
const {runCli}=await import(pathToFileURL(process.argv[1]).href);
await runCli();
