import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import '@rhobear/family-kit/tokens.css';
import '@rhobear/family-kit/fonts.css';
import '@rhobear/family-kit/react/button.css';
import './studio.css';
import {makeFrame, parseHostFrame, reduceJob, type Job} from './bridge';
import {Studio} from './Studio';

function App() {
  const [connected, setConnected] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  useEffect(() => {
    // The web host controls both origins. A native transport requires its own
    // authenticated adapter; no wildcard postMessage is used here.
    if (window.parent === window || location.origin === 'null') return;
    const send = (type: string, payload: Record<string, unknown>) => window.parent.postMessage(makeFrame(type, payload), location.origin);
    const receive = (event: MessageEvent) => {
      if (event.source !== window.parent || event.origin !== location.origin) return;
      const frame = parseHostFrame(event.data);
      if (!frame) return;
      if (frame.type === 'init') {
        setConnected(true);
        if (frame.payload.job) setJob(old => reduceJob(old, frame.payload.job as Job));
      }
      if (frame.type === 'mode.open' && typeof frame.payload.jobId === 'string') {
        send('subscribe', {jobId:frame.payload.jobId, ...(typeof frame.payload.cursor === 'number' ? {after:frame.payload.cursor} : {})});
      }
      if (frame.type === 'job.snapshot' || frame.type === 'job.event') setJob(old => reduceJob(old, frame.payload.job as Job));
    };
    window.addEventListener('message', receive);
    send('ready', {});
    return () => window.removeEventListener('message', receive);
  }, []);
  return <Studio connected={connected} job={job} onBack={() => {
    if (window.parent !== window && location.origin !== 'null') window.parent.postMessage(makeFrame('mode.close', {returnTo:'chat'}), location.origin);
  }} />;
}
const root = document.getElementById('root');
if (root) createRoot(root).render(<App />);
