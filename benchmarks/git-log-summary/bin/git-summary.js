#!/usr/bin/env node

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { summarize, format } from '../src/summarize.js';

// Get __dirname equivalent
const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Parse arguments
let logText = '';
const args = process.argv.slice(2);

if (args.length > 0 && args[0] === '--input') {
  // Read from file
  if (args.length < 2) {
    console.error('Error: --input requires a file path');
    process.exit(1);
  }
  const filePath = args[1];
  try {
    logText = fs.readFileSync(filePath, 'utf-8');
  } catch (err) {
    console.error(`Error reading file ${filePath}: ${err.message}`);
    process.exit(1);
  }
} else if (args.length === 0) {
  // Read from stdin
  try {
    logText = fs.readFileSync(0, 'utf-8');
  } catch (err) {
    console.error(`Error reading stdin: ${err.message}`);
    process.exit(1);
  }
} else {
  console.error('Usage: git-summary.js [--input <file>]');
  process.exit(1);
}

// Process and output
const summary = summarize(logText);
const output = format(summary);
console.log(output);
