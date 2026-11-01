import {safeFetch} from './safe-fetch.mjs';
let raw='';for await(const chunk of process.stdin)raw+=chunk;
const arg=JSON.parse(raw);
if(!['web_search','web_fetch'].includes(arg.name))throw Error('Unknown tool');
if(arg.name==='web_search'&&(typeof arg.query!=='string'||!arg.query.trim()||arg.query.length>300))throw Error('Invalid query');
const url=arg.name==='web_search'?'https://www.bing.com/search?format=rss&q='+encodeURIComponent(arg.query):arg.url;
const result=await safeFetch(url,50000);
const text=result.body.replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi,'').replace(/<[^>]+>/g,' ').replace(/[ \t]+/g,' ').slice(0,16000);
process.stdout.write(JSON.stringify({url:result.url,status:result.statusCode,text,truncated:result.truncated||text.length>=16000,trust:'untrusted public content'}));
