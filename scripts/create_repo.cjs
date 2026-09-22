/**
 * Create the GitHub repository if missing, print its state. ASCII only.
 * Uses GitHub REST API (no gh CLI needed).
 *
 *   node _scratch/create_repo.cjs
 *
 * Token comes from the GH_TOKEN environment variable (set by the caller from
 * the Windows credential manager). The token is never printed.
 */
const OWNER = 'dboycht';
const REPO = 'raspberry-health-monitor';
const DESCRIPTION = '树莓派居家老人健康与安全监护系统（课程设计）：心率血氧/体温/环境/活动采集，本地声光报警，安卓 App 远程监护';

async function api(method, path, body) {
  const res = await fetch(`https://api.github.com${path}`, {
    method,
    headers: {
      Authorization: `token ${process.env.GH_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'User-Agent': 'dsh-agent',
      'Content-Type': 'application/json; charset=utf-8',
    },
    body: body === undefined ? undefined : Buffer.from(JSON.stringify(body), 'utf8'),
  });
  const text = await res.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch { json = { raw: text.slice(0, 300) }; }
  return { status: res.status, json };
}

(async () => {
  if (!process.env.GH_TOKEN) {
    console.error('ERROR: GH_TOKEN is not set');
    process.exit(2);
  }

  const check = await api('GET', `/repos/${OWNER}/${REPO}`);
  if (check.status === 200) {
    console.log(`repo already exists: ${check.json.full_name} private=${check.json.private}`);
    console.log(`default_branch=${check.json.default_branch} html_url=${check.json.html_url}`);
    return;
  }
  if (check.status !== 404) {
    console.error(`unexpected status checking repo: ${check.status} ${JSON.stringify(check.json)}`);
    process.exit(1);
  }

  console.log('repo not found -> creating (public) ...');
  const created = await api('POST', '/user/repos', {
    name: REPO,
    description: DESCRIPTION,
    private: false,
    has_issues: true,
    has_wiki: false,
    auto_init: false,
  });
  if (created.status !== 201) {
    console.error(`create failed: ${created.status} ${JSON.stringify(created.json)}`);
    process.exit(1);
  }
  console.log(`created: ${created.json.full_name} private=${created.json.private} url=${created.json.html_url}`);
})();
