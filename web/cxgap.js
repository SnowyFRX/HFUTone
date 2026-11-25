/* ============================================================================
   HFUTone · 学习通滑块缺口识别

   识别放在前端：验证码是「背景 JPEG + 拼图 PNG」，要先解码。安卓的 chaquopy
   只有标准库（yktqr 就是降级成「没有解码器」），浏览器是两端唯一都能解
   JPEG/PNG 的地方，canvas 取一次像素即可。

   背景上有两个形状：深灰不透明的干扰块、白色半透明的真缺口。拼图 PNG 是
   56×160 竖条，形状只占其中一段，其余全透明，y 与缺口同源，所以只搜 x。

   真缺口 = 原像素混白：B = α*255 + (1-α)*S，实测 α 稳定在 0.26~0.30。
   拟合这条关系的残差，内容一致的位置才小；平坦的草地、天空虽然亮，
   但和拼图块对不上，残差大。

   x：缺口形状在背景图自然像素里的左边界；x0：该形状在拼图竖条里的左留白。
   x - x0 就是拼图块与缺口重合所需的位移，也是提交给服务端的值。
   ========================================================================== */
(function (global) {
  'use strict';

  /* 实测 8 张样本：真缺口 α = 0.262~0.299；误匹配（白色毛发、纯白背景）
     拟合出的 α 会跑到 0.45~0.75。这个区间就是最有效的门控。 */
  var ALPHA_MIN = 0.18;
  var ALPHA_MAX = 0.42;
  /* 次优（排除最优附近 ±6）与最优的残差比。实测真缺口 1.22~1.62，
     误匹配 1.01~1.09。 */
  var RATIO_MIN = 1.05;

  var ALPHA_MASK = 128;   // 拼图 alpha 超过它才算「形状的一部分」

  /* 找出 y 方向同源的那一行，然后一维搜索 x。
     bg / sl 都是 ImageData.data（Uint8ClampedArray，RGBA 四通道）。 */
  function find(bg, bgW, bgH, sl, slW, slH) {
    if (!bg || !sl || !bgW || !slW) {
      return { ok: false, reason: '缺图' };
    }

    // ---- 1) 拼图竖条里，形状实际占了哪块 ----
    var minX = 1e9, maxX = -1, minY = 1e9, maxY = -1;
    for (var y = 0; y < slH; y++) {
      for (var x = 0; x < slW; x++) {
        if (sl[(y * slW + x) * 4 + 3] > ALPHA_MASK) {
          if (x < minX) { minX = x; }
          if (x > maxX) { maxX = x; }
          if (y < minY) { minY = y; }
          if (y > maxY) { maxY = y; }
        }
      }
    }
    if (maxX < 0) {
      return { ok: false, reason: '拼图没有可见区域' };
    }
    var w = maxX - minX + 1;
    var h = maxY - minY + 1;
    if (w >= bgW || minY + h > bgH) {
      return { ok: false, reason: '拼图和背景尺寸不匹配' };
    }

    // ---- 2) 把形状内像素压成三条平铺数组，循环里只做加减乘 ----
    var offs = [];      // 背景索引（不含 x 位移）
    var sr = [];        // 拼图原像素
    var tt = [];        // 255 - S
    var sumTT = 0;
    for (var dy = 0; dy < h; dy++) {
      for (var dx = 0; dx < w; dx++) {
        var si = ((minY + dy) * slW + (minX + dx)) * 4;
        if (sl[si + 3] <= ALPHA_MASK) { continue; }
        // 背景这边的索引不能带 minX：x 是背景图上的搜索起点，
        // 带上 minX 等于把起点又平移一次，结果会整体少一个 x0。
        var bi = ((minY + dy) * bgW + dx) * 4;
        for (var c = 0; c < 3; c++) {
          var s = sl[si + c];
          var t = 255 - s;
          offs.push(bi + c);
          sr.push(s);
          tt.push(t);
          sumTT += t * t;
        }
      }
    }
    var n = sr.length;
    if (!n || sumTT <= 0) {
      return { ok: false, reason: '拼图块太小' };
    }

    // ---- 3) 逐个 x 拟合 B = α*255 + (1-α)*S，取残差最小 ----
    // 残差平方直接由一次遍历的三个量求出：
    //   ΣD² - (ΣD·T)² / ΣT²   （D = B-S，T = 255-S）
    // 这样不用第二遍循环。
    var maxStart = bgW - w;
    var scores = new Float64Array(maxStart + 1);
    var best = Infinity, bestX = -1, bestA = 0;
    for (var x0 = 0; x0 <= maxStart; x0++) {
      var shift = x0 * 4;
      var sumD2 = 0, sumDT = 0;
      for (var k = 0; k < n; k++) {
        var d = bg[offs[k] + shift] - sr[k];
        sumD2 += d * d;
        sumDT += d * tt[k];
      }
      var a = sumDT / sumTT;
      var r = Infinity;
      if (a > 0.05 && a < 0.95) {
        var v = (sumD2 - sumDT * sumDT / sumTT) / n;
        r = v > 0 ? Math.sqrt(v) : 0;
      }
      scores[x0] = r;
      if (r < best) { best = r; bestX = x0; bestA = a; }
    }
    if (bestX < 0) {
      return { ok: false, reason: '没有候选位置' };
    }

    // ---- 4) 分离度：最优位置附近 ±6 之内的第二名不算数 ----
    var second = Infinity;
    var tops = [];
    for (var x1 = 0; x1 <= maxStart; x1++) {
      if (Math.abs(x1 - bestX) > 6 && scores[x1] < second) { second = scores[x1]; }
    }
    for (var x2 = 0; x2 <= maxStart; x2++) {
      if (Math.abs(x2 - bestX) > 6 && isFinite(scores[x2])) {
        tops.push({ x: x2, resid: Math.round(scores[x2] * 10) / 10 });
      }
    }
    tops.sort(function (p, q) { return p.resid - q.resid; });
    tops = tops.slice(0, 3);
    if (!isFinite(second)) { second = best; }
    var ratio = best > 0 ? second / best : 0;

    var alpha = Math.round(bestA * 1000) / 1000;
    var resid = Math.round(best * 10) / 10;
    var ok = (alpha > ALPHA_MIN && alpha < ALPHA_MAX && ratio >= RATIO_MIN);

    var reason = '';
    if (!ok) {
      if (alpha <= ALPHA_MIN || alpha >= ALPHA_MAX) {
        // 拟合出的白化比例不像是「蒙了一层白」，多半落在本来就白的地方
        reason = '白化比例异常 (' + alpha + ')，退回人工';
      } else {
        reason = '候选分离度不足 (' + ratio.toFixed(2) + ')，退回人工';
      }
    }

    return {
      ok: ok,
      x: bestX,          // 缺口形状左边界（背景图自然像素坐标）
      y: minY,           // 缺口形状上边界
      x0: minX,          // 形状在拼图竖条里的左留白
      w: w,
      h: h,
      bgW: bgW,
      bgH: bgH,
      alpha: alpha,
      resid: resid,
      ratio: Math.round(ratio * 100) / 100,
      tops: tops,
      reason: reason
    };
  }

  global.CxGap = {
    find: find,
    ALPHA_MIN: ALPHA_MIN,
    ALPHA_MAX: ALPHA_MAX,
    RATIO_MIN: RATIO_MIN
  };
})(typeof window !== 'undefined' ? window : this);