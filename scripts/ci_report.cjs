/**
 * Dump every workflow run with per-step conclusions, so we can see exactly which
 * step ever failed. ASCII only.
 *
 *   node _scratch/ci_report.cjs
 */
const OWNER = 'dboycht';
const REPO = 'raspberry-health-monitor';

async function api(path) {
  const res = await fetch(`https://api.github.com${path}`, {
    headers: {
      Authorization: `token ${process.env.GH_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'User-Agent': 'dsh-agent',
    },
  });
  return { status: res.status, json: await res.json() };
}

(async () => {
  const runs = await api(`/repos/${OWNER}/${REPO}/actions/runs?per_page=10`);
  if (runs.status !== 200) {
    console.log('cannot list runs:', runs.status, JSON.stringify(runs.json).slice(0, 200));
    return;
  }
  for (const run of runs.json.workflow_runs) {
    console.log(
      `#${run.run_number} ${run.status}/${run.conclusion} sha=${run.head_sha.slice(0, 7)}` +
      ` event=${run.event} created=${run.created_at}`
    );
    const jobs = await api(`/repos/${OWNER}/${REPO}/actions/runs/${run.id}/jobs`);
    for (const job of jobs.json.jobs || []) {
      console.log(`   job "${job.name}" -> ${job.status}/${job.conclusion}`);
      for (const step of job.steps || []) {
        const mark = step.conclusion === 'success' ? 'ok  ' : (step.conclusion === 'skipped' ? 'skip' : 'FAIL');
        console.log(`     [${mark}] ${step.name}`);
      }
    }
  }
})();
