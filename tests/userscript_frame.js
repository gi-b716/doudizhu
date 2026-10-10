// 在 node 里跑油猴脚本里的 nextFrame()，供 tests/test_userscript_nextframe.py 做回归。
// 用法：node tests/userscript_frame.js userscript/ddz-copilot.user.js <hidden|visible>
//
// hidden  = 页面在后台标签里：requestAnimationFrame 一次都不触发（真实浏览器就是这样）。
// visible = 前台：rAF 下一帧就触发。
// 两种情况 nextFrame() 都必须在有限时间内 resolve——hidden 下卡死就是那个把整局送进
// 平台托管的 bug（真实对局 12 局，净 −44 分）。
'use strict';
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const mode = process.argv[3] === 'visible' ? 'visible' : 'hidden';
const m = src.match(/\n    function nextFrame\(\) \{[\s\S]*?\n    \}\n/);
if (!m) { console.log(JSON.stringify({error: '在脚本里找不到 nextFrame'})); process.exit(2); }

let rafCalls = 0;
const raf = (cb) => { rafCalls += 1; if (mode === 'visible') setTimeout(cb, 0); return rafCalls; };
const run = new Function('requestAnimationFrame', 'setTimeout', `${m[0]}\n    return nextFrame();`);

const started = Date.now();
let settled = false;
const done = (resolved) => {
    if (settled) return;
    settled = true;
    console.log(JSON.stringify({mode, resolved, ms: Date.now() - started, rafCalls}));
    process.exit(0);
};
run(raf, setTimeout).then(() => done(true), () => done(false));
setTimeout(() => done(false), 1000);
