import { parentPort, workerData } from 'node:worker_threads';
import { renderSvg } from './artifacts.mjs';
try { parentPort.postMessage({ ok: true, result: renderSvg(workerData.key, workerData.svg) }); }
catch (error) { parentPort.postMessage({ ok: false, error: error.message }); }
