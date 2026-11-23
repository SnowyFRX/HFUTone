"""用 CDP 连上 QtWebEngine，问清楚"点击时的框"到底是什么。

启动窗口时带 --remote-debugging-port=9222，然后跑这个脚本：
  python tools/inspect_focus.py

它会：聚焦一个按钮 → 读 computed outline / box-shadow → 再检查页面里
还有哪些元素带非零 outline。结果打印成 JSON，别猜。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

import yktws                                    # noqa: E402

CDP = 'http://127.0.0.1:9222'


def open_page() -> str:
    for _ in range(20):
        try:
            raw = urllib.request.urlopen(CDP + '/json', timeout=2).read()
            targets = json.loads(raw)
            for t in targets:
                if t.get('type') == 'page' and t.get('webSocketDebuggerUrl'):
                    return t['webSocketDebuggerUrl']
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    raise SystemExit('连不上 CDP（窗口是不是没带 --remote-debugging-port=9222？）')


class Cdp:
    def __init__(self, url: str):
        self.ws = yktws.WS(url, {}, timeout=10)
        self.ws.connect()
        self.n = 0

    def call(self, method: str, **params):
        self.n += 1
        mid = self.n
        self.ws.send_text(json.dumps({'id': mid, 'method': method, 'params': params}))
        deadline = time.time() + 10
        while time.time() < deadline:
            msg = self.ws.recv_json(timeout=5)
            if not msg:
                continue
            if msg.get('id') == mid:
                if 'error' in msg:
                    raise SystemExit(f'CDP 报错: {msg["error"]}')
                return msg.get('result')
        raise SystemExit('CDP 超时')

    def js(self, expr: str):
        r = self.call('Runtime.evaluate', expression=expr,
                      returnByValue=True, awaitPromise=True)
        return r.get('result', {}).get('value')

    def close(self):
        try:
            self.ws.close()
        except OSError:
            pass


PROBE = r"""
(() => {
  const out = {};
  const btn = [...document.querySelectorAll('button, summary')]
      .find(b => (b.textContent || '').includes('刷新课程'))
      || document.querySelector('button');
  if (!btn) return JSON.stringify({error: '没找到按钮'});

  const before = getComputedStyle(btn);
  btn.focus();
  const after = getComputedStyle(btn);
  const focused = {
    text: (btn.textContent || '').trim().slice(0, 12),
    matchesFocus: btn.matches(':focus'),
    matchesFocusVisible: (function(){ try { return btn.matches(':focus-visible'); } catch (e) { return 'unsupported'; } })(),
    outline: after.outline,
    outlineStyle: after.outlineStyle,
    outlineWidth: after.outlineWidth,
    outlineColor: after.outlineColor,
    outlineOffset: after.outlineOffset,
    boxShadow: after.boxShadow,
    border: after.border,
    background: after.backgroundColor,
    borderRadius: after.borderRadius,
  };
  out.focused = focused;
  out.beforeFocus = {outline: before.outline, boxShadow: before.boxShadow};
  btn.blur();

  // 页面上所有带非零 outline 的元素（不管有没有焦点）
  const ringed = [];
  for (const el of document.querySelectorAll('*')) {
    const s = getComputedStyle(el);
    if (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) {
      ringed.push({tag: el.tagName, cls: String(el.className).slice(0, 40),
                   outline: s.outline});
    }
    if (ringed.length > 12) break;
  }
  out.elementsWithOutline = ringed;

  // 焦点相关规则命中了哪些（只看 button / summary / tab）
  const rules = [];
  for (const sheet of document.styleSheets) {
    let list;
    try { list = sheet.cssRules; } catch (e) { continue; }
    for (const rule of list || []) {
      const txt = rule.cssText || '';
      if (/focus|active/.test(txt) && /outline|shadow|border-color/.test(txt)) {
        rules.push(txt.slice(0, 160));
      }
    }
  }
  out.focusRules = rules;
  return JSON.stringify(out, null, 1);
})()
"""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass
    url = open_page()
    cdp = Cdp(url)
    try:
        print(cdp.js(PROBE))
    finally:
        cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())