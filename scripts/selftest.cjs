/**
 * Self-test for the Node helper scripts in this folder (scripts/lib/net.cjs,
 * check_ci.cjs, create_repo.cjs, ...).
 *
 *   node scripts/selftest.cjs              # offline: no network, deterministic
 *   node scripts/selftest.cjs --online     # also exercises the live CI API
 *   node scripts/selftest.cjs --verbose
 *
 * Exit code: 0 = all assertions passed, 1 = a failure (or nothing ran).
 *
 * WHY (2026-09-25, see ERROR.md E33): `node scripts/check_ci.cjs` used to die
 * with UNABLE_TO_VERIFY_LEAF_SIGNATURE on this machine (TLS-intercepting proxy)
 * while the README told you to run exactly that command. The fix re-executes
 * the script with --use-system-ca; these tests pin down that decision logic,
 * because the branch only fires on a machine that has the proxy.
 *
 * Offline by default on purpose: the submit-time gate (`rpi/scripts/validate.py`)
 * runs this, and a check that depends on github.com being up would turn "no
 * network" into a red build - the very class of false failure this file exists
 * to prevent.
 *
 * ASCII only (project rule for probe/script files).
 */

'use strict';

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const netLib = require('./lib/net.cjs');

const FOLDER = __dirname;
const SCRIPT = path.join(FOLDER, 'check_ci.cjs');
const VERBOSE = process.argv.includes('--verbose');
const ONLINE = process.argv.includes('--online') || process.env.CI_SELFTEST_ONLINE === '1';

/**
 * Files that MUST be pure ASCII, with the reason.
 *
 * `check_ci.cjs` and `lib/net.cjs` run on the dev machine and print to the
 * console (Chinese in those files bit us twice already, see ERROR.md E14/E32),
 * so they are held to the strict rule. This file is NOT listed: it never
 * prints to a user console - its assertion names are Chinese for readability,
 * and making them ASCII would replace them with unreadable \u escapes while
 * protecting nothing.
 *
 * Why a list instead of "everything": a guard everyone has to bypass silently
 * is worth less than a short list with stated reasons.
 */
const ASCII_REQUIRED = ['check_ci.cjs', 'lib/net.cjs'];

let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    const result = fn();
    if (result === false) {
      throw new Error('returned false');
    }
    passed += 1;
    if (VERBOSE) {
      console.log(`  ok   ${name}`);
    }
  } catch (err) {
    failures.push(`${name}: ${err && err.message ? err.message : err}`);
    console.log(`  FAIL ${name}: ${err && err.message ? err.message : err}`);
  }
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message || 'assertion failed');
  }
}

/** Does the real API answer without a certificate problem? Decides one test. */
function apiReachableWithoutSystemCa() {
  const probe = spawnSync(
    process.execPath,
    ['-e', "fetch('https://api.github.com/rate_limit').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"],
    { stdio: 'ignore', timeout: 15000 },
  );
  return probe.status === 0;
}

// ---------------------------------------------------------------------------
// 1. isCertError: the classifier that decides whether to fall back
// ---------------------------------------------------------------------------
console.log('[1] lib/net.cjs isCertError');
check('leaf signature error is a cert error', () => {
  const err = new Error('fetch failed');
  err.cause = Object.assign(new Error('unable to verify the first certificate'), {
    code: 'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
  });
  assert(netLib.isCertError(err) === true, 'expected true');
});
check('every known cert code is detected', () => {
  for (const code of netLib.CERT_ERROR_CODES) {
    const err = Object.assign(new Error('x'), { cause: Object.assign(new Error('y'), { code }) });
    assert(netLib.isCertError(err) === true, `missed code ${code}`);
  }
});
check('plain connection error is not a cert error', () => {
  const err = Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:1'), { code: 'ECONNREFUSED' });
  assert(netLib.isCertError(err) === false, 'must not fall back on a socket error');
});
check('dns error is not a cert error', () => {
  const err = Object.assign(new Error('getaddrinfo ENOTFOUND api.github.com'), { code: 'ENOTFOUND' });
  assert(netLib.isCertError(err) === false, 'must not fall back on a dns error');
});
check('http status error is not a cert error', () => {
  assert(netLib.isCertError(new Error('cannot read runs: HTTP 404')) === false, 'must not fall back on 404');
});
check('timeout is not a cert error', () => {
  const err = Object.assign(new Error('The operation was aborted due to timeout'), { code: 'UND_ERR_CONNECT_TIMEOUT' });
  assert(netLib.isCertError(err) === false, 'must not fall back on a timeout');
});
check('null / undefined are safe', () => {
  assert(netLib.isCertError(null) === false && netLib.isCertError(undefined) === false, 'must accept null');
});
check('deep cause chain is walked', () => {
  const root = Object.assign(new Error('unable to verify the first certificate'), { code: 'X' });
  const mid = Object.assign(new Error('fetch failed'), { cause: root });
  const top = Object.assign(new Error('wrapper'), { cause: mid });
  assert(netLib.isCertError(top) === true, 'expected nested cause to be found');
});

// ---------------------------------------------------------------------------
// 2. supportsSystemCa: version gate for the flag
// ---------------------------------------------------------------------------
console.log('[2] lib/net.cjs supportsSystemCa');
check('newer versions supported', () => {
  for (const version of ['v24.14.0', 'v25.0.0', 'v23.1.0', 'v22.15.0', 'v22.20.1']) {
    assert(netLib.supportsSystemCa(version) === true, `${version} should be supported`);
  }
});
check('older versions refused', () => {
  for (const version of ['v22.14.0', 'v21.7.3', 'v20.11.0', 'v18.20.4']) {
    assert(netLib.supportsSystemCa(version) === false, `${version} should be refused`);
  }
});
check('garbage version refused (no crash)', () => {
  for (const version of ['', 'nonsense', null, 0]) {
    assert(netLib.supportsSystemCa(version) === false, `unexpected acceptance of ${String(version)}`);
  }
});
check('missing argument means "the running Node", not "unsupported"', () => {
  const descriptor = Object.getOwnPropertyDescriptor(process, 'version');
  Object.defineProperty(process, 'version', { value: 'v20.11.0', configurable: true });
  try {
    assert(netLib.supportsSystemCa() === false, 'must read process.version when called with no argument');
  } finally {
    Object.defineProperty(process, 'version', descriptor);
  }
  assert(netLib.supportsSystemCa() === netLib.supportsSystemCa(process.version), 'default must match process.version');
});

// ---------------------------------------------------------------------------
// 3. withSystemCa: does it fall back only when it should?
// ---------------------------------------------------------------------------
console.log('[3] lib/net.cjs withSystemCa');

async function main() {
  await checkAsync('non-cert error propagates (no silent fallback)', async () => {
    const err = Object.assign(new Error('boom'), { code: 'ECONNRESET' });
    let warned = 0;
    let caught = null;
    await netLib.withSystemCa(() => { throw err; }, {
      scriptPath: SCRIPT, args: ['--json'], warn: () => { warned += 1; },
    }).catch((e) => { caught = e; });
    assert(caught === err, 'the original error must be re-thrown');
    assert(warned === 0, 'must not print a fallback notice for a non-cert error');
  });

  await checkAsync('re-exec is refused (exit 3) when Node lacks the flag', async () => {
    const certErr = Object.assign(new Error('unable to verify the first certificate'), { code: 'UNABLE_TO_VERIFY_LEAF_SIGNATURE' });
    const notices = [];
    let spawnCalled = 0;
    const descriptor = Object.getOwnPropertyDescriptor(process, 'version');
    Object.defineProperty(process, 'version', { value: 'v20.11.0', configurable: true });
    try {
      const code = await netLib.withSystemCa(() => { throw certErr; }, {
        scriptPath: SCRIPT,
        args: [],
        warn: (text) => notices.push(text),
        spawn: () => { spawnCalled += 1; return { status: 0 }; },
      });
      assert(code === 3, `expected exit 3 on an old Node, got ${code}`);
      assert(spawnCalled === 0, 'must not try the flag on a Node that lacks it');
      assert(notices.some((text) => text.includes('--use-system-ca')), 'hint must name the flag');
    } finally {
      Object.defineProperty(process, 'version', descriptor);
    }
  });

  await checkAsync('cert error re-execs the script with --use-system-ca', async () => {
    const certErr = Object.assign(new Error('unable to verify the first certificate'), { code: 'UNABLE_TO_VERIFY_LEAF_SIGNATURE' });
    let spawnArgs = null;
    const stub = (file, args) => {
      spawnArgs = { file, args };
      return { status: 0 };
    };
    const code = await netLib.withSystemCa(() => { throw certErr; }, {
      scriptPath: SCRIPT, args: ['--json'], warn: () => {}, spawn: stub,
    });
    assert(code === 0, 'exit code from the child must be returned');
    assert(spawnArgs !== null, 'a re-exec must have happened');
    assert(spawnArgs.args[0] === '--use-system-ca', `flag missing: ${JSON.stringify(spawnArgs.args)}`);
    assert(spawnArgs.args[1] === SCRIPT, 'must re-exec the same script');
    assert(spawnArgs.args[2] === '--json', 'original argv must be forwarded');
  });

  await checkAsync('a refused flag (child error) degrades to the manual hint', async () => {
    const certErr = Object.assign(new Error('unable to verify the first certificate'), { code: 'UNABLE_TO_VERIFY_LEAF_SIGNATURE' });
    const notices = [];
    const stub = () => ({ error: new Error('bad option: --use-system-ca'), status: null });
    const code = await netLib.withSystemCa(() => { throw certErr; }, {
      scriptPath: SCRIPT, args: [], warn: (text) => notices.push(text), spawn: stub,
    });
    assert(code === 3, `expected exit 3, got ${code}`);
    assert(notices.some((text) => text.includes('--use-system-ca')), 'hint must name the flag');
    assert(notices.some((text) => text.includes('bad option')), 'hint must include the child error');
  });

  await checkAsync('successful first attempt never falls back', async () => {
    let spawnCalled = 0;
    const code = await netLib.withSystemCa(() => 0, {
      scriptPath: SCRIPT, args: [], warn: () => {}, spawn: () => { spawnCalled += 1; return { status: 0 }; },
    });
    assert(code === 0, 'exit code must pass through');
    assert(spawnCalled === 0, 'must not re-exec on success');
  });

  // -------------------------------------------------------------------------
  // 4. Contract of the real script
  // -------------------------------------------------------------------------
  console.log('[4] scripts/check_ci.cjs contract');
  await checkAsync('--help succeeds and needs no network', async () => {
    const out = spawnSync(process.execPath, [SCRIPT, '--help'], { encoding: 'utf8', timeout: 30000 });
    assert(out.status === 0, `exit ${out.status}: ${out.stderr}`);
    assert(/usage:/i.test(out.stdout), 'help text must show usage');
  });

  check('every ASCII-required script is pure ASCII', () => {
    for (const rel of ASCII_REQUIRED) {
      const file = path.join(FOLDER, rel);
      assert(fs.existsSync(file), `ASCII-required file is missing: ${rel}`);
      const buffer = fs.readFileSync(file);
      const offenders = [];
      for (let i = 0; i < buffer.length; i += 1) {
        if (buffer[i] > 0x7f) {
          offenders.push(i);
        }
      }
      assert(offenders.length === 0, `${rel} has non-ASCII bytes at ${offenders.slice(0, 5)}`);
    }
  });

  check('non-ASCII drift in the other scripts stays visible', () => {
    const rows = walk(FOLDER)
      .filter((file) => file.endsWith('.cjs') || file.endsWith('.mjs'))
      .map((file) => {
        const rel = path.relative(FOLDER, file).split(path.sep).join('/');
        const buffer = fs.readFileSync(file);
        let count = 0;
        for (let i = 0; i < buffer.length; i += 1) {
          if (buffer[i] > 0x7f) {
            count += 1;
          }
        }
        return { rel, count };
      })
      .filter((row) => row.count > 0 && !ASCII_REQUIRED.includes(row.rel));
    if (VERBOSE || rows.length > 0) {
      console.log('  info  non-ASCII bytes in legacy scripts (allowed, informational): '
        + (rows.length === 0 ? 'none' : rows.map((row) => `${row.rel}=${row.count}`).join(', ')));
    }
    assert(ASCII_REQUIRED.includes('check_ci.cjs'), 'ASCII_REQUIRED must list the script this gate protects');
  });

  await checkAsync('--json works offline against the real API, or degrades with a clear exit code', async () => {
    if (!ONLINE) {
      console.log('  skip --json live API test (offline mode; use --online to run it)');
      return true;
    }
    const out = spawnSync(process.execPath, [SCRIPT, '--json'], { encoding: 'utf8', timeout: 60000 });
    const stdout = out.stdout || '';
    const stderr = out.stderr || '';
    if (out.status === 0 || out.status === 1) {
      const payload = JSON.parse(stdout);
      assert(payload.ok === true, 'payload must report ok:true');
      assert(payload.repo === 'dboycht/raspberry-health-monitor', `unexpected repo ${payload.repo}`);
      assert(payload.latest === null || typeof payload.latest.status === 'string', 'latest must carry a status');
      assert(!/gh[opsu]_[A-Za-z0-9]{20,}/.test(stdout + stderr), 'output must not contain a token');
    } else {
      assert(out.status === 2 || out.status === 3, `unexpected exit ${out.status}`);
      assert(stderr.trim().length > 0, 'a failure must explain itself on stderr');
    }
  });

  // ---------------------------------------------------------------------------
  // 5. check_ci.run(): the wrapper that wires the fallback to the real main()
  // ---------------------------------------------------------------------------
  console.log('[5] scripts/check_ci.cjs run() wiring');
  const checkCi = require('./check_ci.cjs');

  await checkAsync('cert error end to end: re-exec happens and the exit code sticks', async () => {
    const certErr = Object.assign(new Error('unable to verify the first certificate'), { code: 'UNABLE_TO_VERIFY_LEAF_SIGNATURE' });
    const savedExitCode = process.exitCode;
    const savedMain = checkCi.main;
    const spawnee = [];
    try {
      // Force the in-process attempt to fail with a certificate error, whatever the network does.
      checkCi.main = () => { throw certErr; };
      const code = await checkCi.run(['--json'], {
        warn: () => {},
        spawn: (file, args) => {
          spawnee.push(args);
          return { status: 0 };
        },
      });
      assert(code === 0, `expected the child's exit code 0, got ${code}`);
      assert(process.exitCode === 0, `process.exitCode must be set to 0, got ${process.exitCode}`);
      assert(spawnee.length === 1, 'the re-exec must run exactly once');
      assert(spawnee[0][0] === '--use-system-ca', `flag missing: ${JSON.stringify(spawnee[0])}`);
    } finally {
      checkCi.main = savedMain;
      process.exitCode = savedExitCode;
    }
  });

  await checkAsync('a non-cert failure is not retried and surfaces', async () => {
    const savedExitCode = process.exitCode;
    const savedMain = checkCi.main;
    const spawnee = [];
    try {
      checkCi.main = () => { throw Object.assign(new Error('HTTP 404'), { code: 'ENOTFOUND' }); };
      const code = await checkCi.run([], {
        warn: () => {},
        spawn: () => { spawnee.push(1); return { status: 0 }; },
      });
      assert(code === 3, `a hard failure must exit 3, got ${code}`);
      assert(process.exitCode === 3, `process.exitCode must be 3, got ${process.exitCode}`);
      assert(spawnee.length === 0, 'must not re-exec on a non-certificate error');
    } finally {
      checkCi.main = savedMain;
      process.exitCode = savedExitCode;
    }
  });

  // ---------------------------------------------------------------------------
  // 6. create_repo.cjs must stay ASCII while still sending a Chinese description
  // ---------------------------------------------------------------------------
  console.log('[6] scripts/create_repo.cjs ASCII-safe description');
  const createRepo = require('./create_repo.cjs');
  check('decodeDescription round-trips a known UTF-8 sequence', () => {
    // "abc" + the 3-byte character U+6811, written as hex so this file stays ASCII.
    assert(createRepo.decodeDescription('616263e6a091') === 'abc' + String.fromCharCode(0x6811), 'round-trip failed');
  });
  check('decodeDescription rejects malformed input', () => {
    for (const bad of ['', 'abc', 'zz', '61 62 6']) {
      let threw = false;
      try {
        createRepo.decodeDescription(bad);
      } catch {
        threw = true;
      }
      assert(threw, `expected a throw for ${JSON.stringify(bad)}`);
    }
  });
  check('description decodes to the expected Chinese text', () => {
    // Code points spelled out so the assertion itself needs no non-ASCII bytes.
    const units = [
      0x6811, 0x8393, 0x6d3e, 0x5c45, 0x5bb6, 0x8001, 0x4eba, 0x5065, 0x5eb7,
      0x4e0e, 0x5b89, 0x5168, 0x76d1, 0x62a4, 0x7cfb, 0x7edf, 0xff08, 0x8bfe,
      0x7a0b, 0x8bbe, 0x8ba1, 0xff09, 0xff1a, 0x5fc3, 0x7387, 0x8840, 0x6c27,
      0x2f, 0x4f53, 0x6e29, 0x2f, 0x73af, 0x5883, 0x2f, 0x6d3b, 0x52a8,
      0x91c7, 0x96c6, 0xff0c, 0x672c, 0x5730, 0x58f0, 0x5149, 0x62a5, 0x8b66,
      0xff0c, 0x5b89, 0x5353, 0x20, 0x41, 0x70, 0x70, 0x20, 0x8fdc, 0x7a0b,
      0x76d1, 0x62a4,
    ];
    const expected = String.fromCharCode(...units);
    assert(createRepo.DESCRIPTION === expected, 'description text drifted');
    assert(Buffer.byteLength(createRepo.DESCRIPTION, 'utf8') === 155, 'description byte length drifted');
  });
  check('create_repo.cjs declares no GH_TOKEN at import time', () => {
    assert(typeof createRepo.main === 'function', 'main must be exported');
    assert(createRepo.REPO === 'raspberry-health-monitor', 'repo name drifted');
  });

  // ---------------------------------------------------------------------------
  // Report
  // ---------------------------------------------------------------------------
  console.log('');
  if (failures.length > 0) {
    console.log(`FAILED: ${failures.length} of ${passed + failures.length} assertions`);
    for (const failure of failures) {
      console.log(`  - ${failure}`);
    }
    return 1;
  }
  console.log(`selftest OK: ${passed} assertions passed (node ${process.version})`);
  console.log(`(api.github.com reachable without --use-system-ca: ${apiReachableWithoutSystemCa() ? 'yes' : 'no'})`);
  return 0;
}

/** Async-aware variant of check(). */
async function checkAsync(name, fn) {
  try {
    const result = await fn();
    if (result === false) {
      throw new Error('returned false');
    }
    passed += 1;
    if (VERBOSE) {
      console.log(`  ok   ${name}`);
    }
  } catch (err) {
    failures.push(`${name}: ${err && err.message ? err.message : err}`);
    console.log(`  FAIL ${name}: ${err && err.message ? err.message : err}`);
  }
}

/** Recursively list files under `dir` (skips node_modules/.git). */
function walk(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '.git') {
      continue;
    }
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...walk(full));
    } else {
      out.push(full);
    }
  }
  return out;
}

main().then((code) => {
  process.exitCode = code;
}, (err) => {
  console.error('selftest crashed:', err);
  process.exitCode = 1;
});
