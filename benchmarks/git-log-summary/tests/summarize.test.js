import { describe, it, expect } from 'vitest';
import { summarize, format } from '../src/summarize.js';

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

  it('should parse three-commit fixture correctly', async () => {
    const fs = await import('fs');
    const logText = fs.readFileSync('tests/fixtures/three-commits.log', 'utf-8');
    const result = summarize(logText);

    expect(result.totalCommits).toBe(3);
    expect(result.authors.size).toBe(2);
    expect(result.authors.get('Jane Doe')).toBe(2);
    expect(result.authors.get('John Smith')).toBe(1);
    expect(result.mostRecent.hash).toBe('8a2b1c3d4e5f');
    expect(result.mostRecent.author).toBe('Jane Doe');
    expect(result.mostRecent.subject).toBe('Add login endpoint');
  });

  it('should handle tied author counts (all 1 commit each)', async () => {
    const fs = await import('fs');
    const logText = fs.readFileSync('tests/fixtures/tied-authors.log', 'utf-8');
    const result = summarize(logText);

    expect(result.totalCommits).toBe(3);
    expect(result.authors.size).toBe(3);
    expect(result.authors.get('Alice Liddell')).toBe(1);
    expect(result.authors.get('Bob Builder')).toBe(1);
    expect(result.authors.get('Charlie Chaplin')).toBe(1);
    expect(result.mostRecent.hash).toBe('1111111111111');
    expect(result.mostRecent.author).toBe('Charlie Chaplin');
    expect(result.mostRecent.subject).toBe('Fix typo');
  });
});

describe('format', () => {
  it('should format summary output with single author', () => {
    const summary = {
      totalCommits: 1,
      authors: new Map([['Alice Liddell', 1]]),
      mostRecent: { hash: 'abc1234567890def', author: 'Alice Liddell', subject: 'First commit' }
    };

    const output = format(summary);
    const lines = output.split('\n');

    expect(lines[0]).toBe('Total commits: 1');
    expect(lines[1]).toBe('Authors:');
    expect(lines[2]).toBe('  Alice Liddell: 1');
    expect(lines[3]).toBe('Most recent:');
    expect(lines[4]).toBe('  abc1234567890def by Alice Liddell — First commit');
  });

  it('should format summary output with multiple authors', () => {
    const summary = {
      totalCommits: 3,
      authors: new Map([['Jane Doe', 2], ['John Smith', 1]]),
      mostRecent: { hash: '8a2b1c3d', author: 'Jane Doe', subject: 'Add login endpoint' }
    };

    const output = format(summary);
    const lines = output.split('\n');

    expect(lines[0]).toBe('Total commits: 3');
    expect(lines[1]).toBe('Authors:');
    expect(lines[2]).toBe('  Jane Doe: 2');
    expect(lines[3]).toBe('  John Smith: 1');
    expect(lines[4]).toBe('Most recent:');
    expect(lines[5]).toBe('  8a2b1c3d by Jane Doe — Add login endpoint');
  });

  it('should format empty summary', () => {
    const summary = {
      totalCommits: 0,
      authors: new Map(),
      mostRecent: undefined
    };

    const output = format(summary);
    expect(output).toBe('Total commits: 0');
  });

  it('should sort tied authors alphabetically', () => {
    const summary = {
      totalCommits: 3,
      authors: new Map([['Charlie Chaplin', 1], ['Alice Liddell', 1], ['Bob Builder', 1]]),
      mostRecent: { hash: '1111111111111', author: 'Charlie Chaplin', subject: 'Fix typo' }
    };

    const output = format(summary);
    const lines = output.split('\n');

    expect(lines[2]).toBe('  Alice Liddell: 1');
    expect(lines[3]).toBe('  Bob Builder: 1');
    expect(lines[4]).toBe('  Charlie Chaplin: 1');
  });
});
