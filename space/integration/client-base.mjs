// The space uses explicit RPC envelopes and an independent read-only follower.
// Only the transport base required by NodeApiClient is retained here.
import {randomUUID} from 'node:crypto';
export class AbstractApiClient {
  constructor(timeoutMs=45000){this.timeoutMs=timeoutMs;}
  mintRpcId(){return randomUUID();}
  onEnvelope(){}
  async postJson(path,body,signal,policy='default'){
    const effective=policy==='caller-signal-only'?signal:signal?AbortSignal.any([signal,AbortSignal.timeout(this.timeoutMs)]):AbortSignal.timeout(this.timeoutMs);
    const response=await this.doFetch(new URL(path,this.resolveBase()),{method:'POST',headers:{'Content-Type':'application/json',Origin:this.resolveBase()},body:JSON.stringify(body),signal:effective});
    if(!response.ok)throw Error('DSH HTTP '+response.status);
    return response;
  }
}
