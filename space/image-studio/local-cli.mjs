import {renderSvg,providerStatus} from './artifacts.mjs';
let raw='';for await(const chunk of process.stdin)raw+=chunk;
const arg=JSON.parse(raw);
if(!['biao','li'].includes(arg.persona))throw Error('Unknown persona');
const result=arg.name==='image_capabilities'?{svgToPng:true,aiConfigured:false}:arg.name==='image_create_svg'?renderSvg('space:'+arg.persona,arg.svg):null;
if(!result)throw Error('Unknown tool');
delete result.data;
if(result.id){result.preview='/artifacts/'+result.id+'.png';result.editableSource='/artifacts/'+result.id+'.svg';}
process.stdout.write(JSON.stringify(result));
