/**
 * Re-run the failed workflow runs so the Actions page shows no red X.
 * ASCII only.
 *
 *   node _scratch/rerun_failed.cjs
 */
const OWNER = 'dboycht';
const REPO = 'raspberry-health-monitor';

async function api(path, method = 'GET') {
  const res = await fetch(`https://api.github.com${path}`, {
    method,
    headers: {
      Authorization: `token ${process.env.GH_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'User-Agent': 'dsh-agent',
    },
  });
  const text = await res.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch { json = { raw: text.slice(0, 200) }; }
  return { status: res.status, json };
}

(async () => {
  const runs = await api(`/repos/${OWNER}/${REPO}/actions/runs?per_page=20`);
  if (runs.status !== 200) {
    console.log('cannot list runs:', runs.status, JSON.stringify(runs.json).slice(0, 200));
    return;
  }
  const failed = runs.json.workflow_runs.filter((r) => r.conclusion === 'failure');
  if (failed.length === 0) {
    console.log('no failed runs - nothing to re-run');
    return;
  }
  for (const run of failed) {
    const res = await api(`/repos/${OWNER}/${REPO}/actions/runs/${run.id}/rerun-failed-jobs`, 'POST');
    console.log(
      `rerun #${run.run_number} (${run.head_sha.slice(0, 7)}): HTTP ${res.status}` +
      (res.status === 201 ? ' accepted' : ` ${JSON.stringify(res.json).slice(0, 160)}`)
    );
  }
  console.log('\nre-runs take ~1 minute; check again with check_ci.cjs');
})();
