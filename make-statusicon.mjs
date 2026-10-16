// 生成菜单栏图标 ui/statusbar.png + ui/statusbar@2x.png。
// ⚠️ 为什么不能直接把 logo-mark.png 缩小当模板图:模板图**只吃 alpha**,
//    而那朵铬玫瑰整体不透明 ⇒ 缩到 18px 就是**一坨黑块**(我看过,确实是)。
//    这里用亮度反推不透明度,把花瓣的明暗结构留下来。
// ⚠️ 跑完**必须看一眼**:看 36px(@2x 实际尺寸)**放大后**那张,不是 288px 的。
// 用法: NODE_PATH=$(npm root -g) bun make-statusicon.mjs   (在仓库根目录跑)
//      ⚠️ 它读 ./logo-mark.png —— 从 ui/ 拷一份过来,或改下面的路径。

import { chromium } from 'playwright';
import { readFileSync, writeFileSync } from 'fs';
const b64 = readFileSync('logo-mark.png').toString('base64');
const b = await chromium.launch({ executablePath:'/Users/cjw/Library/Caches/ms-playwright/chromium-1243/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing', args:['--no-proxy-server'] });
const p = await b.newPage();
await p.setContent('<canvas id=c></canvas>');
// 浏览器对 PNG 的缩放是带插值的 —— 我们**故意**用两步:先大图取细节,再逐级缩
const out = await p.evaluate(async (src) => {
  const img = new Image();
  await new Promise(r => { img.onload = r; img.src = src; });
  const BIG = 288;
  const c = document.createElement('canvas'); c.width = c.height = BIG;
  const g = c.getContext('2d'); g.drawImage(img, 0, 0, BIG, BIG);
  const d = g.getImageData(0, 0, BIG, BIG).data;
  const mask = new Float32Array(BIG * BIG);
  for (let i = 0; i < BIG * BIG; i++) {
    const a = d[i*4+3] / 255;
    const lum = (0.2126*d[i*4] + 0.7152*d[i*4+1] + 0.0722*d[i*4+2]) / 255;
    // ⚠️ 菜单栏只吃 alpha,所以明暗只能靠**不透明度**表达 ——
    //    亮的地方透(留白),暗的缝隙实(画成墨)。这样小尺寸下还能看出花瓣。
    mask[i] = a * Math.max(0, Math.min(1, (0.94 - lum) * 3.2));
  }
  const mk = (S) => {
    const o = document.createElement('canvas'); o.width = o.height = S;
    const og = o.getContext('2d'); const od = og.createImageData(S, S);
    const k = BIG / S;
    for (let y = 0; y < S; y++) for (let x = 0; x < S; x++) {
      let s = 0, n = 0;
      for (let dy = 0; dy < k; dy++) for (let dx = 0; dx < k; dx++) {
        s += mask[(y*k+dy)*BIG + (x*k+dx)]; n++;
      }
      const i = y*S + x;
      od.data[i*4] = 0; od.data[i*4+1] = 0; od.data[i*4+2] = 0;
      od.data[i*4+3] = Math.round((s/n) * 255);
    }
    og.putImageData(od, 0, 0);
    return o.toDataURL('image/png');
  };
  return { s18: mk(18), s36: mk(36) };
}, 'data:image/png;base64,' + b64);
writeFileSync('statusbar.png', Buffer.from(out.s18.split(',')[1], 'base64'));
writeFileSync('statusbar@2x.png', Buffer.from(out.s36.split(',')[1], 'base64'));
await b.close();
console.log('ok');
