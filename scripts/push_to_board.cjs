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
 *     node scripts/push_to_board.cjs                # 真推（默认主机 pi-health）
 *     node scripts/push_to_board.cjs --self-test    # 离线自检（不联网）
 *
 * ⚠️ **绝不推送**：`rpi/config/devices.json`、`rpi/config/devices.local.json`
 * （前者是板子本机配置、后者含 OneNET 密钥；仓库里对应的是 `devices.example.json`）。
 */

'use strict';

const { spawnSync } = require('child_process');
const path = require('path');

/** 允许推送的条目（相对项目根）。目录用 `-r`，文件直接传。 */
const DEFAULT_PLAN = [
  'rpi/health_monitor',
  'rpi/scripts',
  'rpi/tests',
  'rpi/config/devices.example.json', // 只推"示例"，**不推** devices.json
  'rpi/config/stages.json',
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

  let ok = true;
  console.log('板子本机配置（推送后）：');
  for (const rel of PROTECTED) {
    const after = remoteHash(host, rel);
    const same = after === before.get(rel);
    if (!same) ok = false;
    console.log(`  ${same ? '[OK]' : '[X ]'} ${rel} = ${after}（推前 ${before.get(rel)}）`);
  }
  console.log(ok ? '完成：板子本机配置未被改动 ✅' : '⚠️ 板子本机配置发生了变化，请立刻核对！');
  return ok ? 0 : 2;
}

if (require.main === module) {
  process.exit(main(process.argv));
}

module.exports = {
  DEFAULT_PLAN, FORBIDDEN, PROTECTED, REMOTE_ROOT,
  buildPlan, isForbiddenPath, normalize, remoteTarget,
};
