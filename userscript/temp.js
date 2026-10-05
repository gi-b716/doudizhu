// ==UserScript==
// @name         斗地主AI助手 Pro
// @namespace    http://tampermonkey.net/
// @version      3.0
// @description  QOJ.ac 斗地主智能出牌辅助——自动模式、深度策略、队友配合、概率推断、胜率估算
// @author       AI Assistant
// @match        https://qoj.ac/games/doudizhu/game/*
// @match        file://*/doudizhu/*
// @match        file://*/doudizhu*
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    console.log('[DDZ-AI] 脚本开始加载... URL:', location.href);

    // ========================================================================
    // 配置
    // ========================================================================
    const CONFIG = {
        autoPlay: false,
        autoBid: true,
        temperature: 0.25,
        autoDelayMin: 600,
        autoDelayMax: 1800,
        showReasoning: true,
    };

    // ========================================================================
    // 内嵌规则引擎
    // ========================================================================
    const R = (function() {
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
                const t = last.type, RANK = last.rank, L = last.len;
                const width = {single: 1, pair: 2, trio: 3, trio1: 3, trio2: 3}[t];
                if (width) {
                    for (let r = RANK + 1; r <= 14; r++) {
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
                    outer: for (let top = RANK + 1; top <= ACE; top++) {
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
                    for (let r = RANK + 1; r <= TWO; r++) {
                        if (g[r].length !== 4) continue;
                        const kick = kickers(g, new Set([r]), 2, t === 'four2' ? 1 : 2);
                        if (kick) push(g[r].concat(kick), [0, 0, r]);
                    }
                }
                if (t !== 'rocket') {
                    for (let r = 0; r <= TWO; r++) {
                        if (g[r].length === 4 && (t !== 'bomb' || r > RANK)) push(g[r], [1, 0, r]);
                    }
                }
            }
            if (g[13].length && g[14].length && (!last || last.type !== 'rocket')) push([g[13][0], g[14][0]], [2, 0, 14]);
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

        return {rank, sortCards, counts, classify, beats, describe, playable, hints,
            groups, RANK_NAMES, TYPE_NAMES};
    })();

    // ========================================================================
    // 工具函数
    // ========================================================================
    const $ = sel => document.querySelector(sel);
    const $$ = sel => [...document.querySelectorAll(sel)];
    const sleep = ms => new Promise(r => setTimeout(r, ms));

    function cardName(id) {
        const SUITS = ['♠', '♥', '♣', '♦'];
        if (id === 52) return '小王';
        if (id === 53) return '大王';
        return SUITS[id & 3] + R.RANK_NAMES[R.rank(id)];
    }

    // ========================================================================
    // 游戏状态推断
    // ========================================================================
    function inferGameState() {
        try {
            const hand = $$('#ddz-hand .ddz-card[data-card]')
                .map(c => parseInt(c.dataset.card))
                .filter(c => !isNaN(c));

            const myProfile = document.querySelector('.ddz-me .ddz-profile');
            const leftProfile = document.querySelector('.ddz-seat-left .ddz-profile');
            const rightProfile = document.querySelector('.ddz-seat-right .ddz-profile');

            const mySeat = myProfile ? parseInt(myProfile.dataset.seat) : 2;
            const leftSeat = leftProfile ? parseInt(leftProfile.dataset.seat) : 1;
            const rightSeat = rightProfile ? parseInt(rightProfile.dataset.seat) : 0;

            let landlordSeat = null;
            for (const p of [myProfile, leftProfile, rightProfile]) {
                if (p && p.querySelector('.ddz-role-landlord')) {
                    landlordSeat = parseInt(p.dataset.seat);
                }
            }

            const teammateSeat = (landlordSeat !== null)
                ? [0, 1, 2].find(s => s !== mySeat && s !== landlordSeat)
                : null;

            const prevSeat = (mySeat + 2) % 3;
            const nextSeat = (mySeat + 1) % 3;

            const turnProfile = document.querySelector('.ddz-profile.ddz-turn');
            const turnSeat = turnProfile ? parseInt(turnProfile.dataset.seat) : null;
            const isMyTurn = turnSeat === mySeat;

            const role = myProfile && myProfile.querySelector('.ddz-role-landlord') ? 'landlord' : 'farmer';

            const opponentCounts = {};
            document.querySelectorAll('.ddz-profile').forEach(p => {
                const seat = parseInt(p.dataset.seat);
                const countEl = p.querySelector('.ddz-count');
                if (!isNaN(seat) && countEl) {
                    const m = countEl.textContent.match(/(\d+)/);
                    opponentCounts[seat] = m ? parseInt(m[1]) : 17;
                }
            });

            const tablePlays = {};
            document.querySelectorAll('.ddz-play').forEach(play => {
                const seat = parseInt(play.dataset.seat);
                if (isNaN(seat)) return;
                const passEl = play.querySelector('.ddz-pass');
                if (passEl) {
                    tablePlays[seat] = 'pass';
                } else {
                    const cards = [...play.querySelectorAll('.ddz-card[data-card]')]
                        .map(c => parseInt(c.dataset.card));
                    if (cards.length) {
                        const patterns = R.classify(cards);
                        tablePlays[seat] = { cards, pattern: patterns[0] || null };
                    }
                }
            });

            // ===== 核心：isLeading / lastPattern 推断 =====
            let isLeading = true;
            let lastPattern = null;
            let lastPlaySeat = null;

            const actionButtons = $$('#ddz-actions button');
            const isBtnVisible = (btn) => {
                if (!btn) return false;
                const style = window.getComputedStyle(btn);
                return style.display !== 'none' && style.visibility !== 'hidden' && !btn.hidden;
            };
            const isBtnDisabled = (btn) => {
                return btn.disabled || btn.classList.contains('disabled') || btn.getAttribute('aria-disabled') === 'true';
            };
            const passBtn = actionButtons.find(b => {
                const t = b.textContent.trim();
                return (t === '不出' || t.includes('不出')) && isBtnVisible(b);
            });
            const playBtn = actionButtons.find(b => {
                const t = b.textContent.trim();
                return (t === '出牌' || t.includes('出牌')) && isBtnVisible(b);
            });
            const hintBtn = actionButtons.find(b => {
                const t = b.textContent.trim();
                return (t === '提示' || t.includes('提示')) && isBtnVisible(b);
            });

            // 1. 按钮状态最可靠：passBtn.disabled == state.leading
            let leadingByBtn = null;
            if (passBtn) {
                leadingByBtn = isBtnDisabled(passBtn);
            } else if (playBtn || hintBtn) {
                leadingByBtn = true;
            }

            // 2. 桌面最新有效出牌（.ddz-latest 是 renderPlay 标记的当前轮次最后出牌）
            const latestPlay = document.querySelector('.ddz-play.ddz-latest');
            if (latestPlay) {
                const seat = parseInt(latestPlay.dataset.seat);
                const passEl = latestPlay.querySelector('.ddz-pass');
                if (!passEl && !isNaN(seat) && seat !== mySeat) {
                    const cards = [...latestPlay.querySelectorAll('.ddz-card[data-card]')]
                        .map(c => parseInt(c.dataset.card));
                    if (cards.length) {
                        const patterns = R.classify(cards);
                        lastPattern = patterns[0] || null;
                        lastPlaySeat = seat;
                    }
                }
            }

            // 3. 综合：按钮优先；按钮不可见时 fallback 到桌面
            if (leadingByBtn !== null) {
                isLeading = leadingByBtn;
            } else {
                isLeading = !lastPattern;
            }

            if (isLeading) {
                lastPattern = null;
                lastPlaySeat = null;
            }

            const counter = getCounter();
            const phase = getGamePhase();

            return {
                hand, mySeat, leftSeat, rightSeat,
                landlordSeat, teammateSeat, prevSeat, nextSeat,
                turnSeat, isMyTurn, role,
                opponentCounts, tablePlays,
                isLeading, lastPattern, lastPlaySeat,
                counter, phase
            };
        } catch (e) {
            console.error('[DDZ-AI] inferGameState error:', e);
            return {
                hand: [], mySeat: 2, leftSeat: 1, rightSeat: 0,
                landlordSeat: null, teammateSeat: null, prevSeat: 1, nextSeat: 0,
                turnSeat: null, isMyTurn: false, role: 'farmer',
                opponentCounts: {}, tablePlays: {},
                isLeading: true, lastPattern: null, lastPlaySeat: null,
                counter: new Array(15).fill(0), phase: 'unknown'
            };
        }
    }

    function getGamePhase() {
        const actions = $('#ddz-actions');
        if (!actions) return 'unknown';
        const text = actions.textContent;
        if (text.includes('分') && (text.includes('叫') || text.includes('不'))) return 'bidding';
        if (text.includes('出牌') || text.includes('不出') || text.includes('提示')) return 'playing';
        if (text.includes('等待')) return 'waiting';
        return 'unknown';
    }

    function getCounter() {
        try {
            const cells = $$('#ddz-counter .ddz-counter-cell');
            const nums = cells.map(c => {
                const n = c.querySelector('.ddz-counter-num');
                return n ? parseInt(n.textContent) : 0;
            });
            const rankMap = [14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0];
            const counter = new Array(15).fill(0);
            nums.forEach((n, i) => { if (rankMap[i] !== undefined) counter[rankMap[i]] = n; });
            return counter;
        } catch (e) {
            return new Array(15).fill(0);
        }
    }

    // ========================================================================
    // 牌组工具
    // ========================================================================
    function groups(hand) {
        const g = Array.from({length: 15}, () => []);
        for (const card of R.sortCards(hand)) g[R.rank(card)].push(card);
        return g;
    }

    function getAllPlays(hand) {
        const g = groups(hand);
        const plays = [];
        const add = c => plays.push(c);

        for (let r = 0; r < 15; r++) for (const c of g[r]) add([c]);
        for (let r = 0; r < 15; r++) if (g[r].length >= 2) add(g[r].slice(0, 2));
        for (let r = 0; r < 15; r++) if (g[r].length >= 3) add(g[r].slice(0, 3));
        for (let r = 0; r < 13; r++) if (g[r].length === 4) add(g[r].slice());
        if (g[13].length && g[14].length) add([g[13][0], g[14][0]]);

        for (let r = 0; r < 15; r++) {
            if (g[r].length < 3) continue;
            const body = g[r].slice(0, 3);
            for (let k = 0; k < 15; k++) {
                if (k === r || !g[k].length) continue;
                add([...body, g[k][0]]);
            }
        }
        for (let r = 0; r < 15; r++) {
            if (g[r].length < 3) continue;
            const body = g[r].slice(0, 3);
            for (let k = 0; k < 13; k++) {
                if (k === r || g[k].length < 2) continue;
                add([...body, ...g[k].slice(0, 2)]);
            }
        }
        for (let len = 5; len <= 12; len++) {
            for (let s = 0; s + len - 1 <= 10; s++) {
                let ok = true, cards = [];
                for (let r = s; r < s + len; r++) {
                    if (!g[r].length) { ok = false; break; }
                    cards.push(g[r][0]);
                }
                if (ok) add(cards);
            }
        }
        for (let len = 3; len <= 10; len++) {
            for (let s = 0; s + len - 1 <= 10; s++) {
                let ok = true, cards = [];
                for (let r = s; r < s + len; r++) {
                    if (g[r].length < 2) { ok = false; break; }
                    cards.push(...g[r].slice(0, 2));
                }
                if (ok) add(cards);
            }
        }
        for (let len = 2; len <= 6; len++) {
            for (let s = 0; s + len - 1 <= 10; s++) {
                let ok = true, cards = [];
                for (let r = s; r < s + len; r++) {
                    if (g[r].length < 3) { ok = false; break; }
                    cards.push(...g[r].slice(0, 3));
                }
                if (ok) add(cards);
            }
        }
        for (let len = 2; len <= 5; len++) {
            for (let s = 0; s + len - 1 <= 10; s++) {
                let body = [], ok = true, used = new Set();
                for (let r = s; r < s + len; r++) {
                    if (g[r].length < 3) { ok = false; break; }
                    body.push(...g[r].slice(0, 3));
                    used.add(r);
                }
                if (!ok) continue;
                const kickers = [];
                for (let r = 0; r < 15 && kickers.length < len; r++) {
                    if (used.has(r) || !g[r].length) continue;
                    kickers.push(g[r][0]);
                }
                if (kickers.length === len) add([...body, ...kickers]);
            }
        }
        for (let len = 2; len <= 4; len++) {
            for (let s = 0; s + len - 1 <= 10; s++) {
                let body = [], ok = true, used = new Set();
                for (let r = s; r < s + len; r++) {
                    if (g[r].length < 3) { ok = false; break; }
                    body.push(...g[r].slice(0, 3));
                    used.add(r);
                }
                if (!ok) continue;
                const pairs = [];
                for (let r = 0; r < 13 && pairs.length < len * 2; r++) {
                    if (used.has(r) || g[r].length < 2) continue;
                    pairs.push(...g[r].slice(0, 2));
                }
                if (pairs.length === len * 2) add([...body, ...pairs]);
            }
        }
        for (let r = 0; r < 13; r++) {
            if (g[r].length !== 4) continue;
            const kickers = [];
            for (let k = 0; k < 15 && kickers.length < 2; k++) {
                if (k === r || !g[k].length) continue;
                kickers.push(g[k][0]);
            }
            if (kickers.length === 2) add([...g[r].slice(), ...kickers]);
        }
        for (let r = 0; r < 13; r++) {
            if (g[r].length !== 4) continue;
            const pairs = [];
            for (let k = 0; k < 13 && pairs.length < 4; k++) {
                if (k === r || g[k].length < 2) continue;
                pairs.push(...g[k].slice(0, 2));
            }
            if (pairs.length === 4) add([...g[r].slice(), ...pairs]);
        }

        return plays;
    }

    function hasRocket(hand) {
        const c = R.counts(hand);
        return c[13] > 0 && c[14] > 0;
    }

    function countBombs(hand) {
        const c = R.counts(hand);
        let n = 0;
        for (let r = 0; r < 13; r++) if (c[r] === 4) n++;
        if (c[13] && c[14]) n++;
        return n;
    }

    function evaluateRemainingHand(hand) {
        if (hand.length === 0) return -500;
        const g = groups(hand);
        let score = 0;
        const singles = g.filter(c => c.length === 1).length;
        score += singles * 45;                 // 单张多了很难跑
        const pairs = g.filter(c => c.length === 2).length;
        score += pairs * 20;
        const trios = g.filter(c => c.length === 3).length;
        score += trios * -8;                   // 三张好，可带牌
        const bombs = g.filter((c, r) => c.length === 4 && r < 13).length;
        score += bombs * -35;
        if (g[13].length && g[14].length) score -= 45;
        // 最长单顺潜力
        let maxRun1 = 0, cur1 = 0;
        for (let r = 0; r <= 10; r++) {
            if (g[r].length > 0) { cur1++; maxRun1 = Math.max(maxRun1, cur1); }
            else cur1 = 0;
        }
        score -= maxRun1 * 5;
        // 最长连对潜力
        let maxRun2 = 0, cur2 = 0;
        for (let r = 0; r <= 10; r++) {
            if (g[r].length >= 2) { cur2++; maxRun2 = Math.max(maxRun2, cur2); }
            else cur2 = 0;
        }
        score -= maxRun2 * 4;
        score += hand.length * 8;
        return score;
    }

    // ========================================================================
    // 概率推断
    // ========================================================================
    function hypergeometricProb(N, K, n, k) {
        function C(a, b) {
            if (b < 0 || b > a) return 0;
            let res = 1;
            for (let i = 0; i < b; i++) res = res * (a - i) / (i + 1);
            return res;
        }
        return (C(K, k) * C(N - K, n - k)) / C(N, n);
    }

    function probTeammateCanBeatSingle(state, myRank) {
        const { teammateSeat, opponentCounts, counter } = state;
        if (teammateSeat === null) return 0;
        const teammateCards = opponentCounts[teammateSeat] || 0;
        if (teammateCards !== 1) return 0;
        const totalOutside = counter.reduce((a, b) => a + b, 0);
        if (totalOutside === 0) return 0;
        let probHigher = 0;
        for (let r = myRank + 1; r < 15; r++) {
            if (counter[r] > 0) probHigher += counter[r] / totalOutside;
        }
        return Math.min(1, probHigher);
    }

    // ========================================================================
    // 胜率估算
    // ========================================================================
    function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

    function evaluateHandStrength(hand) {
        const c = R.counts(hand);
        let score = 0;
        for (let r = 0; r < 13; r++) if (c[r] === 4) score += 60;
        if (c[13] && c[14]) score += 80;
        score += c[12] * 20;
        score += c[11] * 15;
        score += c[10] * 10;
        for (let r = 7; r < 10; r++) score += c[r] * 5;
        const g = groups(hand);
        let combos = 0;
        for (let len = 5; len <= 12; len++) {
            for (let s = 0; s + len - 1 <= 10; s++) {
                let ok = true;
                for (let r = s; r < s + len; r++) if (!g[r].length) ok = false;
                if (ok) combos++;
            }
        }
        for (let len = 3; len <= 10; len++) {
            for (let s = 0; s + len - 1 <= 10; s++) {
                let ok = true;
                for (let r = s; r < s + len; r++) if (g[r].length < 2) ok = false;
                if (ok) combos++;
            }
        }
        for (let len = 2; len <= 6; len++) {
            for (let s = 0; s + len - 1 <= 10; s++) {
                let ok = true;
                for (let r = s; r < s + len; r++) if (g[r].length < 3) ok = false;
                if (ok) combos++;
            }
        }
        score += combos * 8;
        score -= hand.length * 3;
        return score;
    }

    function estimateOpponentStrength(seat, count, counter) {
        const total = counter.reduce((a, b) => a + b, 0);
        if (total === 0 || count === 0) return 0;
        let weightedSum = 0;
        for (let r = 0; r < 15; r++) weightedSum += counter[r] * (r + 1);
        const avgValue = weightedSum / total;
        let score = avgValue * count * 4;
        const bigCards = counter[14] + counter[13] + counter[12] + counter[11];
        const probHasBig = 1 - Math.pow(1 - bigCards / total, count);
        score += probHasBig * 30;
        for (let r = 0; r < 13; r++) {
            if (counter[r] === 4) {
                const probBomb = hypergeometricProb(total, 4, count, 4);
                score += probBomb * 40;
            }
        }
        return score;
    }

    function estimateWinRate(state) {
        try {
            const { hand, role, opponentCounts, landlordSeat, teammateSeat, mySeat } = state;
            const myCount = hand.length;
            const myStrength = evaluateHandStrength(hand);

            const landlordCnt = landlordSeat !== null ? (opponentCounts[landlordSeat] || 17) : 17;
            const teammateCnt = teammateSeat !== null ? (opponentCounts[teammateSeat] || 0) : 99;
            const otherFarmerSeat = role === 'landlord'
                ? [0,1,2].find(s => s !== mySeat && s !== teammateSeat)
                : null;
            const otherFarmerCnt = otherFarmerSeat !== null ? (opponentCounts[otherFarmerSeat] || 17) : 99;

            // 牌数越少，越接近胜利（威胁越大 / 优势越大）
            const winProgress = (cnt) => {
                if (cnt === 0) return 100; // 已经赢了
                if (cnt <= 1) return 70;   // 极大威胁/优势
                if (cnt <= 2) return 45;
                if (cnt <= 4) return 20;
                if (cnt <= 8) return 8;
                return 0;
            };

            if (role === 'landlord') {
                // 地主胜率
                const myProgress = winProgress(myCount) + myStrength * 0.25;
                const f1Progress = winProgress(teammateCnt);
                const f2Progress = winProgress(otherFarmerCnt);
                const enemyProgress = Math.max(f1Progress, f2Progress) * 0.7 + Math.min(f1Progress, f2Progress) * 0.3 + 15;

                const advantage = (myProgress - enemyProgress * 1.15) / 120;
                let prob = clamp(0.5 + advantage * 0.5, 0.05, 0.95);

                // 任何农民快出完都是致命威胁
                if (teammateCnt <= 2 || otherFarmerCnt <= 2) prob -= 0.25;
                else if (teammateCnt <= 4 || otherFarmerCnt <= 4) prob -= 0.08;

                return clamp(prob, 0.05, 0.95);
            } else {
                // 农民胜率
                const myProgress = winProgress(myCount) + myStrength * 0.25;
                const teammateProgress = winProgress(teammateCnt) * 0.5;
                const teamProgress = myProgress + teammateProgress;

                // 地主威胁：牌少时威胁极大
                const landlordProgress = winProgress(landlordCnt) + 25; // 地主有底牌3张加成

                const advantage = (teamProgress - landlordProgress * 1.1) / 120;
                let prob = clamp(0.5 + advantage * 0.5, 0.05, 0.95);

                // 地主快出完：胜率暴跌
                if (landlordCnt <= 2) prob -= 0.35;
                else if (landlordCnt <= 4) prob -= 0.12;

                // 队友快出完：胜率提升
                if (teammateCnt <= 2) prob += 0.12;

                // 自己快出完：胜率提升
                if (myCount <= 2) prob += 0.08;

                return clamp(prob, 0.05, 0.95);
            }
        } catch (e) {
            console.error('[DDZ-AI] estimateWinRate error:', e);
            return 0.5;
        }
    }

    // 轻量版胜率估算（用于蒙特卡洛内部，不依赖完整 state）
    function fastWinRate(hand, role, mySeat, opponentCounts, landlordSeat, teammateSeat) {
        const myCount = hand.length;
        const myStrength = evaluateHandStrength(hand);
        const landlordCnt = landlordSeat !== null ? (opponentCounts[landlordSeat] || 17) : 17;
        const teammateCnt = teammateSeat !== null ? (opponentCounts[teammateSeat] || 0) : 99;
        const otherFarmerSeat = role === 'landlord'
            ? [0,1,2].find(s => s !== mySeat && s !== teammateSeat)
            : null;
        const otherFarmerCnt = otherFarmerSeat !== null ? (opponentCounts[otherFarmerSeat] || 17) : 99;

        const winProgress = (cnt) => {
            if (cnt === 0) return 100;
            if (cnt <= 1) return 70;
            if (cnt <= 2) return 45;
            if (cnt <= 4) return 20;
            if (cnt <= 8) return 8;
            return 0;
        };

        if (role === 'landlord') {
            const myProgress = winProgress(myCount) + myStrength * 0.25;
            const f1Progress = winProgress(teammateCnt);
            const f2Progress = winProgress(otherFarmerCnt);
            const enemyProgress = Math.max(f1Progress, f2Progress) * 0.7 + Math.min(f1Progress, f2Progress) * 0.3 + 15;
            const advantage = (myProgress - enemyProgress * 1.15) / 120;
            let prob = clamp(0.5 + advantage * 0.5, 0.05, 0.95);
            if (teammateCnt <= 2 || otherFarmerCnt <= 2) prob -= 0.25;
            else if (teammateCnt <= 4 || otherFarmerCnt <= 4) prob -= 0.08;
            return clamp(prob, 0.05, 0.95);
        } else {
            const myProgress = winProgress(myCount) + myStrength * 0.25;
            const teammateProgress = winProgress(teammateCnt) * 0.5;
            const teamProgress = myProgress + teammateProgress;
            const landlordProgress = winProgress(landlordCnt) + 25;
            const advantage = (teamProgress - landlordProgress * 1.1) / 120;
            let prob = clamp(0.5 + advantage * 0.5, 0.05, 0.95);
            if (landlordCnt <= 2) prob -= 0.35;
            else if (landlordCnt <= 4) prob -= 0.12;
            if (teammateCnt <= 2) prob += 0.12;
            if (myCount <= 2) prob += 0.08;
            return clamp(prob, 0.05, 0.95);
        }
    }

    // 对手简单策略模拟（用于蒙特卡洛 rollout）
    // 对手简单策略模拟（轻量版，避免蒙特卡洛内调用昂贵的 getAllPlays）
    function simulateOpponentPlay(hand, lastPattern) {
        if (!hand || !hand.length) return null;
        if (!lastPattern) {
            const g = groups(hand);
            const plays = [];
            // 炸弹 / 火箭
            for (let r = 0; r < 13; r++) if (g[r].length === 4) plays.push(g[r]);
            if (g[13].length && g[14].length) plays.push([g[13][0], g[14][0]]);
            // 三张
            for (let r = 0; r < 15; r++) if (g[r].length === 3) plays.push(g[r]);
            // 对子
            for (let r = 0; r < 15; r++) if (g[r].length === 2) plays.push(g[r]);
            // 单张
            for (let r = 0; r < 15; r++) if (g[r].length === 1) plays.push(g[r]);
            // 最长顺子
            let bestStraight = null, bestLen = 0;
            for (let s = 0; s <= 10; s++) {
                let len = 0;
                for (let r = s; r <= 10 && g[r].length > 0; r++) len++;
                if (len >= 5 && len > bestLen) {
                    bestLen = len;
                    bestStraight = [];
                    for (let r = s; r < s + len; r++) bestStraight.push(g[r][0]);
                }
            }
            if (bestStraight) plays.push(bestStraight);
            // 最长连对
            let bestPairs = null; bestLen = 0;
            for (let s = 0; s <= 10; s++) {
                let len = 0;
                for (let r = s; r <= 10 && g[r].length >= 2; r++) len++;
                if (len >= 3 && len > bestLen) {
                    bestLen = len;
                    bestPairs = [];
                    for (let r = s; r < s + len; r++) bestPairs.push(g[r][0], g[r][1]);
                }
            }
            if (bestPairs) plays.push(bestPairs);

            if (!plays.length) return null;
            const oneShot = plays.find(p => p.length === hand.length);
            if (oneShot) return oneShot;
            plays.sort((a, b) => b.length - a.length);
            const idx = Math.floor(Math.random() * Math.min(plays.length, 5));
            return plays[idx];
        } else {
            const hints = R.hints(hand, lastPattern);
            if (!hints.length) return null;
            const idx = Math.floor(Math.random() * Math.min(hints.length, 3));
            return hints[idx];
        }
    }

    // ========================================================================
    // 策略引擎
    // ========================================================================
    function scorePlay(cards, state) {
        try {
            const { hand, isLeading, lastPattern, role, teammateSeat, landlordSeat, opponentCounts, lastPlaySeat } = state;
            const remaining = hand.filter(c => !cards.includes(c));
            const patterns = R.classify(cards);
            if (!patterns.length) return { score: 99999, reasons: ['非法牌型'], pattern: null, cards };

            // ===== 核心修复：找到能管上 lastPattern 的具体 pattern =====
            let pattern = patterns[0];
            if (!isLeading && lastPattern) {
                const valid = patterns.find(p => R.beats(p, lastPattern));
                if (valid) {
                    pattern = valid;
                    // 二次保险：确认选中的 pattern 真的能击败 lastPattern
                    if (!R.beats(pattern, lastPattern)) {
                        console.error('[DDZ-AI] scorePlay pattern 二次验证失败', pattern, lastPattern, cards);
                        return { score: 99999, reasons: ['管不上(二次验证)'], pattern: patterns[0], cards };
                    }
                } else {
                    return { score: 99999, reasons: ['管不上'], pattern: patterns[0], cards };
                }
            }

            let score = 0;
            const reasons = [];

            if (cards.length === hand.length) {
                score -= 10000;
                reasons.push('一手出完');
                return { score, reasons, pattern, cards };
            }

            // 关键修复：评估剩余牌的"相对变化"而非绝对值。
            // 前期手牌多时 remScore 绝对值很大，会误伤正常跟牌。
            const handScore = evaluateRemainingHand(hand);
            const remScore = evaluateRemainingHand(remaining);
            const delta = remScore - handScore;
            score += delta * 1.5;

            const typeBonus = {
                rocket: -200, bomb: -100,
                plane: -180, plane1: -140, plane2: -140,
                straight: -120, pairs: -90,
                trio: -40, trio1: -70, trio2: -60,
                four2: -25, four22: -15,
                pair: 20, single: 40
            };
            score += (typeBonus[pattern.type] || 0);

            const teammateCount = teammateSeat !== null ? (opponentCounts[teammateSeat] || 17) : 99;
            const landlordCount = landlordSeat !== null ? (opponentCounts[landlordSeat] || 17) : 99;
            const otherFarmer = [0,1,2].find(s => s !== state.mySeat && s !== state.landlordSeat && s !== teammateSeat);
            const otherFarmerCount = otherFarmer !== undefined ? (opponentCounts[otherFarmer] || 17) : 99;
            const minOpp = Math.min(teammateCount, landlordCount, otherFarmerCount);

            if (role === 'farmer') {
                if (isLeading) {
                    if (teammateCount === 1) {
                        const prob = probTeammateCanBeatSingle(state, pattern.rank);
                        if (pattern.type === 'single') {
                            if (prob > 0.6) {
                                score -= 300;
                                reasons.push(`队友剩1张，送单张成功概率${(prob*100).toFixed(0)}%`);
                            } else if (prob > 0.3) {
                                score -= 80;
                            } else {
                                score += 50;
                                reasons.push(`队友剩1张，但接不住此单张`);
                            }
                        }
                    } else if (teammateCount === 2) {
                        if (pattern.type === 'pair') {
                            score -= 100;
                            reasons.push('队友剩2张，送对子');
                        } else if (pattern.type === 'single') {
                            score -= 50;
                            reasons.push('队友剩2张，送单张');
                        }
                    }
                    if (landlordCount <= 2) {
                        if (['single', 'pair'].includes(pattern.type)) {
                            score += 150;
                            reasons.push('地主快出完，小牌型危险');
                        }
                        score -= cards.length * 15;
                    }
                    if (teammateCount <= 3 && landlordCount > 3) {
                        if (['single', 'pair'].includes(pattern.type)) {
                            score -= 60;
                            reasons.push('队友快出完，协助跑牌');
                        }
                    }
                } else {
                    if (lastPlaySeat === teammateSeat) {
                        // 跟队友的牌通常不如pass，加重惩罚让AI倾向于不出
                        if (teammateCount <= 2) {
                            score += 500;
                            reasons.push(`队友剩${teammateCount}张，让他跑`);
                        } else if (teammateCount <= 5) {
                            score += 120;
                            reasons.push('队友在跑牌，可考虑让过');
                        } else {
                            score += 40;
                            reasons.push('队友出牌，可让过');
                        }
                        if (landlordCount <= 2 && teammateCount > 3) {
                            score -= 150;
                            reasons.push('地主快出完，需接管');
                        }
                    } else if (lastPlaySeat === landlordSeat) {
                        if (landlordCount <= 2) {
                            score -= 350;
                            reasons.push(`地主剩${landlordCount}张，必须压制`);
                            score -= pattern.rank * 8;
                        } else if (landlordCount <= 5) {
                            score -= 60;
                            reasons.push('地主牌较少，建议压制');
                        }
                    }
                }
            } else {
                const minFarmer = Math.min(teammateCount, otherFarmerCount);
                if (isLeading) {
                    if (minFarmer <= 2) {
                        score -= cards.length * 20;
                        reasons.push('农民快出完，加速');
                    }
                    if (['straight', 'pairs', 'plane', 'plane1', 'plane2'].includes(pattern.type)) {
                        score -= 30;
                    }
                } else {
                    if (minFarmer <= 2) {
                        score -= 150;
                        reasons.push('农民快出完，必须压');
                    }
                }
            }

            for (const c of cards) {
                const r = R.rank(c);
                if (r >= 14) score += 100;
                else if (r === 13) score += 70;
                else if (r === 12) score += 45;
                else if (r === 11) score += 25;
            }

            if (countBombs(remaining) < countBombs(hand)) {
                score += 200;
                reasons.push('消耗了炸弹');
            }
            if (hasRocket(hand) && !hasRocket(remaining)) {
                score += 400;
                reasons.push('消耗了王炸');
            }

            // ===== 核心修复：大小控制逻辑 =====
            if (!isLeading && lastPattern) {
                // 确保能管上（前面已处理，这里是二次保险）
                if (!R.beats(pattern, lastPattern)) {
                    score += 10000;
                } else {
                    // 同类型同长度时，比较 rank 差异
                    if (pattern.type === lastPattern.type && pattern.len === lastPattern.len) {
                        const diff = pattern.rank - lastPattern.rank;
                        if (diff <= 0) score += 1000;
                        else if (diff === 1) score -= 60;  // 刚好压过，最优
                        else if (diff <= 3) score -= 20;   // 合理
                        else score += diff * 5;            // 浪费大牌
                    }
                    // 炸弹/火箭管普通牌的特殊处理（加重惩罚）
                    else if (pattern.type === 'bomb' && lastPattern.type !== 'bomb' && lastPattern.type !== 'rocket') {
                        score += 180;
                        reasons.push('炸弹管普通牌，较亏');
                    } else if (pattern.type === 'rocket') {
                        score += 350;
                        reasons.push('王炸管普通牌，极亏');
                    }
                    // 其他跨类型（如飞机管顺子不会出现，因为 beats 已过滤）
                }
            } else if (isLeading) {
                if (pattern.type === 'single') score += R.rank(cards[0]) * 6;
                if (pattern.type === 'pair') score += R.rank(cards[0]) * 5;
            }

            if (minOpp <= 1) {
                if (isLeading && ['single', 'pair'].includes(pattern.type)) {
                    score -= pattern.rank * 10;
                }
            }

            return { score, reasons, pattern, cards };
        } catch (e) {
            console.error('[DDZ-AI] scorePlay error:', e);
            return { score: 99999, reasons: ['评分出错'], pattern: null, cards };
        }
    }

    function scorePass(state) {
        try {
            const { role, lastPattern, lastPlaySeat, teammateSeat, landlordSeat, opponentCounts, hand, isLeading } = state;
            if (isLeading) return { score: 99999, reasons: ['自由出牌不能pass'], pass: true };

            // 关键修复：不出牌的 baseline 必须比正常跟牌差。
            // 在斗地主中，"管牌"是默认行为，pass 需要明确理由。
            let score = 280;
            const reasons = [];
            const teammateCount = teammateSeat !== null ? (opponentCounts[teammateSeat] || 17) : 99;
            const landlordCount = landlordSeat !== null ? (opponentCounts[landlordSeat] || 17) : 99;
            const myStrength = evaluateHandStrength(hand);

            if (lastPlaySeat === teammateSeat) {
                // 队友出的牌：倾向于 pass 让队友跑
                if (teammateCount <= 2) {
                    score -= 450; // 总分 -170，比大多数跟牌好
                    reasons.push(`队友剩${teammateCount}张，必须让过`);
                } else if (teammateCount <= 4) {
                    score -= 120; // 总分 +160，仅比很差的跟牌略好
                    reasons.push('队友牌少，建议让过');
                } else {
                    score -= 20; // 总分 +260，基本不会选 pass
                }
                // 地主也快出完时，不能放任不管
                if (landlordCount <= 2 && teammateCount > 3) {
                    score += 500;
                    reasons.push('地主也快出完，需接管');
                }
            } else if (lastPlaySeat === landlordSeat) {
                // 地主出的牌：默认必须管，只有特殊理由才 pass
                if (landlordCount <= 2) {
                    score += 1000; // 绝对不能放
                    reasons.push(`地主剩${landlordCount}张，绝对不能放`);
                } else if (landlordCount <= 4) {
                    score += 180;
                    reasons.push('地主牌少，建议压制');
                }
                // 地主出小牌，可轻微倾向于 pass，让队友处理
                if (lastPattern && lastPattern.rank <= 5) {
                    score -= 25;
                }
                // 地主出大牌
                if (lastPattern && lastPattern.rank >= 10) {
                    score += 80;
                    reasons.push('地主出大牌，不管危险');
                }
            } else {
                score += 80;
                reasons.push('不明出牌者，建议管牌');
            }

            // 自己牌很弱，可以保存实力
            if (myStrength < 25) {
                score -= 40;
                reasons.push('自己牌弱，保存实力');
            }
            // 自己牌很强，应该争夺主动权
            if (myStrength > 110) {
                score += 80;
                reasons.push('自己牌力强，建议争夺主动权');
            }

            return { score, reasons, pass: true };
        } catch (e) {
            console.error('[DDZ-AI] scorePass error:', e);
            return { score: 300, reasons: ['pass评估出错'], pass: true };
        }
    }

    // ===================== 蒙特卡洛深度评估 =====================
    function getRemainingPool(counter, myHand) {
        const pool = [];
        const myRanks = R.counts(myHand);
        for (let r = 0; r < 15; r++) {
            const totalLeft = counter[r];
            const myCount = myRanks[r];
            const outsideCount = Math.max(0, totalLeft - myCount);
            if (r === 13) {
                for (let i = 0; i < outsideCount; i++) pool.push(52);
            } else if (r === 14) {
                for (let i = 0; i < outsideCount; i++) pool.push(53);
            } else {
                for (let i = 0; i < outsideCount; i++) pool.push(r * 4 + (i % 4));
            }
        }
        return pool;
    }

    function sampleWithoutReplacement(arr, count) {
        if (count <= 0) return [];
        const copy = arr.slice();
        const res = [];
        for (let i = 0; i < count && copy.length > 0; i++) {
            const idx = Math.floor(Math.random() * copy.length);
            res.push(copy.splice(idx, 1)[0]);
        }
        return res;
    }

    // 对单一候选进行蒙特卡洛快速模拟（一轮展望 + 胜率评估）
    function monteCarloEvaluateFast(candidate, state, pool, nextCount, prevCount, iterations) {
        if (pool.length === 0) return 0;

        const myRemaining = candidate.pass ? state.hand : state.hand.filter(c => !candidate.cards.includes(c));
        let totalScore = 0;
        const nextSeat = (state.mySeat + 1) % 3;
        const prevSeat = (state.mySeat + 2) % 3;
        const hasOneShot = myRemaining.length > 0 && getAllPlays(myRemaining).some(p => p.length === myRemaining.length);

        for (let i = 0; i < iterations; i++) {
            const dealPool = pool.slice();
            const hands = {};
            hands[nextSeat] = sampleWithoutReplacement(dealPool, nextCount);
            hands[prevSeat] = sampleWithoutReplacement(dealPool, prevCount);

            // ====== 一轮展望：执行候选 → 下家反应 → 上家反应 ======
            let currentPattern = candidate.pass ? state.lastPattern : candidate.pattern;
            let currentSeat = state.mySeat;
            let gotLead = false; // 是否都pass，我获得出牌权

            // 下家反应
            const nextPlay = simulateOpponentPlay(hands[nextSeat], currentPattern);
            if (nextPlay) {
                hands[nextSeat] = hands[nextSeat].filter(c => !nextPlay.includes(c));
                const pat = R.classify(nextPlay);
                currentPattern = pat.length ? pat[0] : null;
                currentSeat = nextSeat;
            } else {
                // 上家反应
                const prevPlay = simulateOpponentPlay(hands[prevSeat], currentPattern);
                if (prevPlay) {
                    hands[prevSeat] = hands[prevSeat].filter(c => !prevPlay.includes(c));
                    const pat = R.classify(prevPlay);
                    currentPattern = pat.length ? pat[0] : null;
                    currentSeat = prevSeat;
                } else {
                    gotLead = true;
                    currentPattern = null;
                }
            }

            // ====== 终端局面胜率评估 ======
            let terminalWinRate = 0;
            if (myRemaining.length === 0) {
                terminalWinRate = 1.0;
            } else {
                const oppCounts = {};
                oppCounts[nextSeat] = hands[nextSeat].length;
                oppCounts[prevSeat] = hands[prevSeat].length;
                terminalWinRate = fastWinRate(myRemaining, state.role, state.mySeat, oppCounts, state.landlordSeat, state.teammateSeat);
            }

            // 胜率 0~1 映射为分数 -500 ~ +500（越低越好）
            totalScore += (0.5 - terminalWinRate) * 1000;

            // ====== 一轮展望的即时奖惩 ======
            if (candidate.pass) {
                if (gotLead) {
                    totalScore -= 120; // pass后获得出牌权，极好
                } else if (currentSeat === nextSeat) {
                    totalScore += 70;  // 下家接上，较糟
                } else if (currentSeat === prevSeat) {
                    totalScore += 35;  // 上家接上
                }
            } else {
                if (gotLead) {
                    totalScore -= 150; // 出牌后没人管，获得继续出牌权，极好
                } else if (currentSeat === nextSeat) {
                    totalScore += 90;  // 下家管上
                } else if (currentSeat === prevSeat) {
                    totalScore += 50;  // 上家管上
                }
            }

            // ====== 一手出完奖励 ======
            if (myRemaining.length === 0) {
                totalScore -= 600;
            } else if (hasOneShot) {
                totalScore -= 80;
            }

            // ====== 剩余手牌结构 ======
            const remScore = evaluateRemainingHand(myRemaining);
            totalScore += remScore * 0.35;
        }
        return totalScore / iterations;
    }

    // ===================== 异步深度搜索 =====================
    let gSearch = null;

    function getTurnSignature(state) {
        const lp = state.lastPattern ? `${state.lastPattern.type}:${state.lastPattern.rank}:${state.lastPattern.len}` : 'lead';
        return `${state.phase}-${state.turnSeat}-${state.hand.length}-${state.isLeading}-${lp}`;
    }

    function buildCandidates(state) {
        const candidates = [];
        if (state.isLeading) {
            const plays = getAllPlays(state.hand);
            const scored = plays.map(cards => {
                const res = scorePlay(cards, state);
                return { ...res, cards };
            }).filter(s => s.pattern !== null);
            scored.sort((a, b) => a.score - b.score);
            candidates.push(...scored.slice(0, 12));
        } else {
            const hints = R.hints(state.hand, state.lastPattern);
            const scored = hints.map(cards => {
                const res = scorePlay(cards, state);
                return { ...res, cards };
            }).filter(s => s.pattern !== null && R.beats(s.pattern, state.lastPattern));
            scored.sort((a, b) => a.score - b.score);
            candidates.push(...scored.slice(0, 12));
            const passRes = scorePass(state);
            candidates.push({ ...passRes, cards: null });
        }
        return candidates;
    }

    function startDeepSearch(state) {
        const sig = getTurnSignature(state);
        if (gSearch && gSearch.signature === sig) return;

        const candidates = buildCandidates(state);
        if (candidates.length <= 1) {
            gSearch = { signature: sig, done: true, best: candidates[0] || null, progress: 1, completed: 1, totalIter: 1 };
            return;
        }

        // 初始化蒙特卡洛统计
        candidates.forEach(c => { c.mcTotal = 0; c.mcCount = 0; c.finalScore = c.score; });
        const totalIter = 400000;
        const batchSize = 500;   // 每帧批量执行500轮
        const timeLimit = 10000; // 最多跑10秒
        gSearch = { signature: sig, done: false, best: candidates[0], progress: 0, completed: 0, totalIter, candidates };

        const pool = getRemainingPool(state.counter, state.hand);
        const nextSeat = (state.mySeat + 1) % 3;
        const prevSeat = (state.mySeat + 2) % 3;
        const nextCount = state.opponentCounts[nextSeat] || 0;
        const prevCount = state.opponentCounts[prevSeat] || 0;

        let completed = 0;
        const startTime = performance.now();

        function step() {
            if (!gSearch || gSearch.signature !== sig) return;
            const frameStart = performance.now();

            // 批量执行，但单帧不超过12ms避免卡顿
            for (let b = 0; b < batchSize && completed < totalIter; b++) {
                for (const cand of candidates) {
                    const mc = monteCarloEvaluateFast(cand, state, pool, nextCount, prevCount, 1);
                    cand.mcTotal += mc;
                    cand.mcCount++;
                }
                completed++;
                if (performance.now() - frameStart > 12) break;
            }

            gSearch.progress = completed / totalIter;
            gSearch.completed = completed;

            // 动态权重：早期 baseScore 主导（减少 MC 噪声），后期 MC 权重上升
            const progress = completed / totalIter;
            const mcWeight = Math.min(0.65, 0.2 + progress * 0.55); // 20% → 65%
            const baseWeight = 1 - mcWeight;
            for (const cand of candidates) {
                const mcAvg = cand.mcCount > 0 ? cand.mcTotal / cand.mcCount : 0;
                cand.finalScore = cand.score * baseWeight + mcAvg * mcWeight;
            }
            candidates.sort((a, b) => a.finalScore - b.finalScore);
            gSearch.best = candidates[0];

            const elapsed = performance.now() - startTime;
            if (completed < totalIter && elapsed < timeLimit) {
                requestAnimationFrame(step);
            } else {
                gSearch.done = true;
                gSearch.progress = 1;
                gSearch.completed = completed;
            }
        }
        step();
    }

    function selectWithStrategy(options, temperature) {
        if (options.length === 0) return null;
        if (options.length === 1) return { ...options[0], chosen: 'only' };

        options = options.slice().sort((a, b) => a.score - b.score);

        if (options.length >= 2 && options[1].score - options[0].score > 150) {
            return { ...options[0], chosen: 'best' };
        }

        const minScore = options[0].score;
        const expScores = options.map(o => Math.exp(-(o.score - minScore) / temperature));
        const sum = expScores.reduce((a, b) => a + b, 0);
        let r = Math.random() * sum;
        for (let i = 0; i < options.length; i++) {
            r -= expScores[i];
            if (r <= 0) return { ...options[i], chosen: i === 0 ? 'best_prob' : `alt_${i}` };
        }
        return { ...options[0], chosen: 'best_fallback' };
    }

    function getSuggestion(state) {
        if (!state) state = inferGameState();
        const { hand, isLeading, lastPattern, phase, isMyTurn } = state;

        if (phase !== 'playing' || !isMyTurn) return null;
        if (!hand.length) return null;

        if (isLeading) {
            return suggestLead(state);
        } else {
            if (!lastPattern) return null;
            return suggestFollow(state);
        }
    }

    function suggestLead(state) {
        try {
            const { hand } = state;
            const plays = getAllPlays(hand);

            const oneShot = plays.find(p => p.length === hand.length);
            if (oneShot) {
                const pat = R.classify(oneShot)[0];
                return { cards: oneShot, desc: R.describe(pat) + ' 【一手出完】', pattern: pat, reasons: ['手牌可一次清空'], score: -9999 };
            }

            const scored = plays.map(cards => {
                const res = scorePlay(cards, state);
                return { ...res, cards };
            }).filter(s => s.pattern !== null);

            if (!scored.length) return null;

            const best = selectWithStrategy(scored, CONFIG.temperature);
            if (!best) return null;

            let desc = R.describe(best.pattern);
            if (best.chosen && best.chosen.includes('alt')) desc += ' (备选)';
            return { cards: best.cards, desc, pattern: best.pattern, reasons: best.reasons, score: best.score };
        } catch (e) {
            console.error('[DDZ-AI] suggestLead error:', e);
            return null;
        }
    }

    function suggestFollow(state) {
        try {
            const { hand, lastPattern } = state;
            const hints = R.hints(hand, lastPattern);

            // 始终评估"不出"选项，与所有跟牌选项一起比较
            const passOption = scorePass(state);
            const options = [passOption];

            if (hints.length) {
                const scored = hints.map(cards => {
                    const res = scorePlay(cards, state);
                    return { ...res, cards };
                }).filter(s => s.pattern !== null && R.beats(s.pattern, lastPattern));
                options.push(...scored);
            }

            if (options.length === 0) {
                return { cards: null, desc: '【不出】没有能管上的牌', pattern: null, reasons: ['无合法跟牌'], score: 0, pass: true };
            }

            const best = selectWithStrategy(options, CONFIG.temperature);
            if (!best) return null;

            if (best.pass) {
                let desc = '【不出】';
                if (best.reasons && best.reasons.length) desc += best.reasons[0];
                return { cards: null, desc, pattern: null, reasons: best.reasons, score: best.score, pass: true };
            }

            // 最终安全检查：确保返回的跟牌真的能击败 lastPattern
            if (!R.beats(best.pattern, lastPattern)) {
                console.error('[DDZ-AI] suggestFollow 安全检查拦截', best.pattern, lastPattern);
                return { cards: null, desc: '【不出】安全检查拦截', pattern: null, reasons: ['跟牌验证失败，转为不出'], score: 0, pass: true };
            }

            let desc = R.describe(best.pattern);
            if (best.chosen && best.chosen.includes('alt')) desc += ' (备选)';
            return { cards: best.cards, desc, pattern: best.pattern, reasons: best.reasons, score: best.score };
        } catch (e) {
            console.error('[DDZ-AI] suggestFollow error:', e);
            return null;
        }
    }

    function getBidSuggestion(state) {
        try {
            const { hand } = state;
            const c = R.counts(hand);
            let strength = 0;
            // 炸弹权重大幅提高
            for (let r = 0; r < 13; r++) if (c[r] === 4) strength += 55;
            if (c[13] && c[14]) strength += 85;
            // 大牌
            strength += c[12] * 22; // 2
            strength += c[11] * 14; // A
            strength += c[10] * 9;  // K
            strength += c[9] * 5;   // Q
            // 长顺子（真正可出的）
            const g = groups(hand);
            let maxRun1 = 0, cur1 = 0;
            let maxRun2 = 0, cur2 = 0;
            for (let r = 0; r <= 10; r++) {
                if (g[r].length > 0) { cur1++; maxRun1 = Math.max(maxRun1, cur1); }
                else cur1 = 0;
                if (g[r].length >= 2) { cur2++; maxRun2 = Math.max(maxRun2, cur2); }
                else cur2 = 0;
            }
            if (maxRun1 >= 8) strength += 18;
            else if (maxRun1 >= 6) strength += 10;
            else if (maxRun1 >= 5) strength += 5;
            if (maxRun2 >= 4) strength += 8;
            // 飞机/连对/顺子组合数量
            let comboScore = 0;
            for (let len = 5; len <= 12; len++) {
                for (let s = 0; s + len - 1 <= 10; s++) {
                    let ok = true;
                    for (let r = s; r < s + len; r++) if (!g[r].length) ok = false;
                    if (ok) comboScore += 3;
                }
            }
            for (let len = 3; len <= 10; len++) {
                for (let s = 0; s + len - 1 <= 10; s++) {
                    let ok = true;
                    for (let r = s; r < s + len; r++) if (g[r].length < 2) ok = false;
                    if (ok) comboScore += 3;
                }
            }
            for (let len = 2; len <= 6; len++) {
                for (let s = 0; s + len - 1 <= 10; s++) {
                    let ok = true;
                    for (let r = s; r < s + len; r++) if (g[r].length < 3) ok = false;
                    if (ok) comboScore += 4;
                }
            }
            strength += Math.min(comboScore, 30);
            // 手牌数惩罚（牌多不容易跑）
            strength -= hand.length * 1.5;

            // 大幅提高门槛，没明显优势不叫
            if (strength >= 120) return 3;
            if (strength >= 85) return 2;
            if (strength >= 50) return 1;
            return 0;
        } catch (e) {
            console.error('[DDZ-AI] getBidSuggestion error:', e);
            return 0;
        }
    }

    // ========================================================================
    // UI
    // ========================================================================
    function injectStyles() {
        try {
            const style = document.createElement('style');
            style.textContent = `
                #ddz-ai-panel {
                    position: fixed !important;
                    right: 16px !important;
                    top: 76px !important;
                    width: 280px !important;
                    background: #142218 !important;
                    border: 2px solid #f6c343 !important;
                    border-radius: 14px !important;
                    color: #fff !important;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Microsoft YaHei", sans-serif !important;
                    font-size: 13px !important;
                    z-index: 2147483647 !important;
                    box-shadow: 0 6px 28px rgba(0,0,0,0.5) !important;
                    user-select: none !important;
                    overflow: hidden !important;
                    display: block !important;
                    visibility: visible !important;
                    opacity: 1 !important;
                }
                #ddz-ai-panel.ddz-ai-collapsed .ddz-ai-body { display: none !important; }
                .ddz-ai-header {
                    display: flex !important; justify-content: space-between !important; align-items: center !important;
                    padding: 8px 12px !important;
                    background: linear-gradient(90deg, #1f7a4a, #0f4d2c) !important;
                    border-radius: 12px 12px 0 0 !important;
                    font-weight: 700 !important; color: #f6c343 !important;
                }
                .ddz-ai-header-left { display: flex; align-items: center; gap: 10px; }
                .ddz-ai-switch-label { display: flex; align-items: center; gap: 5px; font-size: 11px; color: #fff; cursor: pointer; }
                .ddz-ai-switch-label input { cursor: pointer; }
                #ddz-ai-toggle { background: rgba(255,255,255,0.2); border: none; color: #fff; width: 24px; height: 24px; border-radius: 6px; cursor: pointer; font-weight: 700; }
                .ddz-ai-body { padding: 10px 12px 12px; }
                .ddz-ai-status { font-size: 12px; color: #ced4da; margin-bottom: 8px; padding: 5px 8px; background: rgba(0,0,0,0.3); border-radius: 6px; display: flex; justify-content: space-between; align-items: center; }
                .ddz-ai-status-badge { font-size: 10px; padding: 1px 6px; border-radius: 999px; background: #495057; color: #fff; }
                .ddz-ai-status-badge.active { background: #28a745; }
                .ddz-ai-suggestion { margin-bottom: 8px; }
                .ddz-ai-label { font-size: 11px; color: #f6c343; margin-bottom: 5px; font-weight: 700; display: flex; justify-content: space-between; }
                .ddz-ai-cards { display: flex; flex-wrap: wrap; gap: 3px; margin-bottom: 6px; }
                .ddz-ai-card { display: inline-flex; align-items: center; justify-content: center; padding: 2px 6px; border-radius: 4px; background: #fff; color: #212529; font-weight: 700; font-size: 12px; border: 1px solid #ddd; }
                .ddz-ai-card.red { color: #dc3545; }
                .ddz-ai-desc { font-size: 12px; color: #e9ecef; margin-top: 4px; font-weight: 600; }
                .ddz-ai-reasoning { margin: 8px 0; padding: 6px 8px; background: rgba(0,0,0,0.25); border-radius: 6px; font-size: 11px; color: #adb5bd; line-height: 1.6; max-height: 120px; overflow-y: auto; }
                .ddz-ai-reasoning:empty { display: none; }
                .ddz-ai-reasoning div { padding: 1px 0; }
                .ddz-ai-reason-tag { color: #f6c343; }
                .ddz-ai-warn { color: #ff6b6b; font-weight: 700; }
                .ddz-ai-ok { color: #51cf66; }
                .ddz-ai-tip { color: #74c0fc; }
                .ddz-ai-buttons { display: flex; gap: 6px; }
                .ddz-ai-btn { flex: 1; padding: 7px 0; border-radius: 999px; border: none; font-size: 12px; font-weight: 700; cursor: pointer; background: rgba(255,255,255,0.12); color: #fff; transition: background 0.15s; }
                .ddz-ai-btn:hover { background: rgba(255,255,255,0.22); }
                .ddz-ai-btn:disabled { opacity: 0.35; cursor: not-allowed; }
                .ddz-ai-btn-primary { background: #f6c343; color: #4a3b00; }
                .ddz-ai-btn-primary:hover { background: #ffd866; }
                .ddz-ai-info-row { display: flex; justify-content: space-between; font-size: 11px; color: #adb5bd; margin-top: 6px; padding-top: 6px; border-top: 1px dashed rgba(255,255,255,0.12); }
                .ddz-ai-winrate { margin-top: 6px; padding-top: 6px; border-top: 1px dashed rgba(255,255,255,0.12); }
                .ddz-ai-winrate-label { font-size: 11px; color: #adb5bd; margin-bottom: 3px; }
                .ddz-ai-winrate-bar { height: 6px; border-radius: 3px; background: rgba(255,255,255,0.15); overflow: hidden; }
                .ddz-ai-winrate-fill { height: 100%; border-radius: 3px; transition: width 0.3s ease; }
                .ddz-ai-winrate-text { font-size: 11px; margin-top: 3px; display: flex; justify-content: space-between; }
                .ddz-ai-winrate-high { color: #51cf66; }
                .ddz-ai-winrate-mid { color: #f6c343; }
                .ddz-ai-winrate-low { color: #ff6b6b; }
                #ddz-ai-prompt-overlay, #ddz-ai-continue-overlay {
                    position: fixed !important; inset: 0 !important; background: rgba(0,0,0,0.65) !important;
                    z-index: 2147483647 !important; display: flex !important; align-items: center !important; justify-content: center !important;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Microsoft YaHei", sans-serif !important;
                }
                #ddz-ai-prompt-box, #ddz-ai-continue-box {
                    background: #142218 !important; border: 2px solid #f6c343 !important; border-radius: 16px !important;
                    width: 320px !important; padding: 22px !important; color: #fff !important; text-align: center !important;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.6) !important;
                }
                #ddz-ai-prompt-title, #ddz-ai-continue-title { font-size: 16px !important; font-weight: 700 !important; color: #f6c343 !important; margin-bottom: 10px !important; }
                #ddz-ai-prompt-countdown, #ddz-ai-continue-countdown { font-size: 13px !important; color: #ced4da !important; margin-bottom: 18px !important; }
                .ddz-ai-prompt-btns { display: flex !important; gap: 10px !important; justify-content: center !important; }
                .ddz-ai-prompt-btn { padding: 9px 18px !important; border-radius: 999px !important; border: none !important; font-size: 13px !important; font-weight: 700 !important; cursor: pointer !important; transition: transform .1s, opacity .15s; }
                .ddz-ai-prompt-btn:hover { transform: translateY(-1px); opacity: .9; }
                .ddz-ai-prompt-btn-yes { background: #28a745 !important; color: #fff !important; }
                .ddz-ai-prompt-btn-no { background: rgba(255,255,255,0.15) !important; color: #fff !important; }
            `;
            document.head.appendChild(style);
            console.log('[DDZ-AI] 样式已注入');
        } catch (e) {
            console.error('[DDZ-AI] injectStyles error:', e);
        }
    }

    let promptShown = false;
    function showAutoPlayPrompt() {
        if (promptShown) return;
        promptShown = true;
        try {
            const overlay = document.createElement('div');
            overlay.id = 'ddz-ai-prompt-overlay';
            const box = document.createElement('div');
            box.id = 'ddz-ai-prompt-box';

            const title = document.createElement('div');
            title.id = 'ddz-ai-prompt-title';
            title.textContent = '是否开启自动打牌模式？';

            const countdown = document.createElement('div');
            countdown.id = 'ddz-ai-prompt-countdown';

            const btnRow = document.createElement('div');
            btnRow.className = 'ddz-ai-prompt-btns';

            let timeLeft = 5;
            let timer = null;

            function finish(choice) {
                clearInterval(timer);
                CONFIG.autoPlay = choice;
                const autoCheck = document.getElementById('ddz-ai-auto');
                if (autoCheck) autoCheck.checked = choice;
                overlay.remove();
                updatePanel();
            }

            const yesBtn = document.createElement('button');
            yesBtn.className = 'ddz-ai-prompt-btn ddz-ai-prompt-btn-yes';
            yesBtn.textContent = '开启自动';
            yesBtn.addEventListener('click', () => finish(true));

            const noBtn = document.createElement('button');
            noBtn.className = 'ddz-ai-prompt-btn ddz-ai-prompt-btn-no';
            noBtn.textContent = '手动辅助';
            noBtn.addEventListener('click', () => finish(false));

            btnRow.append(yesBtn, noBtn);
            box.append(title, countdown, btnRow);
            overlay.append(box);
            document.body.appendChild(overlay);

            countdown.textContent = `${timeLeft} 秒后自动开启`;
            timer = setInterval(() => {
                timeLeft--;
                if (timeLeft > 0) {
                    countdown.textContent = `${timeLeft} 秒后自动开启`;
                } else {
                    countdown.textContent = '正在开启自动模式…';
                    finish(true);
                }
            }, 1000);
        } catch (e) {
            console.error('[DDZ-AI] showAutoPlayPrompt error:', e);
        }
    }

    let continuePromptShown = false;

    function isContinueButtonText(t) {
        return (t.includes('→') || t.includes('再来一局') || t.includes('再来一场比赛')) && !t.includes('发送');
    }

    function findContinueButton() {
        try {
            const resultBox = document.getElementById('ddz-result');
            if (resultBox) {
                const title = resultBox.querySelector('.ddz-result-title');
                if (title) {
                    const btn = [...resultBox.querySelectorAll('button')].find(b => {
                        const t = b.textContent.trim();
                        return isContinueButtonText(t) && !b.disabled;
                    });
                    if (btn) return btn;
                }
            }
            const actions = document.getElementById('ddz-actions');
            if (actions) {
                const btn = [...actions.querySelectorAll('button')].find(b => {
                    const t = b.textContent.trim();
                    return isContinueButtonText(t) && !b.disabled;
                });
                if (btn) return btn;
            }
        } catch (e) {}
        return null;
    }

    function hasEndGameUI() {
        try {
            const resultBox = document.getElementById('ddz-result');
            if (resultBox && resultBox.querySelector('.ddz-result-title')) return true;
            const actions = document.getElementById('ddz-actions');
            if (actions) {
                return [...actions.querySelectorAll('button')].some(b => {
                    const t = b.textContent.trim();
                    return (t.includes('再来一局') || t.includes('查看结果')) && !b.disabled;
                });
            }
        } catch (e) {}
        return false;
    }

    function showContinuePrompt(targetBtn) {
        try {
            const overlay = document.createElement('div');
            overlay.id = 'ddz-ai-continue-overlay';
            const box = document.createElement('div');
            box.id = 'ddz-ai-continue-box';

            const title = document.createElement('div');
            title.id = 'ddz-ai-continue-title';
            title.textContent = '比赛结束，是否继续下一局？';

            const countdown = document.createElement('div');
            countdown.id = 'ddz-ai-continue-countdown';

            const btnRow = document.createElement('div');
            btnRow.className = 'ddz-ai-prompt-btns';

            let timeLeft = 5;
            let timer = null;

            function finish(shouldContinue) {
                clearInterval(timer);
                overlay.remove();
                if (shouldContinue && targetBtn && document.body.contains(targetBtn)) {
                    targetBtn.click();
                }
            }

            const yesBtn = document.createElement('button');
            yesBtn.className = 'ddz-ai-prompt-btn ddz-ai-prompt-btn-yes';
            yesBtn.textContent = '继续';
            yesBtn.addEventListener('click', () => finish(true));

            const noBtn = document.createElement('button');
            noBtn.className = 'ddz-ai-prompt-btn ddz-ai-prompt-btn-no';
            noBtn.textContent = '取消';
            noBtn.addEventListener('click', () => finish(false));

            btnRow.append(yesBtn, noBtn);
            box.append(title, countdown, btnRow);
            overlay.append(box);
            document.body.appendChild(overlay);

            countdown.textContent = `${timeLeft} 秒后自动继续`;
            timer = setInterval(() => {
     %2
