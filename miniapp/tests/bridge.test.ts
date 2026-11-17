import { describe, expect, it } from 'vitest';
import { parseHostFrame, makeFrame, reduceJob } from '../src/bridge';

describe('host bridge', () => {
  it('rejects unknown frame fields, versions and non-object payloads', () => {
    const good = {bridgeVersion:1, requestId:'r1', type:'mode.open', payload:{threadId:'t1'}};
    expect(parseHostFrame(good)).toEqual(good);
    expect(parseHostFrame({...good, token:'secret'})).toBeNull();
    expect(parseHostFrame({...good, bridgeVersion:2})).toBeNull();
    expect(parseHostFrame({...good, payload:[]})).toBeNull();
    expect(parseHostFrame({...good, type:'run.shell'})).toBeNull();
    expect(parseHostFrame({...good, payload:{threadId:'t1', token:'secret'}})).toBeNull();
  });
  it('makes the exact allowlisted envelope', () => {
    expect(makeFrame('subscribe', {jobId:'j', after:4}, 'r1')).toEqual({bridgeVersion:1, requestId:'r1', type:'subscribe', payload:{jobId:'j', after:4}});
    expect(() => makeFrame('subscribe', {jobId:'j', token:'secret'}, 'r1')).toThrow();
  });
  it('accepts only Captur\'d engine manifests', () => {
    const init = {bridgeVersion:1, requestId:'r1', type:'init', payload:{engine:{engine:'capturd'}, theme:{}, locale:'en', reducedMotion:false}};
    expect(parseHostFrame(init)).toEqual(init);
    expect(parseHostFrame({...init, payload:{...init.payload, engine:{engine:'forge'}}})).toBeNull();
  });
  it('never regresses a job cursor or accepts another engine/job', () => {
    expect(reduceJob(null, {jobId:'j', engine:'forge', latestSeq:1, state:'running'})).toBeNull();
    const current = {jobId:'j', engine:'capturd', latestSeq:5, state:'running'};
    expect(reduceJob(current, {jobId:'j', engine:'capturd', latestSeq:4, state:'queued'})).toEqual(current);
    expect(reduceJob(current, {jobId:'x', engine:'capturd', latestSeq:6, state:'failed'})).toEqual(current);
    expect(reduceJob(current, {jobId:'j', engine:'forge', latestSeq:6, state:'failed'})).toEqual(current);
    expect(reduceJob(current, {jobId:'j', engine:'capturd', latestSeq:6, state:'succeeded'})?.state).toBe('succeeded');
  });
});
