import React from 'react';
import { Button } from '@rhobear/family-kit/react/Button';
import type { Job } from './bridge';

type Props = {connected: boolean; job: Job | null; onBack: () => void};
export function Studio({connected, job, onBack}: Props) {
  if (!connected) return <main className="studio"><h1>Captur'd</h1><p>Open Captur'd from Builds to connect to your work.</p></main>;
  const progress = job?.progress && typeof job.progress === 'object' ? job.progress as Record<string, unknown> : null;
  const count = typeof progress?.completedUnits === 'number' && typeof progress?.totalUnits === 'number'
    ? `${progress.completedUnits} of ${progress.totalUnits}` : null;
  return <main className="studio"><header><h1>Captur'd</h1><Button variant="secondary" onPress={onBack}>Back to thread</Button></header>
    {!job ? <section aria-live="polite"><p>No capture is open.</p><p>Start a capture in your thread.</p></section> :
      <section aria-live="polite"><p>{job.state === 'running' ? 'Your agent is capturing' : `Capture: ${String(job.state)}`}</p>
        {count && <p>{count}</p>}
        <div className="stage"><p>Waiting for capture details from Builds.</p></div>
      </section>}
  </main>;
}
