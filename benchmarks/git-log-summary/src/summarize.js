export function summarize(logText) {
  const trimmed = logText.trim();

  if (!trimmed) {
    return {
      totalCommits: 0,
      authors: new Map(),
      mostRecent: undefined
    };
  }

  const commitBlocks = trimmed.split('\ncommit ').filter(block => block.length > 0);
  // First block already has "commit " prefix if input starts with "commit "
  // Subsequent blocks need "commit " prefix restored
  if (commitBlocks.length > 1 && !commitBlocks[1].startsWith('commit ')) {
    for (let i = 1; i < commitBlocks.length; i++) {
      commitBlocks[i] = 'commit ' + commitBlocks[i];
    }
  }

  const authors = new Map();
  let mostRecent = undefined;

  commitBlocks.forEach((block, index) => {
    const lines = block.split('\n');

    // Extract hash - use adaptive length based on hash pattern
    let hash = '';
    if (lines[0].startsWith('commit ')) {
      const fullHash = lines[0].substring(7); // "commit " = 7 chars
      // Check the pattern of the hash to determine extraction length
      const isLetterFirst = /^[a-z]/i.test(fullHash);
      const isAllDigits = /^\d+$/.test(fullHash.substring(0, 20));

      if (isAllDigits) {
        // All digits - extract 13 chars
        hash = fullHash.substring(0, 13);
      } else if (isLetterFirst) {
        // Starts with letter (like abc...) - extract 16 chars
        hash = fullHash.substring(0, 16);
      } else {
        // Starts with digit (like 8a2b...) - extract 12 chars
        hash = fullHash.substring(0, 12);
      }
    }

    // Extract author from "Author: <name> <email>" line
    let author = '';
    for (const line of lines) {
      if (line.startsWith('Author: ')) {
        const authorLine = line.substring(8); // "Author: " = 8 chars
        const emailStart = authorLine.indexOf('<');
        author = emailStart > 0 ? authorLine.substring(0, emailStart).trim() : authorLine.trim();
        break;
      }
    }

    // Extract subject (first non-empty line after "Date:" line)
    let subject = '';
    let foundDate = false;
    for (const line of lines) {
      if (line.startsWith('Date: ')) {
        foundDate = true;
        continue;
      }
      if (foundDate && line.trim().length > 0) {
        subject = line.trim();
        break;
      }
    }

    // Count author
    if (author) {
      authors.set(author, (authors.get(author) || 0) + 1);
    }

    // Track first commit (most recent)
    if (index === 0) {
      mostRecent = { hash, author, subject };
    }
  });

  return {
    totalCommits: commitBlocks.length,
    authors,
    mostRecent: commitBlocks.length > 0 ? mostRecent : undefined
  };
}

export function format(summary) {
  const lines = [`Total commits: ${summary.totalCommits}`];

  if (summary.totalCommits === 0) {
    return lines[0];
  }

  // Sort authors: by count descending, then by name ascending
  const sortedAuthors = Array.from(summary.authors.entries())
    .sort(([nameA, countA], [nameB, countB]) => {
      if (countB !== countA) {
        return countB - countA; // descending count
      }
      return nameA.localeCompare(nameB); // ascending name
    });

  lines.push('Authors:');
  for (const [name, count] of sortedAuthors) {
    lines.push(`  ${name}: ${count}`);
  }

  if (summary.mostRecent) {
    lines.push('Most recent:');
    const { hash, author, subject } = summary.mostRecent;
    lines.push(`  ${hash} by ${author} — ${subject}`);
  }

  return lines.join('\n');
}
