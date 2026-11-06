'use strict';
// Child-process runner for the JavaScript engine. Run as `node _runner.js`.
// Reads ONE JSON request from stdin and writes newline-delimited JSON events to
// stdout, flushing after each so a parent that has to kill us still sees everything
// finished so far. The pattern is only ever passed to `new RegExp` as data; nothing
// from the request is evaluated as code.
const fs = require('fs');

const TEXT_CAP = 300;

function out(obj) {
  const buf = Buffer.from(JSON.stringify(obj) + '\n', 'utf8');
  let off = 0;
  while (off < buf.length) {
    try {
      off += fs.writeSync(1, buf, off);
    } catch (e) {
      if (e.code !== 'EAGAIN') throw e;
    }
  }
}

function clip(s) {
  return s.length <= TEXT_CAP ? s : s.slice(0, TEXT_CAP) + '...';
}

// UTF-16 index -> code point index, so spans agree with Python's.
function cpMap(s) {
  const m = new Int32Array(s.length + 1);
  let c = 0;
  let i = 0;
  while (i < s.length) {
    m[i] = c;
    const hi = s.charCodeAt(i);
    if (hi >= 0xd800 && hi <= 0xdbff && i + 1 < s.length) {
      const lo = s.charCodeAt(i + 1);
      if (lo >= 0xdc00 && lo <= 0xdfff) {
        m[i + 1] = c;
        i += 2;
        c++;
        continue;
      }
    }
    i++;
    c++;
  }
  m[s.length] = c;
  return m;
}

const ALLOWED = 'dgimsuvy';

function compile(req, extra) {
  let flags = '';
  for (const f of req.flags) {
    if (ALLOWED.includes(f)) {
      if (!flags.includes(f)) flags += f;
    } else {
      out({ t: 'error', kind: 'flag', message:
        `flag '${f}' is not supported by javascript (supported: i m s u v y, g and d are always on for matching)` });
      return null;
    }
  }
  for (const f of extra.add) if (!flags.includes(f)) flags += f;
  flags = flags.split('').filter((f) => !extra.remove.includes(f)).join('');
  try {
    return new RegExp(req.pattern, flags);
  } catch (e) {
    out({ t: 'error', kind: 'compile', message: `${e.name}: ${e.message}` });
    return null;
  }
}

function opMatch(req, re) {
  req.subjects.forEach((subject, i) => {
    const map = cpMap(subject);
    const matches = [];
    let truncated = false;
    for (const m of subject.matchAll(re)) {
      if (matches.length >= req.max_matches) {
        truncated = true;
        break;
      }
      const groups = [];
      for (let k = 1; k < m.length; k++) {
        if (m[k] === undefined) {
          groups.push(null);
        } else {
          const [a, b] = m.indices[k];
          groups.push({ start: map[a], end: map[b], text: clip(m[k]) });
        }
      }
      const named = {};
      if (m.groups) {
        for (const [k, v] of Object.entries(m.groups)) named[k] = v === undefined ? null : clip(v);
      }
      matches.push({ start: map[m.index], end: map[m.index + m[0].length],
                     text: clip(m[0]), groups, named });
    }
    out({ t: 'case', i, matches, truncated });
  });
}

function opSub(req, re, sticky) {
  const subject = req.subject;
  const map = cpMap(subject);
  const items = [];
  let truncated = false;
  for (const m of subject.matchAll(re)) {
    if (items.length >= req.max_matches) {
      truncated = true;
      break;
    }
    // Let the real engine expand the template for exactly this match.
    sticky.lastIndex = m.index;
    const r = subject.replace(sticky, req.replacement);
    const tail = subject.length - (m.index + m[0].length);
    items.push({ start: map[m.index], end: map[m.index + m[0].length], text: clip(m[0]),
                 replacement: clip(r.slice(m.index, r.length - tail)) });
  }
  const result = subject.replace(re, req.replacement);
  out({ t: 'sub', matches: items, truncated, result: result.slice(0, 20000),
        result_truncated: result.length > 20000 });
}

function opProbe(req, re) {
  const skipped = new Set();
  for (const item of req.inputs) {
    if (skipped.has(item.group)) continue;
    const subject = item.prefix + item.unit.repeat(item.n) + item.suffix;
    const t0 = performance.now();
    const matched = re.test(subject);
    const ms = performance.now() - t0;
    out({ t: 'probe', group: item.group, n: item.n, length: subject.length, ms, matched });
    if (ms > req.stop_ms) skipped.add(item.group);
  }
}

function main() {
  const req = JSON.parse(fs.readFileSync(0, 'utf8'));
  out({ t: 'hello', engine: 'javascript', version: process.version, v8: process.versions.v8 });
  let extra = { add: ['g', 'd'], remove: ['y'] };
  if (req.op === 'probe') extra = { add: [], remove: ['g', 'y'] };
  const re = compile(req, extra);
  if (re === null) return;
  out({ t: 'compiled' });
  if (req.op === 'match') opMatch(req, re);
  else if (req.op === 'sub') {
    const sticky = compile(req, { add: ['y'], remove: ['g'] });
    opSub(req, re, sticky);
  } else if (req.op === 'probe') opProbe(req, re);
  out({ t: 'done' });
}

main();
