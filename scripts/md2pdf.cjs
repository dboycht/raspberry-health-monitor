#!/usr/bin/env node
/**
 * md2pdf —— 把一个 Markdown 文件转成排版好的 PDF（**离线、无第三方依赖**）。
 *
 * 为什么自己写而不是装 pandoc：
 *   本机已经有 Edge/Chrome（它们自带"打印成 PDF"能力），而 pandoc + LaTeX 中文链路
 *   配置成本高、体积大。这里只做本项目用到的 Markdown 子集，够用且完全离线：
 *   标题 / 表格 / 有序与无序列表 / 围栏代码块 / 引用 / 分隔线 / 行内粗体与代码。
 *
 * 用法：
 *   node md2pdf.cjs --in <输入.md> --out <输出.pdf> [--title "标题"] [--open]
 *
 * 判据（写完必须验证，别只看退出码）：
 *   ① PDF 文件存在且 **大小 > 10 KB**（太小说明渲染失败，常见于路径写错）；
 *   ② 控制台打印的页数 ≥ 1（用 %PDF 里的 /Count 近似统计）；
 *   ③ 生成 HTML 时中文不能是乱码（headless 渲染用 UTF-8，文件必须显式写 utf8）。
 */

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

// ---------------------------------------------------------------- CLI

function parseArgs(argv) {
  const out = { open: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--in') out.input = argv[++i];
    else if (a === '--out') out.output = argv[++i];
    else if (a === '--title') out.title = argv[++i];
    else if (a === '--html') out.html = argv[++i];
    else if (a === '--open') out.open = true;
  }
  return out;
}

// ---------------------------------------------------------------- Markdown 解析

const escapeHtml = (s) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/** 行内元素：`code`、**bold**、*italic*。先转义再替换，避免注入。 */
function inline(text) {
  let s = escapeHtml(text);
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
  return s;
}

function splitRow(line) {
  // 去掉首尾竖线，按 | 切分（本项目表格里不含转义竖线）
  return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
}

function isTableSeparator(line) {
  return /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && line.includes('-');
}

function convert(markdown, title) {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const html = [];
  let i = 0;
  let listType = null;   // 'ul' | 'ol' | null

  const closeList = () => {
    if (listType) { html.push(`</${listType}>`); listType = null; }
  };

  while (i < lines.length) {
    const line = lines[i];

    // 围栏代码块
    const fence = line.match(/^\s*```(.*)$/);
    if (fence) {
      closeList();
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++; // 跳过结束围栏
      html.push(`<pre><code>${escapeHtml(buf.join('\n'))}</code></pre>`);
      continue;
    }

    // 表格
    if (line.includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      closeList();
      const header = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        rows.push(splitRow(lines[i]));
        i++;
      }
      html.push('<table>');
      html.push('<thead><tr>' + header.map((c) => `<th>${inline(c)}</th>`).join('') + '</tr></thead>');
      html.push('<tbody>');
      for (const r of rows) {
        html.push('<tr>' + r.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>');
      }
      html.push('</tbody></table>');
      continue;
    }

    // 标题
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      closeList();
      const level = h[1].length;
      html.push(`<h${level}>${inline(h[2])}</h${level}>`);
      i++;
      continue;
    }

    // 分隔线
    if (/^\s*(---+|\*\*\*+|___+)\s*$/.test(line)) {
      closeList();
      html.push('<hr>');
      i++;
      continue;
    }

    // 引用
    if (/^\s*>\s?/.test(line)) {
      closeList();
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      html.push('<blockquote>' + buf.map((b) => inline(b)).join('<br>') + '</blockquote>');
      continue;
    }

    // 列表
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ul || ol) {
      const want = ul ? 'ul' : 'ol';
      if (listType !== want) { closeList(); html.push(`<${want}>`); listType = want; }
      html.push(`<li>${inline((ul || ol)[1])}</li>`);
      i++;
      continue;
    }

    // 空行
    if (line.trim() === '') { closeList(); i++; continue; }

    // 普通段落（连续行合并）
    closeList();
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim() !== '' && !/^\s*(#{1,6}\s|>|[-*]\s|\d+\.\s|```)/.test(lines[i])
           && !(lines[i].includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1]))) {
      buf.push(lines[i]);
      i++;
    }
    html.push(`<p>${buf.map((b) => inline(b)).join('<br>')}</p>`);
  }
  closeList();

  const css = `
    @page { size: A4; margin: 14mm 12mm 16mm 12mm; }
    * { box-sizing: border-box; }
    body {
      font-family: "Microsoft YaHei", "微软雅黑", "Noto Sans CJK SC", "PingFang SC", sans-serif;
      font-size: 10.5pt; line-height: 1.55; color: #1a1a1a; margin: 0;
    }
    h1 { font-size: 19pt; margin: 0 0 10px; padding-bottom: 6px; border-bottom: 3px solid #1f77b4; color: #0b3d5c; }
    h2 { font-size: 14.5pt; margin: 18px 0 8px; padding: 5px 8px; background: #eef5fb;
         border-left: 5px solid #1f77b4; color: #0b3d5c; page-break-after: avoid; }
    h3 { font-size: 12pt; margin: 14px 0 6px; color: #14507a; page-break-after: avoid; }
    h4 { font-size: 11pt; margin: 12px 0 5px; color: #14507a; }
    p { margin: 6px 0; }
    table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 9.5pt; }
    th, td { border: 1px solid #b9c9d6; padding: 4px 6px; text-align: left; vertical-align: top; }
    th { background: #dfeaf4; font-weight: 600; }
    tr { page-break-inside: avoid; }
    thead { display: table-header-group; }
    code { font-family: Consolas, "Courier New", monospace; background: #f2f4f6;
           padding: 1px 4px; border-radius: 3px; font-size: 9pt; }
    pre { background: #f6f8fa; border: 1px solid #d6dde4; border-radius: 4px;
          padding: 8px 10px; overflow-wrap: anywhere; white-space: pre-wrap; page-break-inside: avoid; }
    pre code { background: none; padding: 0; }
    blockquote { margin: 8px 0; padding: 6px 10px; background: #fff8e6;
                 border-left: 4px solid #e8b84b; color: #5a4a1a; }
    ul, ol { margin: 6px 0 6px 22px; padding: 0; }
    li { margin: 2px 0; }
    hr { border: none; border-top: 1px dashed #b9c9d6; margin: 14px 0; }
    strong { color: #b3261e; }
    @media print { h2 { break-after: avoid; } }
  `;

  return `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>${escapeHtml(title)}</title><style>${css}</style></head>
<body>${html.join('\n')}</body></html>`;
}

// ---------------------------------------------------------------- PDF

function findBrowser() {
  const cands = [
    process.env.MD2PDF_BROWSER,
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    '/usr/bin/chromium', '/usr/bin/google-chrome', '/usr/bin/microsoft-edge',
  ].filter(Boolean);
  for (const c of cands) if (fs.existsSync(c)) return c;
  throw new Error('找不到 Chrome/Edge（可用 MD2PDF_BROWSER 环境变量指定路径）');
}

function printToPdf(browser, htmlPath, pdfPath) {
  const profile = path.join(require('os').tmpdir(), `md2pdf-profile-${process.pid}`);
  const base = [
    '--disable-extensions', '--no-first-run', '--no-default-browser-check',
    '--disable-gpu', `--user-data-dir=${profile}`,
  ];
  const common = [
    `--print-to-pdf=${pdfPath}`,
    '--no-pdf-header-footer',
    '--print-to-pdf-no-header',
    '--virtual-time-budget=8000',
  ];
  // 新版 headless 用 --headless=new；老版用 --headless。两个都试，谁成功算谁。
  const attempts = [
    ['--headless=new', ...base, ...common, htmlPath],
    ['--headless', ...base, ...common, htmlPath],
  ];
  let lastErr = null;
  for (const args of attempts) {
    try {
      execFileSync(browser, args, { stdio: 'ignore', timeout: 120000 });
      if (fs.existsSync(pdfPath) && fs.statSync(pdfPath).size > 10000) {
        return args[0];
      }
      lastErr = new Error(`渲染后文件缺失或过小（${args[0]}）`);
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error('打印 PDF 失败');
}

/** 粗略统计页数：PDF 里 /Type /Page 的出现次数（够用即可，不做严格解析）。 */
function countPages(pdfPath) {
  try {
    const raw = fs.readFileSync(pdfPath, 'latin1');
    const m = raw.match(/\/Type\s*\/Page[^s]/g);
    return m ? m.length : 0;
  } catch { return 0; }
}

// ---------------------------------------------------------------- main

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.input || !args.output) {
    console.error('用法: node md2pdf.cjs --in <输入.md> --out <输出.pdf> [--title 标题] [--html 中间件.html] [--open]');
    process.exit(2);
  }
  const mdPath = path.resolve(args.input);
  const pdfPath = path.resolve(args.output);
  if (!fs.existsSync(mdPath)) { console.error(`输入不存在：${mdPath}`); process.exit(2); }

  const markdown = fs.readFileSync(mdPath, 'utf8');
  const title = args.title || (markdown.match(/^#\s+(.+)$/m) || [, path.basename(mdPath)])[1];
  const html = convert(markdown, title);

  const htmlPath = args.html ? path.resolve(args.html) : pdfPath.replace(/\.pdf$/i, '.html');
  fs.writeFileSync(htmlPath, html, 'utf8');   // 显式 utf8，否则中文会乱码

  // ------------------------------------------------------------------
  // 覆盖已存在的输出：**先删后写**，但删不掉时要给出可操作的建议
  // ⚠️ 2026-09-24 实测踩到：用 Adobe Acrobat / Edge 打开着目标 PDF 时，
  //    文件被独占锁住，unlinkSync 直接抛 EBUSY 且堆栈很难看懂。
  //    正确姿势是：**先渲染到临时文件，再替换**；替换失败就把临时文件路径给用户，
  //    而不是粗暴地去结束别人的阅读器进程。
  // ------------------------------------------------------------------
  const outDir = path.dirname(pdfPath);
  fs.mkdirSync(outDir, { recursive: true });
  const rendered = path.join(outDir, `.md2pdf-${process.pid}.tmp.pdf`);
  try { if (fs.existsSync(rendered)) fs.unlinkSync(rendered); } catch {}

  const browser = findBrowser();
  const flag = printToPdf(browser, htmlPath, rendered);

  let replaced = true;
  try {
    fs.copyFileSync(rendered, pdfPath);
  } catch (e) {
    replaced = false;
    console.error(`⚠️ 无法覆盖目标文件（多半是被 PDF 阅读器占用）：${path.basename(pdfPath)}`);
    console.error('   → 请关闭阅读器里打开的这个文件后重跑；本次结果保留在：');
    console.error(`   → ${rendered}`);
  }
  if (replaced) { try { fs.unlinkSync(rendered); } catch {} }

  const finalPath = replaced ? pdfPath : rendered;
  const size = fs.statSync(finalPath).size;
  const pages = countPages(finalPath);
  console.log(`✅ 已生成 PDF：${finalPath}`);
  console.log(`   渲染器：${path.basename(browser)} ${flag}`);
  console.log(`   体积：${(size / 1024).toFixed(1)} KB；页数（近似）：${pages}`);
  console.log(`   中间 HTML：${htmlPath}`);

  if (size < 10000) { console.error('⚠️ 文件过小，可能渲染失败'); process.exit(1); }
  if (pages < 1) { console.error('⚠️ 页数统计为 0，请人工打开确认'); }
  if (!replaced) process.exit(1);

  if (args.open) {
    try { execFileSync('cmd', ['/c', 'start', '', pdfPath], { stdio: 'ignore' }); } catch {}
  }
}

main();
