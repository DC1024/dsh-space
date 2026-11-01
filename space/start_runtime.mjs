// Keys are read into memory only. Never copied into the new home or argv/logs.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=path.dirname(fileURLToPath(import.meta.url));
const installed=process.env.DSH_RUNTIME||'/usr/local/lib/node_modules/@deepseek-ai/dsh';
const require=createRequire(path.join(installed,'package.json'));
const YAML=require('js-yaml');
for(const method of ['log','warn','error']){const original=console[method].bind(console);console[method]=(...args)=>original(...args.map(v=>typeof v==='string'?v.replace(/([?&]token=)[^&\s]+/g,'$1[hidden]'):v));}
const credentialCandidates=[
  process.env.DSH_CREDENTIALS_FILE,
  path.join(process.env.HOME||'', '.dsh', '.credentials.yaml'),
  path.join(root, 'data', 'runtime-home', '.credentials.yaml'),
].filter(Boolean);
for(const source of credentialCandidates){
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
const binPath=fs.existsSync(path.join(installed,'lib/bin.js'))
 ?path.join(installed,'lib/bin.js')
 :path.join(installed,'node_modules/@deepseek-ai/dsh/lib/bin.js');
process.argv=[process.execPath,binPath,'--profile','space','--host','127.0.0.1','--port','19992','--no-open'];
const {runCli}=await import(pathToFileURL(process.argv[1]).href);
await runCli();
