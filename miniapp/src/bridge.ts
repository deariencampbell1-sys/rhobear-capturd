/** The host bridge, pinned to north-star a9a9cb1 §9. */
type JsonObject = Record<string, unknown>;
type Frame = {bridgeVersion: 1; requestId: string; type: string; payload: JsonObject};
const object = (v: unknown): v is JsonObject => typeof v === 'object' && v !== null && !Array.isArray(v);
const exact = (v: JsonObject, required: string[], optional: string[] = []) =>
  required.every(k => Object.hasOwn(v, k)) && Object.keys(v).every(k => [...required, ...optional].includes(k));
const identifier = (v: unknown) => typeof v === 'string' && v.length > 0 && v.length <= 200;
const clean = (v: unknown, depth = 0): boolean => {
  if (depth > 20) return false;
  if (Array.isArray(v)) return v.length <= 1000 && v.every(x => clean(x, depth + 1));
  if (object(v)) return Object.entries(v).every(([k, x]) => !/^(token|accessToken|refreshToken|authorization|signedUrl)$/i.test(k) && clean(x, depth + 1));
  return v === null || typeof v === 'boolean' || typeof v === 'number' || (typeof v === 'string' && v.length <= 10000);
};
const incoming: Record<string, [string[], string[]]> = {
  init:[['engine','theme','locale','reducedMotion'],['job']], 'mode.open':[['threadId'],['jobId','cursor']],
  'scene.render':[['bounds'],[]], 'job.snapshot':[['job'],['event']], 'job.event':[['job'],['event']],
  'artifact.ready':[['artifact'],[]], response:[['ok'],['result','error']], 'save.result':[['outcome'],['destination','reason']],
};
const outgoing: Record<string, [string[], string[]]> = {
  ready:[[],[]], submit:[['input'],[]], cancel:[['jobId'],['reason']], subscribe:[['jobId'],['after']], unsubscribe:[['jobId'],[]],
  approve:[['jobId','expectedRevision','shotIndex','takeIndex'],[]], retake:[['jobId','expectedRevision','shotIndex','takeIndex','direction'],[]],
  recapture:[['jobId','expectedRevision','shotIndex','takeIndex'],[]], 'artifact.open':[['artifactId','revision'],[]],
  'artifact.share':[['artifactId','revision'],[]], 'artifact.download':[['artifactId','revision'],[]],
  'settings.open':[[],[]], 'bank.open':[[],[]], 'chat.open':[[],[]], 'mode.close':[['returnTo'],[]],
  'save.request':[['artifactId','revision'],['destination']], notice:[['code'],['message']], 'action.primary':[['label','enabled','busy'],[]],
};
function validPayload(type: string, payload: JsonObject, allowed: typeof incoming): boolean {
  const schema = allowed[type];
  if (!schema || !exact(payload, ...schema) || !clean(payload)) return false;
  if ('jobId' in payload && !identifier(payload.jobId)) return false;
  if ('expectedRevision' in payload && !identifier(payload.expectedRevision)) return false;
  if ('after' in payload && (!Number.isSafeInteger(payload.after) || (payload.after as number) < 0)) return false;
  if ('threadId' in payload && !identifier(payload.threadId)) return false;
  if ('job' in payload && (!object(payload.job) || payload.job.engine !== 'capturd' || !identifier(payload.job.jobId))) return false;
  if (type === 'init' && (!object(payload.engine) || payload.engine.engine !== 'capturd' || !object(payload.theme) || typeof payload.locale !== 'string' || typeof payload.reducedMotion !== 'boolean')) return false;
  if (type === 'response' && (typeof payload.ok !== 'boolean' || (payload.ok === true && 'error' in payload) || (payload.ok === false && !object(payload.error)))) return false;
  return true;
}
export function parseHostFrame(value: unknown): Frame | null {
  try {
    if (typeof value === 'string') { if (value.length > 1_000_000) return null; value = JSON.parse(value); }
    if (!object(value) || !exact(value, ['bridgeVersion','requestId','type','payload']) || value.bridgeVersion !== 1 || !identifier(value.requestId) || typeof value.type !== 'string' || !object(value.payload) || !validPayload(value.type, value.payload, incoming)) return null;
    return value as Frame;
  } catch { return null; }
}
export function makeFrame(type: string, payload: JsonObject, requestId: string = crypto.randomUUID()): Frame {
  if (!identifier(requestId) || !validPayload(type, payload, outgoing)) throw new Error('Invalid bridge request');
  return {bridgeVersion:1, requestId, type, payload};
}
export type Job = JsonObject & {jobId: string; engine: string; latestSeq: number; state: string};
export function reduceJob(current: Job | null, next: Job): Job | null {
  if (next.engine !== 'capturd' || !identifier(next.jobId) || !Number.isSafeInteger(next.latestSeq) || next.latestSeq < 0) return current;
  if (current && (next.jobId !== current.jobId || next.latestSeq <= current.latestSeq)) return current;
  return next;
}
