/**
 * Fetch the latest failed run's job log (needs GH_TOKEN for logs endpoint).
 *   node _scratch/fetch_ci_log.cjs
 */
const OWNER = 'dboycht';
const REPO = 'raspberry-health-monitor';

async function main() {
  const token = process.env.GH_TOKEN || '';
  const headers = { Accept: 'application/vnd.github+json', 'User-Agent': 'dsh-agent' };
  if (token) headers.Authorization = `token ${token}`;

  const runsRes = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/actions/runs?per_page=1`, { headers });
  const runs = await runsRes.json();
  const run = (runs.workflow_runs || [])[0];
  if (!run) return console.log('no runs');
  console.log(`run #${run.run_number} ${run.status}/${run.conclusion} ${run.head_sha.slice(0, 7)}`);

  const jobsRes = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/actions/runs/${run.id}/jobs`, { headers });
  const jobs = await jobsRes.json();
  for (const job of jobs.jobs || []) {
    console.log(`job: ${job.name} -> ${job.conclusion} (id ${job.id})`);
    const logRes = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/actions/jobs/${job.id}/logs`, {
      headers: { ...headers, Accept: 'application/vnd.github+json' },
      redirect: 'follow',
    });
    if (logRes.status !== 200) {
      console.log(`  cannot fetch log: HTTP ${logRes.status} (token ${token ? 'present' : 'missing'})`);
      continue;
    }
    const text = await logRes.text();
    const lines = text.split('\n');
    // print the FAIL section: last 120 lines is usually where the assertion is
    const interesting = lines.filter((l) =>
      /FAIL|Error|error|Traceback|assert|raise|not found|Exception|====|PASS|结果/.test(l)
    );
    console.log('  --- filtered log lines (last 60) ---');
    for (const line of interesting.slice(-60)) console.log('  ' + line.trimEnd());
  }
}
main();
