export function summarize(logText) {
  return {
    totalCommits: 0,
    authors: new Map(),
    mostRecent: undefined
  };
}
