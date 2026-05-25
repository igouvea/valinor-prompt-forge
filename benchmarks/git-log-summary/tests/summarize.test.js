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

  it('should parse a single commit correctly', async () => {
    const fs = await import('fs');
    const logText = fs.readFileSync('tests/fixtures/single-commit.log', 'utf-8');
    const result = summarize(logText);

    expect(result.totalCommits).toBe(1);
    expect(result.authors.size).toBe(1);
    expect(result.authors.get('Alice Liddell')).toBe(1);
    expect(result.mostRecent.hash).toBe('abc1234567890def');
    expect(result.mostRecent.author).toBe('Alice Liddell');
    expect(result.mostRecent.subject).toBe('First commit');
  });
});
