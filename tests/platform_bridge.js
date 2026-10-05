// 在 node 里跑平台自己的规则引擎，供 tests/test_platform_parity.py 做对照。
// 用法：node tests/platform_bridge.js src/doudizhu-rules.js < payload.json
'use strict';
const R = require(process.argv[2]);
const RANK_PROBES = [0, 51, 52, 53];
let raw = '';
process.stdin.on('data', (chunk) => { raw += chunk; });
process.stdin.on('end', () => {
    const payload = JSON.parse(raw);
    const out = {classify: [], beats: [], keys: [], ranks: RANK_PROBES.map((c) => R.rank(c))};
    for (const cards of payload.hands) {
        out.classify.push(R.classify(cards).map((p) => ({type: p.type, rank: p.rank, len: p.len})));
    }
    for (const [a, b] of payload.beats) out.beats.push(R.beats(a, b));
    for (const p of payload.keys) out.keys.push(R.patternKey(p));
    process.stdout.write(JSON.stringify(out));
});
