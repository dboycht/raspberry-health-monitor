#!/usr/bin/env node
/**
 * 把开发机的代码推到树莓派（**白名单推送 + 板子本机配置保护**）。
 *
 * 为什么要这个脚本（2026-09-26 真实事故，见 `ERROR.md` E46）
 * ---------------------------------------------------------
 * 我用 `scp -r rpi/config pi-health:.../rpi/` 图省事，**把板子本机的
 * `config/devices.json` 覆盖成了仓库默认版**，一次丢掉三处现场设置：
 * `onenet.enabled=true`（云上报直接停了）、`status_led.pins.red=12`、
 * `thresholds.re_alert_interval_s=5`。板子上**没有备份**，只能靠零散证据（服务日志）
 * 反推出原值手工恢复。
 *
 * 判据：**"推送"必须白名单**（默认只推代码/文档/示例配置），
 * 并且推完要**证明**板子那两个本机文件没被动过（前后哈希一致）。
 *
 * 用法::
 *
 *     node scripts/push_to_board.cjs --dry-run      # 只打印将推送什么
 *     node scripts/push_to_board.cjs                # 真推（默认主机 pi-health）+ 漂移检查（只报不删）
 *     node scripts/push_to_board.cjs --prune        # 顺手删掉板上"本地已没有"的残留**代码**文件
 *     node scripts/push_to_board.cjs --self-test    # 离线自检（不联网）
 *
 * ⚠️ **绝不推送**：`rpi/config/devices.json`、`rpi/config/devices.local.json`
 * （前者是板子本机配置、后者含 OneNET 密钥；仓库里对应的是 `devices.example.json`）。
 * ⚠️ `--prune` **只删代码类后缀**（`.py/.cjs/.mjs/.js`）且跳过 `__pycache__`/`*.pyc`/
 * 本机配置 —— 板子上的 CSV / 数据库是**运行时产物**，本地没有，绝不能按"本地没有"删掉。
 */

'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

/** 允许推送的条目（相对项目根）。目录用 `-r`，文件直接传。 */
const DEFAULT_PLAN = [
  'rpi/health_monitor',
  'rpi/scripts',
  'rpi/tests',
  'rpi/config/devices.example.json', // 只推"示例"，**不推** devices.json
  'rpi/config/stages.json',
  // ★ 2026-09-28 补：仓库根的 `scripts/`（主机侧 Node 工具）**板子上也要有** ——
  //   因为 `rpi/scripts/validate.py` 的**第 11 项"Node 脚本自检"要在板子上跑**
  //   （它调 `scripts/selftest.cjs` 与 `scripts/push_to_board.cjs`）。
  //   漏了这一项 ⇒ 开发机 11 项全绿、**板子上第 11 项红**（E35/E41 那一类
  //   "守卫在另一台机器上才暴露"）。见 ERROR.md E46 第二段补记。
  'scripts',
  'basic',
  'docs',
  'hardware',
  'README.md',
];

/** 板上路径（相对 ~），与本地相对路径一致（本项目两边目录结构相同）。 */
const REMOTE_ROOT = 'raspberry-health-monitor';

/** 绝不允许出现在推送计划里的板子本机文件（事故就是它们被覆盖）。 */
const FORBIDDEN = [
  'rpi/config/devices.json',
  'rpi/config/devices.local.json',
  'rpi/config/devices.json.stage-bak',
];

/** 推送前后要校验哈希的"板子本机文件"。 */
const PROTECTED = ['rpi/config/devices.json', 'rpi/config/devices.local.json'];

/**
 * 允许被 `--prune` 删掉的**代码类**后缀。
 *
 * ⚠️ 为什么只删代码、绝不删数据/配置：`basic/data/`、`basic/hw/data/`、`rpi/data/` 里
 * 是**板子上的运行时产物**（用户的 CSV / 数据库），本地根本没有 ——
 * 一旦按"本地没有就删"，会把真实采集数据删掉。见 ERROR.md E46 补记之三。
 */
const PRUNABLE_EXT = ['.py', '.cjs', '.mjs', '.js'];

/** 永远不参与 `--prune` 的路径特征（缓存 / 本机配置 / 备份）。 */
const NEVER_PRUNE_HINTS = ['__pycache__', '.pyc', 'devices.json', 'devices.local.json', 'stage-bak'];

/** 是否为 Python 缓存产物（两侧都可能存在，**不参与漂移比较**，否则报告全是噪音）。 */
function isCachePath(rel) {
  const target = normalize(rel);
  return target.includes('__pycache__') || target.endsWith('.pyc');
}

/** 某个相对路径是否**允许**被 prune 删掉。 */
function shouldPrune(rel) {
  const target = normalize(rel);
  if (isForbiddenPath(target)) return false;
  if (NEVER_PRUNE_HINTS.some((hint) => target.includes(hint))) return false;
  return PRUNABLE_EXT.some((ext) => target.endsWith(ext));
}

/**
 * 比较"本地文件清单"与"板上文件清单"（**纯函数**，可离线单测）。
 *
 * 为什么需要它（ERROR.md E46 补记之三）：`scp` **只覆盖、不删除** ——
 * 本地把 `tests/test_x.py` 移到 `tests/sensors/test_x.py` 之后，板子上**两份都在**，
 * unittest 会把同一批用例**收集两次**（真机 `Ran 760` vs 开发机 `Ran 743`）。
 * 更坏的情况是残留的是**旧的、坏的**版本 ⇒ 板子上红一个"本地找不到的文件"，极难排查。
 *
 * @returns {{stale: string[], missing: string[]}} stale = 板上多出来的（本地已无）
 */
function diffFiles(localFiles, remoteFiles) {
  const localSet = new Set(localFiles.map(normalize));
  const remoteSet = new Set(remoteFiles.map(normalize));
  return {
    stale: remoteFiles.map(normalize).filter((f) => !localSet.has(f)).sort(),
    missing: localFiles.map(normalize).filter((f) => !remoteSet.has(f)).sort(),
  };
}

/** 递归列出本地某个目录下的全部文件（相对路径、正斜杠、排序）。 */
function walkDir(root, rel) {
  const base = path.join(root, rel);
  const out = [];
  const walk = (dir, prefix) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const next = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(path.join(dir, entry.name), next);
      else out.push(`${rel}/${next}`);
    }
  };
  if (fs.existsSync(base) && fs.statSync(base).isDirectory()) walk(base, '');
  return out.sort();
}

function normalize(rel) {
  return String(rel).replace(/\\/g, '/').replace(/^\.\//, '').replace(/\/+$/, '');
}

/**
 * 算出这个条目在 `scp` 里的**远端目标**。
 *
 * ⚠️⚠️ 血泪（2026-09-26，本脚本第一版的 bug）：
 * `scp -r <本地目录> host:<远端同名目录>` 时，若远端那个目录**已存在**，
 * scp 会把本地目录**塞进去** ⇒ 板上多出一层嵌套（`rpi/scripts/scripts/…`、
 * `rpi/health_monitor/health_monitor/…`、`rpi/tests/tests/…`）——
 * 轻则文件没更新（我因此跑了一次"没有 --blink"的旧探针），
 * 重则**测试被重复收集**。
 * 正确做法：远端目标一律写**父目录**，让 scp 把 basename 放进去。
 */
function remoteTarget(rel, host = 'pi-health', root = REMOTE_ROOT) {
  const target = normalize(rel);
  const parent = target.includes('/') ? target.slice(0, target.lastIndexOf('/')) : '.';
  return `${host}:${root}/${parent}/`;
}

/** 某个相对路径是否属于"绝不许推送"的本机文件（含其子路径与 `.bak` 变体）。 */
function isForbiddenPath(rel) {
  const target = normalize(rel);
  return FORBIDDEN.some(
    (bad) => target === bad || target.startsWith(bad + '/') || target.startsWith(bad + '.'),
  );
}

/**
 * 校验并规范化推送计划。
 * @returns {{items: string[], errors: string[]}}
 */
function buildPlan(items = DEFAULT_PLAN) {
  const errors = [];
  const out = [];
  for (const raw of items) {
    const rel = normalize(raw);
    if (!rel) continue;
    if (isForbiddenPath(rel)) {
      errors.push(`计划里含板子本机文件（绝不许推）：${rel}`);
      continue;
    }
    // 目录 `rpi/config` 会把 devices.json 一起带过去 —— 这种事也必须拦下
    if (rel === 'rpi/config') {
      errors.push('不许整目录推送 rpi/config（会带上 devices.json / devices.local.json）');
      continue;
    }
    out.push(rel);
  }
  return { items: out, errors };
}

/** 自检：不联网，只验证"该拦的能拦住、该推的没被误伤"。 */
function selfTest() {
  const cases = [];
  const assert = (name, cond) => cases.push({ name, ok: !!cond });

  assert('默认计划不含 devices.json', !buildPlan().items.includes('rpi/config/devices.json'));
  assert('推 devices.json 会被拦下', buildPlan(['rpi/config/devices.json']).errors.length === 1);
  assert(
    '推 devices.local.json 会被拦下',
    buildPlan(['rpi/config/devices.local.json']).errors.length === 1,
  );
  assert('整目录 rpi/config 会被拦下', buildPlan(['rpi/config']).errors.length === 1);
  assert('示例配置可以推', buildPlan(['rpi/config/devices.example.json']).errors.length === 0);
  assert('反斜杠路径也能识别', isForbiddenPath('rpi\\config\\devices.json'));
  assert('代码目录照常可推', buildPlan(['rpi/health_monitor']).items.length === 1);
  assert('子路径同样被拦', isForbiddenPath('rpi/config/devices.json.bak'));
  // ★ 回归（2026-09-26 本脚本自己的 bug）：远端目标必须是**父目录**，
  //   否则 scp 会把本地目录塞进同名远端目录，板上多一层嵌套（文件没更新 / 测试重复收集）。
  assert(
    '目录的远端目标是父目录（不嵌套）',
    remoteTarget('rpi/scripts') === 'pi-health:raspberry-health-monitor/rpi/',
  );
  assert(
    '文件的远端目标也是父目录',
    remoteTarget('README.md') === 'pi-health:raspberry-health-monitor/./',
  );
  assert(
    '两级目录同样只去到父目录',
    remoteTarget('rpi/config/stages.json') === 'pi-health:raspberry-health-monitor/rpi/config/',
  );
  // ★ 2026-09-28 回归（真机实测踩到）：板子上要跑 `validate.py` 的**第 11 项 Node 自检**，
  //   它需要仓库根的 `scripts/` ⇒ 白名单必须带上它，否则"开发机全绿、板子红一项"。
  assert('默认计划包含 scripts/（板子上第 11 项要用）', buildPlan().items.includes('scripts'));
  assert(
    'scripts/ 的远端目标是项目根',
    remoteTarget('scripts') === 'pi-health:raspberry-health-monitor/./',
  );
  assert(
    '默认计划里没有任何被保护的板子本机文件',
    buildPlan().items.every((i) => !isForbiddenPath(i)),
  );
  // ★ 2026-09-28 回归（真机实测踩到）：scp 只覆盖不删除 ⇒ 本地"移动/改名/删除"的旧文件
  //   会留在板上，被 unittest **重复收集**（真机 760 vs 开发机 743）。
  const d = diffFiles(
    ['tests/sensors/test_a.py', 'tests/b.py'],
    ['tests/sensors/test_a.py', 'tests/b.py', 'tests/test_a.py', 'rpi/data/x.csv'],
  );
  assert('漂移检测：认出板上多出的旧文件', d.stale.includes('tests/test_a.py'));
  assert('漂移检测：数据文件也在漂移里（但不会被删）', d.stale.includes('rpi/data/x.csv'));
  assert('漂移检测：本地没有缺失项时 missing 为空', d.missing.length === 0);
  assert(
    '漂移检测：本地有而板上没有 ⇒ 报缺失',
    diffFiles(['tests/x.py'], []).missing.includes('tests/x.py'),
  );
  assert('prune 只删代码类（.py 可以）', shouldPrune('tests/test_a.py'));
  assert('prune 不删数据（.csv 不行）', !shouldPrune('basic/data/sample.csv'));
  assert('prune 不删缓存（__pycache__ 不行）', !shouldPrune('rpi/tests/__pycache__/test_a.pyc'));
  assert('prune 绝不碰本机配置', !shouldPrune('rpi/config/devices.json'));
  assert('缓存不参与漂移比较', isCachePath('rpi/tests/__pycache__/test_a.cpython-313.pyc'));

  const failed = cases.filter((c) => !c.ok);
  for (const c of cases) {
    console.log(`${c.ok ? '  [OK]' : '  [X ]'} ${c.name}`);
  }
  console.log(`self-test: ${cases.length - failed.length}/${cases.length} assertions passed`);
  return failed.length === 0 ? 0 : 1;
}

function sh(cmd, args, opts = {}) {
  const res = spawnSync(cmd, args, { encoding: 'utf8', ...opts });
  if (res.error) throw res.error;
  if (res.status !== 0) {
    const err = (res.stderr || '').trim();
    throw new Error(`${cmd} ${args.join(' ')} 失败（退出码 ${res.status}）：${err}`);
  }
  return (res.stdout || '').trim();
}

/** 取板子上"受保护文件"的哈希（文件不存在就返回 "missing"）。 */
function remoteHash(host, rel) {
  const cmd = `cd ~/${REMOTE_ROOT} && (md5sum ${rel} 2>/dev/null | cut -d' ' -f1 || echo missing)`;
  return sh('ssh', ['-o', 'BatchMode=yes', host, cmd]);
}

/** 取板子上某个目录的文件清单（相对项目根的路径）。 */
function remoteFiles(host, rel) {
  const cmd = `cd ~/${REMOTE_ROOT} && (find ${rel} -type f -print 2>/dev/null | sort || true)`;
  const out = sh('ssh', ['-o', 'BatchMode=yes', host, cmd]);
  return out ? out.split('\n').map((s) => s.trim()).filter(Boolean) : [];
}

/** 删掉板子上某个残留文件（只在 `--prune` 且 `shouldPrune()` 通过时调用）。 */
function remoteDelete(host, rel) {
  sh('ssh', ['-o', 'BatchMode=yes', host, `cd ~/${REMOTE_ROOT} && rm -f ${rel}`]);
}

function main(argv) {
  const args = new Set(argv.slice(2));
  if (args.has('--self-test')) return selfTest();

  const dryRun = args.has('--dry-run');
  const hostIdx = argv.indexOf('--host');
  const host = hostIdx > 0 ? argv[hostIdx + 1] : 'pi-health';
  const root = path.resolve(__dirname, '..');

  const { items, errors } = buildPlan();
  if (errors.length) {
    errors.forEach((e) => console.error('[X] ' + e));
    return 1;
  }

  console.log(`推送目标：${host}:~/${REMOTE_ROOT}`);
  console.log(`计划（${items.length} 项）：`);
  items.forEach((i) => console.log('  - ' + i));

  const before = new Map();
  if (!dryRun) {
    for (const rel of PROTECTED) before.set(rel, remoteHash(host, rel));
    console.log('板子本机配置（推送前）：');
    for (const [rel, hash] of before) console.log(`  ${rel} = ${hash}`);
  } else {
    console.log('（--dry-run：不推送、也不校验哈希）');
    return 0;
  }

  for (const rel of items) {
    const local = path.join(root, rel);
    const remote = remoteTarget(rel, host);
    console.log(`scp ${rel} -> ${remote}`);
    sh('scp', ['-q', '-r', local, remote]);
  }

  // ---- 漂移检查：`scp` 只覆盖、不删除，本地"改名/移动/删除"的旧文件会留在板上 ----
  // （2026-09-28 实测：板上 `rpi/tests/` 顶层留了两个旧位置的副本 ⇒ 用例被收集两次，
  //   真机 `Ran 760` vs 开发机 `Ran 743`。见 ERROR.md E46 补记之三。）
  const prune = args.has('--prune');
  const staleAll = [];
  console.log('文件清单漂移检查（本地 ↔ 板上，只看目录项）：');
  for (const rel of items) {
    if (!fs.statSync(path.join(root, rel)).isDirectory()) continue;
    const localFiles = walkDir(root, rel).filter((f) => !isCachePath(f));
    const remoteList = remoteFiles(host, rel).filter((f) => !isCachePath(f));
    const { stale, missing } = diffFiles(localFiles, remoteList);
    console.log(`  ${rel}：本地 ${localFiles.length} / 板上 ${remoteList.length}`);
    for (const f of stale) {
      const prunable = shouldPrune(f);
      if (prunable) staleAll.push(f);
      console.log(`    [漂移] 板上多出${prunable ? '' : '（非代码类，不动）'}：${f}`);
    }
    for (const f of missing) console.log(`    [缺失] 板子上没有：${f}`);
  }
  if (staleAll.length && prune) {
    console.log('删掉这些残留（--prune）：');
    for (const f of staleAll) {
      remoteDelete(host, f);
      console.log(`    [已删] ${f}`);
    }
  } else if (staleAll.length) {
    console.log(`⚠️ 有 ${staleAll.length} 个残留代码文件会让板子上的测试被重复收集`
      + '（或红一个"本地找不到的文件"）；确认后加 --prune 删除。');
  } else {
    console.log('  [OK] 两侧文件清单一致（无漂移）');
  }

  let ok = true;
  console.log('板子本机配置（推送后）：');
  for (const rel of PROTECTED) {
    const after = remoteHash(host, rel);
    const same = after === before.get(rel);
    if (!same) ok = false;
    console.log(`  ${same ? '[OK]' : '[X ]'} ${rel} = ${after}（推前 ${before.get(rel)}）`);
  }
  console.log(ok ? '完成：板子本机配置未被改动 ✅' : '⚠️ 板子本机配置发生了变化，请立刻核对！');

  // ⚠️ 漂移提醒**必须放在最后一行**（2026-10-01，ERROR.md E73）：
  //    上一版的漂移报告打在中段，而我（和任何用 `tail` / `Select-Object -Last 3` 的人）
  //    只看末尾几行 ⇒ **把那条警告截掉了**，于是"板子上有仓库已删的旧文件"没被发现，
  //    白白在板子上验了一轮"Python 3.13 跑测试失败"（其实是残留文件导入失败）。
  //    判据：**给人看的警告要出现在输出末尾**，否则它等于没打印。
  if (staleAll.length && !prune) {
    console.log(`⚠️ 还有 ${staleAll.length} 个残留代码文件（见上方 [漂移]）：`
      + '板子上跑测试会被它们干扰 ⇒ 确认后加 --prune 清理。');
  }
  return ok ? 0 : 2;
}

if (require.main === module) {
  process.exit(main(process.argv));
}

module.exports = {
  DEFAULT_PLAN, FORBIDDEN, PROTECTED, REMOTE_ROOT, PRUNABLE_EXT,
  buildPlan, isForbiddenPath, normalize, remoteTarget, shouldPrune, diffFiles, walkDir,
};
