// Reuse the existing local DSH client without copying or printing credentials.
let raw = '';
for await (const chunk of process.stdin) raw += chunk;
const request = JSON.parse(raw);
const base = request.base;
const parsed = new URL(base);
if (parsed.hostname !== '127.0.0.1' || parsed.protocol !== 'http:') throw Error('Local DSH only');
process.env.DSH_HOME = request.home;
const { NodeApiClient } = await import('./dsh-client.mjs');
const client = new NodeApiClient(base, request.timeoutMs || 45000, {});
const method = request.method;
if (!/^[a-zA-Z]+\/[a-zA-Z]+$/.test(method)) throw Error('Invalid method');
const envelope = { type: 'client-request', rpcId: crypto.randomUUID(), method, payload: { args: request.args || {} } };
const response = await client.doFetch(base + '/api/' + method, {
  method: 'POST', headers: { 'Content-Type': 'application/json', Origin: base }, body: JSON.stringify(envelope)
});
const body = await response.text();
if (!response.ok) throw Error('DSH HTTP ' + response.status);
JSON.parse(body);
process.stdout.write(body);
