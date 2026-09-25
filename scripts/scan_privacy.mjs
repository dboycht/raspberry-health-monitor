/**
 * Pre-push hygiene scanner.
 *   node scripts/scan_privacy.mjs [repo-root]
 * Checks:
 *   1) BOM in source files
 *   2) mojibake signatures
 *   3) non-ASCII bytes inside .ps1/.sh/.bat (must be pure ASCII)
 *   4) obvious secrets (gho_, ghp_, sk-, AKIA, api_key=, token=...)
 *   5) files that should never be committed (data/, *.db, logs, __pycache__)
 *
 * ASCII only: the mojibake needles are written as \u escapes (the point of this
 * file is to find corrupted text, so it must not carry suspicious bytes itself).
 * The repo root defaults to this file's repository, so the canonical checkout
 * works too; a dev-machine absolute path would silently scan nothing there.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, extname, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = process.argv[2]
  ? resolve(process.argv[2])
  : resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SKIP_DIRS = new Set(['.git', 'node_modules', '__pycache__', '.gradle', 'build', 'dist', '.venv']);
const SRC_EXT = new Set(['.py', '.md', '.json', '.kts', '.kt', '.xml', '.yml', '.yaml', '.txt', '.properties', '.cfg', '.toml']);
const ASCII_ONLY_EXT = new Set(['.ps1', '.sh', '.bat', '.cmd']);
// U+FFFD replacement char plus the classic GBK-read-as-UTF8 mojibake leads.
const MOJIBAKE = ['\uFFFD', '\u95c2', '\u938c', '\u95b8'];
const SECRET_PATTERNS = [
  [/gh[oprsu]_[A-Za-z0-9]{20,}/, 'GitHub token'],
  [/sk-[A-Za-z0-9]{20,}/, 'OpenAI-style key'],
  [/AKIA[0-9A-Z]{16}/, 'AWS key'],
  [/(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*["'][^"'{}$<>\s]{16,}["']/i, 'hardcoded credential'],
];
const FORBIDDEN_PATH = [
  /^rpi\/data\//,          // runtime data of the python service
  /\/data\/history\.db$/,
  /\.db$/, /\.log$/, /__pycache__/, /\.pyc$/,
  // NOTE: DEVELOPMENT.md / ERROR.md legitimately exist in the *dev copy*; only a
  // canonical-repo scan should treat them as findings, so they are not listed here.
];

const findings = [];
const files = [];

function walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) walk(full);
    else files.push(full);
  }
}
walk(ROOT);

for (const file of files) {
  const rel = relative(ROOT, file).replace(/\\/g, '/');
  const ext = extname(file).toLowerCase();

  for (const re of FORBIDDEN_PATH) {
    if (re.test('/' + rel)) findings.push(`FORBIDDEN-PATH ${rel}`);
  }

  const buf = readFileSync(file);
  if (!SRC_EXT.has(ext) && !ASCII_ONLY_EXT.has(ext)) continue;

  if (buf.length >= 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf) {
    findings.push(`BOM ${rel}`);
  }

  const text = buf.toString('utf8');
  for (const bad of MOJIBAKE) {
    if (text.includes(bad)) findings.push(`MOJIBAKE(${bad}) ${rel}`);
  }

  if (ASCII_ONLY_EXT.has(ext)) {
    let count = 0;
    for (const b of buf) if (b > 0x7f) count++;
    if (count > 0) findings.push(`NON-ASCII(${count} bytes) ${rel}`);
  }

  for (const [re, label] of SECRET_PATTERNS) {
    const m = text.match(re);
    if (m) findings.push(`SECRET(${label}) ${rel}: ${m[0].slice(0, 40)}...`);
  }
}

// CRLF sanity: report files that contain the '\r\r\n' double-CR damage
for (const file of files) {
  const ext = extname(file).toLowerCase();
  if (!SRC_EXT.has(ext)) continue;
  const text = readFileSync(file).toString('utf8');
  if (text.includes('\r\r\n')) findings.push(`DOUBLE-CR ${relative(ROOT, file)}`);
}

console.log(`scanned files: ${files.length}`);
if (findings.length === 0) {
  console.log('RESULT: clean (no findings)');
  process.exitCode = 0;
} else {
  console.log(`RESULT: ${findings.length} finding(s)`);
  for (const f of findings) console.log('  ' + f);
  // Exit code instead of process.exit(): a forced exit can truncate piped stdout
  // on Windows, which would hide the very findings this script exists to report.
  process.exitCode = 1;
}
