/**
 * Doc-checker self-test: inject a dangling reference, assert the checker fails,
 * then restore. ASCII only in code; the injected text is written with explicit
 * \u escapes so no shell quoting can eat it.
 *
 *   node _scratch/inject_bad_ref.mjs inject
 *   node _scratch/inject_bad_ref.mjs restore
 */
import { readFileSync, writeFileSync } from 'node:fs';

const TARGET = 'D:/code/DeepSeekHarness/raspberry-health-monitor/docs/README.md';
const MARK = '\u4e34\u65f6\u5f15\u7528'; // 临时引用
const BAD = '\u0064\u006f\u0063\u0073\u002f\u0039\u0039\u002d\u4e0d\u5b58\u5728\u7684\u6587\u6863\u002e\u006d\u0064'; // docs/99-不存在.md
const LINE = `${MARK}\uff1a\`${BAD}\`\n`;
const MODE = process.argv[2] || 'inject';

let text = readFileSync(TARGET, 'utf8');
if (MODE === 'inject') {
  if (text.includes(LINE)) {
    console.log('already injected');
  } else {
    // 放在正文里（不是围栏代码块内），检查器必须抓到
    writeFileSync(TARGET, text + '\n' + LINE, 'utf8');
    console.log('injected dangling reference');
  }
} else {
  writeFileSync(TARGET, text.split(LINE).join(''), 'utf8');
  console.log('restored');
}
