/**
 * Read the latest GitHub Actions runs for this repo.
 *
 *   node scripts/check_ci.cjs            # human-readable report
 *   node scripts/check_ci.cjs --json     # machine-readable (used by the self-test)
 *   node scripts/check_ci.cjs --help
 *
 * Exit code: 0 = latest run succeeded (or nothing ran yet), 1 = latest run
 * failed / still running, 2 = could not read the API, 3 = TLS fallback failed.
 *
 * Read-only and unauthenticated: this repo is public, so no token is needed.
 * ASCII only (project rule for probe/script files).
 *
 * NOTE: does not call process.exit() directly - see the wrapper at the bottom
 * (a forced exit can truncate piped stdout on Windows).
 */

'use strict';

const { isCertError, withSystemCa, manualHint } = require('./lib/net.cjs');

const OWNER = 'dboycht';
const REPO = 'raspberry-health-monitor';

async function get(path) {
  const res = await fetch(`https://api.github.com${path}`, {
    headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'dsh-agent' },
  });
  const text = await res.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = { raw: text.slice(0, 300) };
  }
  return { status: res.status, json };
}

const HELP = `usage: node scripts/check_ci.cjs [--json]

Reads the latest GitHub Actions runs for ${OWNER}/${REPO} (read-only, no token).
Environment note: if a TLS-intercepting proxy is running, this script retries
itself once with --use-system-ca, so no extra flag is needed.`;

/**
 * @returns {Promise<number>} exit code
 */
async function main(argv) {
  if (argv.includes('--help') || argv.includes('-h')) {
    console.log(HELP);
    return 0;
  }
  const asJson = argv.includes('--json');

  const runs = await get(`/repos/${OWNER}/${REPO}/actions/runs?per_page=5`);
  if (runs.status !== 200) {
    const detail = JSON.stringify(runs.json).slice(0, 200);
    if (asJson) {
      console.log(JSON.stringify({ ok: false, reason: `HTTP ${runs.status}`, detail }, null, 2));
    } else {
      console.log(`cannot read runs: HTTP ${runs.status} ${detail}`);
    }
    return 2;
  }

  const list = runs.json.workflow_runs || [];
  const latest = list[0] || null;

  if (asJson) {
    console.log(JSON.stringify({
      ok: true,
      repo: `${OWNER}/${REPO}`,
      runCount: list.length,
      latest: latest && {
        number: latest.run_number,
        status: latest.status,
        conclusion: latest.conclusion,
        head: String(latest.head_sha || '').slice(0, 7),
        url: latest.html_url,
        createdAt: latest.created_at,
      },
    }, null, 2));
    return latest && latest.conclusion === 'failure' ? 1 : 0;
  }

  if (list.length === 0) {
    console.log('no workflow runs yet (GitHub may still be queuing the first one)');
    return 0;
  }

  console.log(`workflow runs: ${list.length}`);
  for (const run of list) {
    console.log(
      `  #${run.run_number} ${run.name} [${run.event}] status=${run.status} conclusion=${run.conclusion}` +
      ` head=${String(run.head_sha || '').slice(0, 7)} created=${run.created_at}`
    );
    console.log(`     ${run.html_url}`);
  }

  if (latest.status !== 'completed') {
    console.log('\nlatest run not finished yet; re-run this script in ~30s');
    return 1;
  }
  if (latest.conclusion === 'success') {
    console.log('\nRESULT: latest run SUCCESS');
    return 0;
  }

  console.log(`\nRESULT: latest run ${latest.conclusion} -> fetching jobs for detail`);
  const jobs = await get(`/repos/${OWNER}/${REPO}/actions/runs/${latest.id}/jobs`);
  for (const job of jobs.json.jobs || []) {
    console.log(`  job ${job.name}: ${job.status}/${job.conclusion}`);
    for (const step of job.steps || []) {
      if (step.conclusion && step.conclusion !== 'success' && step.conclusion !== 'skipped') {
        console.log(`     FAILED step: ${step.name} (${step.conclusion})`);
      }
    }
  }
  return 1;
}

/** Run main() and let the process drain stdout naturally (no forced exit).
 *
 * `opts.spawn` exists so the self-test can exercise the certificate fallback
 * without spawning a real process (and without needing a proxy to be running).
 */
function run(argv, opts) {
  const options = opts || {};
  // NOTE: `main` is looked up at call time (module.exports.main), not captured,
  // so the self-test can substitute it via require('./check_ci.cjs').main = ...
  const attempt = options.main || ((args) => module.exports.main(args));
  return withSystemCa(() => attempt(argv), {
    scriptPath: options.scriptPath || __filename,
    args: argv,
    warn: options.warn || ((text) => console.error(text)),
    spawn: options.spawn,
  }).then(
    (code) => {
      process.exitCode = code;
      return code;
    },
    (err) => {
      if (isCertError(err)) {
        console.error(manualHint(__filename));
      } else {
        console.error('check_ci failed:', err && err.message ? err.message : err);
      }
      process.exitCode = 3;
      return 3;
    },
  );
}

// Exports are assigned BEFORE run() is invoked below: run() looks main up at
// call time (see its NOTE), so module.exports.main must already be in place.
module.exports = { main, run, get, OWNER, REPO };

if (require.main === module) {
  run(process.argv.slice(2));
}
