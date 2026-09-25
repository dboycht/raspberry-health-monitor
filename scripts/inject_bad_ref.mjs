/**
 * Doc-checker self-test: inject a dangling reference, assert the checker fails,
 * then restore.
 *
 *   node scripts/inject_bad_ref.mjs inject
 *   node scripts/inject_bad_ref.mjs restore
 *
 * ASCII only: the injected Chinese text and the comments are written with
 * explicit \u escapes (project rule for scripts, see ERROR.md E14), so no
 * editor/console encoding can corrupt them and no shell quoting can eat them.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Repo root derived from this file's location, so the copy in the canonical
// checkout works too (a hard-coded dev path would silently fail there).
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const TARGET = resolve(REPO_ROOT, 'docs', 'README.md');
const MARK = '\u4e34\u65f6\u5f15\u7528'; // "temporary reference"
const BAD = '\u0064\u006f\u0063\u0073\u002f\u0039\u0039\u002d\u4e0d\u5b58\u5728\u7684\u6587\u6863\u002e\u006d\u0064'; // docs/99-<missing>.md
const LINE = `${MARK}\uff1a\`${BAD}\`\n`;
const MODE = process.argv[2] || 'inject';

let text = readFileSync(TARGET, 'utf8');
if (MODE === 'inject') {
  if (text.includes(LINE)) {
    console.log('already injected');
  } else {
    // In the body (not inside a fenced code block) so the checker must catch it.
    writeFileSync(TARGET, text + '\n' + LINE, 'utf8');
    console.log('injected dangling reference');
  }
} else {
  writeFileSync(TARGET, text.split(LINE).join(''), 'utf8');
  console.log('restored');
}
