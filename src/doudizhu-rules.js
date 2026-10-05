// 斗地主 card rules, mirroring web/app/models/UOJDoudizhuRules.php (the server is
// authoritative). Keep both in sync; web/app/tests/doudizhu_rules_parity_test.js
// compares them on random hands.
(function (root) {
    'use strict';
    const ACE = 11, TWO = 12;
    const RANK_NAMES = ['3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A', '2', '小王', '大王'];
    const TYPE_ORDER = ['rocket', 'bomb', 'single', 'pair', 'trio', 'straight', 'pairs', 'plane',
        'trio1', 'trio2', 'four2', 'four22', 'plane1', 'plane2'];
    const TYPE_NAMES = {rocket: '王炸', bomb: '炸弹', single: '单张', pair: '对子', trio: '三张',
        straight: '顺子', pairs: '连对', plane: '飞机', trio1: '三带一', trio2: '三带一对',
        four2: '四带二', four22: '四带两对', plane1: '飞机带单', plane2: '飞机带对'};

    const rank = card => card < 52 ? card >> 2 : card - 39;
    const sortCards = cards => cards.slice().sort((a, b) => a - b);
    function counts(cards) {
        const c = new Array(15).fill(0);
        for (const card of cards) c[rank(card)]++;
        return c;
    }
    function kickersOk(c) {
        if (c[13] && c[14]) return false;
        return c.every(n => n < 4);
    }
    function sequence(c, width, minLen) {
        const ranks = [];
        for (let r = 0; r < 15; r++) {
            if (c[r] === 0) continue;
            if (c[r] !== width) return null;
            ranks.push(r);
        }
        const len = ranks.length, top = ranks[len - 1];
        if (len < minLen || top > ACE || top - ranks[0] !== len - 1) return null;
        return {rank: top, len};
    }

    function classify(cards) {
        const n = cards.length;
        if (!n) return [];
        const c = counts(cards), res = [];
        const add = (type, r, len = 1) => res.push({type, rank: r, len});
        const ofCount = k => c.flatMap((x, r) => x === k ? [r] : []);
        const distinct = c.filter(x => x).length;
        if (n === 2 && c[13] && c[14]) add('rocket', 14);
        if (n === 1) add('single', rank(cards[0]));
        if (n === 2 && ofCount(2).length) add('pair', ofCount(2)[0]);
        if (n === 3 && ofCount(3).length) add('trio', ofCount(3)[0]);
        if (n === 4 && ofCount(4).length) add('bomb', ofCount(4)[0]);
        if (n === 4 && distinct === 2 && ofCount(3).length) add('trio1', ofCount(3)[0]);
        if (n === 5 && distinct === 2 && ofCount(3).length && ofCount(2).length) add('trio2', ofCount(3)[0]);
        for (const [type, width, minLen] of [['straight', 1, 5], ['pairs', 2, 3], ['plane', 3, 2]]) {
            const s = sequence(c, width, minLen);
            if (s) add(type, s.rank, s.len);
        }
        for (const r of ofCount(4)) {
            const rest = c.slice(); rest[r] = 0;
            if (n === 6 && kickersOk(rest)) add('four2', r);
            if (n === 8 && rest.filter(x => x === 2).length === 2 && rest.filter(x => x).length === 2) add('four22', r);
        }
        for (const [unit, type] of [[4, 'plane1'], [5, 'plane2']]) {
            if (n % unit !== 0 || n / unit < 2) continue;
            const k = n / unit;
            for (let s = 0; s + k - 1 <= ACE; s++) {
                const rest = c.slice();
                let ok = true;
                for (let r = s; r < s + k; r++) {
                    if (c[r] !== 3) { ok = false; break; }
                    rest[r] = 0;
                }
                if (!ok) continue;
                if (type === 'plane1') {
                    if (!kickersOk(rest)) continue;
                } else {
                    if (rest.some(x => x !== 0 && x !== 2) || rest.filter(x => x === 2).length !== k) continue;
                }
                add(type, s + k - 1, k);
            }
        }
        res.sort((a, b) => (TYPE_ORDER.indexOf(a.type) - TYPE_ORDER.indexOf(b.type)) || b.rank - a.rank);
        return res;
    }

    function beats(a, b) {
        if (!b) return true;
        if (b.type === 'rocket') return false;
        if (a.type === 'rocket') return true;
        if (a.type === 'bomb') return b.type !== 'bomb' || a.rank > b.rank;
        return a.type === b.type && a.len === b.len && a.rank > b.rank;
    }
    const patternKey = p => `${p.type}:${p.rank}:${p.len}`;
    const describe = p => p.type === 'rocket' ? TYPE_NAMES.rocket : `${TYPE_NAMES[p.type]} ${RANK_NAMES[p.rank]}`;
    const playable = (cards, last) => classify(cards).filter(p => beats(p, last));

    function groups(hand) {
        const g = Array.from({length: 15}, () => []);
        for (const card of sortCards(hand)) g[rank(card)].push(card);
        return g;
    }
    function breaks(g, r, take) {
        if (r >= 13) return g[13].length && g[14].length ? 1 : 0;
        return g[r].length > take ? 1 : 0;
    }
    const cmpKeys = (a, b) => {
        if (a.length !== b.length) return a.length - b.length;
        for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] - b[i];
        return 0;
    };
    function kickers(g, exclude, k, unit) {
        const ranks = [];
        for (let r = 0; r < 15; r++) {
            if (exclude.has(r) || g[r].length < unit || (unit === 2 && r >= 13)) continue;
            ranks.push(r);
        }
        ranks.sort((a, b) => cmpKeys([breaks(g, a, unit), a], [breaks(g, b, unit), b]));
        const out = [];
        if (unit === 2) {
            if (ranks.length < k) return null;
            for (const r of ranks.slice(0, k)) out.push(g[r][0], g[r][1]);
            return out;
        }
        for (let layer = 0; layer < 3 && out.length < k; layer++) {
            for (const r of ranks) if (out.length < k && g[r][layer] !== undefined) out.push(g[r][layer]);
        }
        return out.length === k ? out : null;
    }

    function hints(hand, last) {
        const g = groups(hand), cand = [];
        const push = (cards, key) => cand.push([key, cards]);
        if (!last) {
            g.forEach((cards, r) => {
                if (!cards.length || (r >= 13 && g[13].length && g[14].length)) return;
                push(cards, [cards.length === 4 ? 1 : 0, r]);
            });
        } else {
            const t = last.type, R = last.rank, L = last.len;
            const width = {single: 1, pair: 2, trio: 3, trio1: 3, trio2: 3}[t];
            if (width) {
                for (let r = R + 1; r <= 14; r++) {
                    if (g[r].length < width) continue;
                    let body = g[r].slice(0, width);
                    if (t === 'trio1' || t === 'trio2') {
                        const kick = kickers(g, new Set([r]), 1, t === 'trio1' ? 1 : 2);
                        if (!kick) continue;
                        body = body.concat(kick);
                    }
                    push(body, [0, breaks(g, r, width), r]);
                }
            }
            const seq = {straight: 1, pairs: 2, plane: 3, plane1: 3, plane2: 3}[t];
            if (seq) {
                outer: for (let top = R + 1; top <= ACE; top++) {
                    const start = top - L + 1;
                    if (start < 0) continue;
                    let body = [], broken = 0;
                    const exclude = new Set();
                    for (let r = start; r <= top; r++) {
                        if (g[r].length < seq) continue outer;
                        body = body.concat(g[r].slice(0, seq));
                        exclude.add(r);
                        broken += breaks(g, r, seq);
                    }
                    if (t === 'plane1' || t === 'plane2') {
                        const kick = kickers(g, exclude, L, t === 'plane1' ? 1 : 2);
                        if (!kick) continue;
                        body = body.concat(kick);
                    }
                    push(body, [0, broken ? 1 : 0, top]);
                }
            }
            if (t === 'four2' || t === 'four22') {
                for (let r = R + 1; r <= TWO; r++) {
                    if (g[r].length !== 4) continue;
                    const kick = kickers(g, new Set([r]), 2, t === 'four2' ? 1 : 2);
                    if (kick) push(g[r].concat(kick), [0, 0, r]);
                }
            }
            if (t !== 'rocket') {
                for (let r = 0; r <= TWO; r++) {
                    if (g[r].length === 4 && (t !== 'bomb' || r > R)) push(g[r], [1, 0, r]);
                }
            }
        }
        if (g[13].length && g[14].length && (!last || last.type !== 'rocket')) push([g[13][0], g[14][0]], [2, 0, 14]);
        // Array.prototype.sort is stable, matching PHP 8's usort on the same keys.
        cand.sort((a, b) => cmpKeys(a[0], b[0]));
        const out = [], seen = new Set();
        for (const [, raw] of cand) {
            const cards = sortCards(raw), id = cards.join(',');
            if (seen.has(id) || !playable(cards, last).length) continue;
            seen.add(id);
            out.push(cards);
        }
        return out;
    }

    function choose(cards, last, choice) {
        const options = playable(cards, last);
        return options.find(p => choice && patternKey(p) === choice) || options[0] || null;
    }

    // Longest run of consecutive ranks (up to A) holding at least `width` cards
    // each, taking one `width`-sized slice per rank; null if shorter than minLen.
    function longestRun(cards, width, minLen) {
        const g = groups(cards);
        let best = null;
        for (let s = 0; s <= ACE; s++) {
            let e = s;
            while (e <= ACE && g[e].length >= width) e++;
            if (e - s >= minLen && (!best || e - s > best[1] - best[0])) best = [s, e];
        }
        if (!best) return null;
        const out = [];
        for (let r = best[0]; r < best[1]; r++) out.push(...g[r].slice(0, width));
        return out;
    }

    /**
     * What a drag across `swept` (a contiguous stretch of the hand) should select.
     * Following, the weakest play inside it that beats `last`; leading, the sweep
     * itself when it is valid, else the longest straight / pair run / plane in it,
     * so sweeping 3 4 4 5 6 7 8 selects 3 4 5 6 7 8.
     */
    function smartSelect(swept, last) {
        if (last) {
            const h = hints(swept, last);
            return h.length ? h[0] : swept;
        }
        if (classify(swept).length) return swept;
        const runs = [longestRun(swept, 3, 2), longestRun(swept, 2, 3), longestRun(swept, 1, 5)].filter(Boolean);
        runs.sort((a, b) => b.length - a.length);
        return runs.length ? runs[0] : swept;
    }

    const api = {rank, sortCards, counts, classify, beats, patternKey, describe, playable, hints, choose,
        smartSelect, RANK_NAMES, TYPE_NAMES};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.DoudizhuRules = api;
})(this);
