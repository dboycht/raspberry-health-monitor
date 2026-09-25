/**
 * Shared helpers for the Node scripts in this folder.
 *
 * WHY THIS EXISTS (measured 2026-09-25, see ERROR.md E33)
 * ------------------------------------------------------
 * On this machine the dev box sits behind Steam++ (SteamTools), which
 * intercepts TLS and re-signs traffic with a self-signed root CA.
 * Windows trusts that CA, but Node.js ships its own CA bundle and does not,
 * so `fetch()` dies with:
 *
 *     TypeError: fetch failed
 *     [cause]: Error: unable to verify the first certificate
 *       code: 'UNABLE_TO_VERIFY_LEAF_SIGNATURE'
 *
 * Node 22.15+/24 can close that gap with the `--use-system-ca` flag, but the
 * flag must be on the command line (it is rejected inside NODE_OPTIONS), so a
 * script cannot simply enable it for itself. Running the documented command
 * `node scripts/check_ci.cjs` therefore failed while `node --use-system-ca
 * scripts/check_ci.cjs` worked - an environment dependency hidden from the
 * user. The helpers below remove that dependency: try in-process first, and on
 * a *certificate* error re-execute this very script with `--use-system-ca`.
 *
 * Deliberately NOT done: `rejectUnauthorized: false`. Disabling verification
 * would hide real TLS problems; re-exec'ing with the system CA store keeps
 * verification fully on.
 *
 * ASCII only (project rule for probe/script files).
 */

'use strict';

const { spawnSync } = require('node:child_process');

/** Node error codes that mean "TLS chain not trusted by the bundled CA list". */
const CERT_ERROR_CODES = new Set([
  'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
  'SELF_SIGNED_CERT_IN_CHAIN',
  'DEPTH_ZERO_SELF_SIGNED_CERT',
  'UNABLE_TO_GET_ISSUER_CERT',
  'UNABLE_TO_GET_ISSUER_CERT_LOCALLY',
  'CERT_UNTRUSTED',
  'CERT_HAS_EXPIRED',
]);

/**
 * True when the error looks like a TLS trust failure (as opposed to DNS,
 * proxy or connection failures).
 *
 * `fetch` wraps the real cause, so we walk the whole cause chain; matching the
 * message text as well keeps this working across Node versions/proxies.
 */
function isCertError(err) {
  let node = err;
  for (let depth = 0; node && depth < 8; depth += 1) {
    if (typeof node.code === 'string' && CERT_ERROR_CODES.has(node.code)) {
      return true;
    }
    if (typeof node.message === 'string') {
      const text = node.message.toLowerCase();
      if (
        text.includes('unable to verify the first certificate') ||
        text.includes('self-signed certificate') ||
        text.includes('unable to get local issuer certificate') ||
        text.includes('certificate has expired')
      ) {
        return true;
      }
    }
    node = node.cause;
  }
  return false;
}

/** True when this Node supports `--use-system-ca` (added in v22.15 / v23). */
function supportsSystemCa(versionString) {
  // No argument means "the running Node"; anything else must be a real version
  // string (null/'' -> false, i.e. "cannot use the flag", never a crash).
  const source = versionString === undefined ? process.version : versionString;
  const match = /^v(\d+)\.(\d+)\./.exec(String(source === null ? '' : source));
  if (!match) {
    return false;
  }
  const major = Number(match[1]);
  const minor = Number(match[2]);
  if (major >= 24) {
    return true;
  }
  if (major === 23) {
    return true;
  }
  if (major === 22) {
    return minor >= 15;
  }
  return false;
}

/** The message printed when we cannot fall back (old Node / flag refused). */
function manualHint(scriptPath) {
  return (
    'TLS certificate could not be verified and the automatic fallback is not available.\n' +
    '  Fix: run the script as  node --use-system-ca ' + scriptPath + '\n' +
    '  (this machine needs the system CA store because a TLS-intercepting proxy is running)'
  );
}

/**
 * Run `fn`; if it throws a certificate error, re-execute `scriptPath` with
 * `--use-system-ca` and return the child's exit code.
 *
 * @param {() => Promise<number>} fn      in-process attempt, returns exit code
 * @param {object} opts
 * @param {string} opts.scriptPath        __filename of the caller
 * @param {string[]} [opts.args]          process.argv.slice(2) of the caller
 * @param {(text: string) => void} [opts.warn]  how to report the fallback
 * @param {Function} [opts.spawn]         injectable spawner (tests); defaults to spawnSync
 * @returns {Promise<number>} exit code
 */
async function withSystemCa(fn, opts) {
  const warn = (opts && opts.warn) || ((text) => console.error(text));
  const spawn = (opts && opts.spawn) || spawnSync;
  try {
    return await fn();
  } catch (err) {
    if (!isCertError(err)) {
      throw err;
    }
    const scriptPath = (opts && opts.scriptPath) || process.argv[1];
    const args = (opts && opts.args) || process.argv.slice(2);
    if (!supportsSystemCa()) {
      warn(manualHint(scriptPath));
      return 3;
    }
    warn('[tls] certificate not trusted by Node\'s CA bundle; retrying with --use-system-ca');
    const child = spawn(
      process.execPath,
      ['--use-system-ca', scriptPath, ...args],
      { stdio: 'inherit' },
    );
    if (child.error) {
      warn(manualHint(scriptPath) + '\n  (' + child.error.message + ')');
      return 3;
    }
    return child.status === null ? 3 : child.status;
  }
}

module.exports = { isCertError, supportsSystemCa, withSystemCa, manualHint, CERT_ERROR_CODES };
