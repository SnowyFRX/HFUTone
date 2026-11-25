/* 用 node 直接跑生产代码 web/cxgap.js，对固化的真实验证码样本做回归。
   跑法：node tests/cxgap_runner.mjs   （输出 JSON，供 tests/test_cxgap.py 断言）

   为什么要自带一个 PNG 解码器：node 内置没有图像库，而安卓端同样没有
   （chaquopy 只有标准库），所以生产代码吃的是 canvas 给的 RGBA 像素。
   这里把样本存成 PNG（lossless，不会改动像素），用 zlib 解出同样的
   ImageData 结构，喂给同一份 cxgap.js。 */
import { inflateSync } from 'node:zlib';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');
const FIX = join(HERE, 'fixtures', 'captcha');

function decodePng(path) {
  const buf = readFileSync(path);
  let off = 8;
  let width = 0, height = 0, bitDepth = 0, colorType = 0;
  const idat = [];
  while (off + 8 <= buf.length) {
    const len = buf.readUInt32BE(off);
    const type = buf.toString('ascii', off + 4, off + 8);
    const data = buf.subarray(off + 8, off + 8 + len);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data[8];
      colorType = data[9];
      if (data[12] !== 0) { throw new Error('不支持 interlaced PNG'); }
    } else if (type === 'IDAT') {
      idat.push(data);
    } else if (type === 'IEND') {
      break;
    }
    off += 12 + len;
  }
  if (bitDepth !== 8 || (colorType !== 2 && colorType !== 6)) {
    throw new Error(`只支持 8bit RGB/RGBA（colorType=${colorType} bitDepth=${bitDepth}）`);
  }
  const raw = inflateSync(Buffer.concat(idat));
  const bpp = colorType === 6 ? 4 : 3;
  const stride = width * bpp;
  const out = new Uint8ClampedArray(width * height * 4);
  let prev = Buffer.alloc(stride);
  for (let y = 0; y < height; y++) {
    const base = y * (stride + 1);
    const ft = raw[base];
    const line = Buffer.from(raw.subarray(base + 1, base + 1 + stride));
    for (let i = 0; i < stride; i++) {
      const a = i >= bpp ? line[i - bpp] : 0;
      const b = prev[i];
      const c = i >= bpp ? prev[i - bpp] : 0;
      let v = line[i];
      if (ft === 1) { v = (v + a) & 255; }
      else if (ft === 2) { v = (v + b) & 255; }
      else if (ft === 3) { v = (v + ((a + b) >> 1)) & 255; }
      else if (ft === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
        const pr = (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c);
        v = (v + pr) & 255;
      }
      line[i] = v;
    }
    for (let x = 0; x < width; x++) {
      const si = x * bpp, di = (y * width + x) * 4;
      out[di] = line[si];
      out[di + 1] = line[si + 1];
      out[di + 2] = line[si + 2];
      out[di + 3] = bpp === 4 ? line[si + 3] : 255;
    }
    prev = line;
  }
  return { width, height, data: out };
}

const sandbox = { window: {} };
vm.runInNewContext(readFileSync(join(ROOT, 'web', 'cxgap.js'), 'utf8'), sandbox);
const CxGap = sandbox.window.CxGap;
if (!CxGap || typeof CxGap.find !== 'function') {
  console.error('cxgap.js 没有导出 CxGap.find');
  process.exit(2);
}

const names = readdirSync(FIX)
  .filter((f) => f.endsWith('_bg.png'))
  .map((f) => f.slice(0, -'_bg.png'.length))
  .sort();

const result = {};
const t0 = Date.now();
for (const name of names) {
  const bg = decodePng(join(FIX, `${name}_bg.png`));
  const sl = decodePng(join(FIX, `${name}_slice.png`));
  const g = CxGap.find(bg.data, bg.width, bg.height, sl.data, sl.width, sl.height);
  result[name] = {
    ok: g.ok, x: g.x, y: g.y, x0: g.x0, w: g.w, h: g.h,
    alpha: g.alpha, resid: g.resid, ratio: g.ratio, reason: g.reason
  };
}
process.stdout.write(JSON.stringify({ elapsedMs: Date.now() - t0, samples: result }, null, 1));