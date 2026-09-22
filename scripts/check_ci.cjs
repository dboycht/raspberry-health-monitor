/**
 * Check the latest GitHub Actions runs for this repo (read-only, no token needed
 * for public repos). ASCII only.
 *
 *   node _scratch/check_ci.cjs
 */
const OWNER = 'dboycht';
const REPO = 'raspberry-health-monitor';

async function get(path) {
  const res = await fetch(`https://api.github.com${path}`, {
    headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'dsh-agent' },
  });
  const text = await res.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch { json = { raw: text.slice(0, 300) }; }
  return { status: res.status, json };
}

(async () => {
  const runs = await get(`/repos/${OWNER}/${REPO}/actions/runs?per_page=5`);
  if (runs.status !== 200) {
    console.log(`cannot read runs: HTTP ${runs.status} ${JSON.stringify(runs.json).slice(0, 200)}`);
    return;
  }
  const list = runs.json.workflow_runs || [];
  if (list.length === 0) {
    console.log('no workflow runs yet (GitHub may still be queuing the first one)');
    return;
  }
  console.log(`workflow runs: ${list.length}`);
  for (const run of list) {
    console.log(
      `  #${run.run_number} ${run.name} [${run.event}] status=${run.status} conclusion=${run.conclusion}` +
      ` head=${run.head_sha.slice(0, 7)} created=${run.created_at}`
    );
    console.log(`     ${run.html_url}`);
  }
  const latest = list[0];
  if (latest.status !== 'completed') {
    console.log('\nlatest run not finished yet; re-run this script in ~30s');
    return;
  }
  if (latest.conclusion === 'success') {
    console.log('\nRESULT: latest run SUCCESS');
    return;
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
})();
