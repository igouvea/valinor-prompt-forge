import { describe, it, expect } from 'vitest';
import { summarize } from '../src/summarize.js';

describe('summarize', () => {
  it('should return zero commits and empty authors for empty input', () => {
    const result = summarize('');
    expect(result.totalCommits).toBe(0);
    expect(result.authors.size).toBe(0);
    expect(result.mostRecent).toBeUndefined();
  });

  it('should return zero commits for whitespace-only input', () => {
    const result = summarize('   \n\n  ');
    expect(result.totalCommits).toBe(0);
    expect(result.authors.size).toBe(0);
    expect(result.mostRecent).toBeUndefined();
  });
});
