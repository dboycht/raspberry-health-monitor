/**
 * Create the GitHub repository if missing, print its state. ASCII only.
 * Uses GitHub REST API (no gh CLI needed).
 *
 *   node scripts/create_repo.cjs
 *
 * Token comes from the GH_TOKEN environment variable (set by the caller from
 * the Windows credential manager). The token is never printed.
 *
 * NOTE: this file is pure ASCII on purpose (project rule for scripts, see
 * ERROR.md E14). The repository description is Chinese, so it is stored as
 * UTF-8 hex bytes and decoded at runtime - editing visible text would need a
 * non-ASCII byte, which the `scripts/selftest.cjs` ASCII scan rejects.
 */
const OWNER = 'dboycht';
const REPO = 'raspberry-health-monitor';

/** Decode UTF-8 hex (e.g. "e69c" -> its character); exported for the selftest. */
function decodeDescription(hex) {
  const clean = String(hex || '').replace(/\s+/g, '');
  if (clean.length === 0 || clean.length % 2 !== 0 || !/^[0-9a-fA-F]+$/.test(clean)) {
    throw new Error('description hex must be a non-empty even-length hex string');
  }
  return Buffer.from(clean, 'hex').toString('utf8');
}

/** Repo description, UTF-8 hex encoded so this file stays ASCII: */
const DESCRIPTION_HEX_LINES = [
  'e6a091e88e93e6b4bee5b185e5aeb6e88081e4babae581a5e5bab7e4b88ee5ae89e585a8e79b91e68aa4e7b3bbe7bb9fefbc88e8afbee7a88be8aebe',
  'e8aea1efbc89efbc9ae5bf83e78e87e8a180e6b0a72fe4bd93e6b8a92fe78eafe5a2832fe6b4bbe58aa8e98787e99b86efbc8ce69cace59cb0e5a3b0',
  'e58589e68aa5e8ada6efbc8ce5ae89e58d932041707020e8bf9ce7a88be79b91e68aa4',
];
const DESCRIPTION = decodeDescription(DESCRIPTION_HEX_LINES.join(''));

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

/** Create the repo when missing; exposed so the selftest can import this file. */
async function main() {
  if (!process.env.GH_TOKEN) {
    console.error('ERROR: GH_TOKEN is not set');
    return 2;
  }

  const check = await api('GET', `/repos/${OWNER}/${REPO}`);
  if (check.status === 200) {
    console.log(`repo already exists: ${check.json.full_name} private=${check.json.private}`);
    console.log(`default_branch=${check.json.default_branch} html_url=${check.json.html_url}`);
    return 0;
  }
  if (check.status !== 404) {
    console.error(`unexpected status checking repo: ${check.status} ${JSON.stringify(check.json)}`);
    return 1;
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
    return 1;
  }
  console.log(`created: ${created.json.full_name} private=${created.json.private} url=${created.json.html_url}`);
  return 0;
}

if (require.main === module) {
  main().then(
    (code) => { process.exitCode = code; },
    (err) => {
      console.error('create_repo failed:', err && err.message ? err.message : err);
      process.exitCode = 1;
    },
  );
}

module.exports = {
  main, api, decodeDescription, DESCRIPTION, DESCRIPTION_HEX_LINES, OWNER, REPO,
};
