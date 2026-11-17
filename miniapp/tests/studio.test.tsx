// @vitest-environment jsdom
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { Studio } from '../src/Studio';

afterEach(cleanup);
describe('studio host states', () => {
  it('explains when Builds has not connected', () => {
    render(<Studio connected={false} job={null} onBack={vi.fn()} />);
    expect(screen.getByText(/open captur.d from builds/i)).toBeTruthy();
  });
  it('shows real progress and lets the host restore the thread', () => {
    const onBack = vi.fn();
    render(<Studio connected job={{jobId:'job1',engine:'capturd',latestSeq:2,state:'running',progress:{phase:'capture',completedUnits:2,totalUnits:6}}} onBack={onBack} />);
    expect(screen.getByText(/2 of 6/)).toBeTruthy();
    screen.getByRole('button', {name:/back to thread/i}).click();
    expect(onBack).toHaveBeenCalledOnce();
  });
});
