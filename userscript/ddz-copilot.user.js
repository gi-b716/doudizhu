// ==UserScript==
// @name         斗地主 Copilot
// @namespace    ddz-copilot
// @version      0.3.0
// @description  真实对局中实时给出可解释的出牌建议（只建议，不代打、不自动提交）
// @author       you
// @include      /^https?:\/\/[^/]+\/games\/doudizhu/
// @run-at       document-start
// @grant        none
// @noframes
// ==/UserScript==

/*
 * 设计约束（都来自 PLAN §3.6，改动前先读一遍）：
 *
 * 1. 页面状态在 IIFE 闭包里，外面拿不到 → 唯一的信息来源是拦截 window.fetch。
 *    **绝不改写请求或响应**：只是 clone 一份自己看。另外兜底读 #ddz-initial。
 * 2. 只建议：不点「出牌」、不发 play/pass/bid、不改轮询频率。「帮我选中」默认关闭，
 *    它只帮你在手牌上做出选择，点不点出牌永远是你自己的事。
 * 3. 服务没起或解析失败时，面板要显示「服务未启动 / 建议暂不可用」，不能静默什么都不显示。
 */

(() => {
    'use strict';

    const API = 'http://127.0.0.1:17876';
    const HEALTH_MS = 5000;
    const DEBOUNCE_MS = 150;
    const RANKS = ['3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A', '2', '小王', '大王'];

    const store = {
        get(key, fallback) {
            try {
                const raw = localStorage.getItem(key);
                return raw === null ? fallback : JSON.parse(raw);
            } catch (e) {
                return fallback;
            }
        },
        set(key, value) {
            try {
                localStorage.setItem(key, JSON.stringify(value));
            } catch (e) { /* 隐私模式 */ }
        },
    };

    const session = (() => {
        try {
            let id = sessionStorage.getItem('ddz-copilot-session');
            if (!id) {
                id = Math.random().toString(36).slice(2, 10);
                sessionStorage.setItem('ddz-copilot-session', id);
            }
            return id;
        } catch (e) {
            return 'anon';
        }
    })();

    // engine: 'heuristic' | 'douzero'（两个引擎选项；douzero 要等 Phase 4 权重就位）
    const config = Object.assign(
        {collapsed: false, autoSelect: false, engine: 'heuristic', left: null, top: null},
        store.get('ddz-copilot-config', {}),
    );

    // ---------------------------------------------------------------- 运行时状态

    let latestState = null;      // 平台最新一份 state
    let lastVersion = -1;
    let advice = null;           // 最新一条建议（带 version）
    let adviseInFlight = false;
    let adviseTimer = null;
    let lastError = '';
    let serviceOk = null;        // null / true / false
    let ingestedVersion = -1;
    let handEl = null;

    // ---------------------------------------------------------------- fetch 拦截

    const nativeFetch = window.fetch;
    window.fetch = function (...args) {
        const promise = nativeFetch.apply(this, args);
        try {
            const input = args[0];
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url.indexOf('/games/doudizhu/api') !== -1) {
                promise.then((response) => {
                    // clone 一份自己读，绝不碰原来那个 response
                    response.clone().json().then(onApiPayload).catch(() => {});
                }).catch(() => {});
            }
        } catch (e) { /* 拦截失败也不影响页面 */ }
        return promise;
    };

    function onApiPayload(data) {
        if (data && data.state) accept(data.state);
    }

    function bootstrap() {
        // 首屏兜底：HTML 里内嵌的 <script id="ddz-initial">
        const node = document.getElementById('ddz-initial');
        if (!node) return;
        try {
            const initial = JSON.parse(node.textContent);
            if (initial && initial.state) accept(initial.state);
        } catch (e) { /* 没有就算了，反正还有轮询 */ }
    }

    // ---------------------------------------------------------------- 状态分发

    function accept(state) {
        if (!state || typeof state.version !== 'number') return;
        if (state.unchanged) return;               // 只刷新计时，不影响建议
        if (state.version === lastVersion) return; // 同一份状态不重复处理
        if (state.version < lastVersion) {
            // version 只在一局之内递增；退回去说明换局了（整页跳转之外的情况也兜住）
            advice = null;
            ingestedVersion = -1;
            lastError = '';
        }
        lastVersion = state.version;
        latestState = state;

        if (state.phase === 'finished') {
            if (ingestedVersion !== state.version) {
                ingestedVersion = state.version;
                ingest(state);
            }
            stopAdvising();
            render();
            return;
        }
        scheduleAdvise();
        render();
    }

    function stopAdvising() {
        clearTimeout(adviseTimer);
        adviseTimer = null;
    }

    function myTurn() {
        return !!latestState && latestState.seat !== null && latestState.turn === latestState.seat;
    }

    function scheduleAdvise() {
        stopAdvising();
        if (!latestState || latestState.seat === null) return;      // 旁观：不请求
        if (!myTurn()) return;                                       // 没轮到：显示等待
        if (advice && advice.version === latestState.version) return; // 这个局面已经有建议了
        adviseTimer = setTimeout(requestAdvise, DEBOUNCE_MS);
    }

    function requestAdvise() {
        if (adviseInFlight || !latestState) return;
        const version = latestState.version;
        adviseInFlight = true;
        post('/advise', {
            session,
            game_id: currentGameId(),
            state: latestState,
            options: {agent: engineSpec()},
        })
            .then((data) => {
                adviseInFlight = false;
                markServiceOk(true);
                if (!data) return;
                if (latestState && latestState.version !== version) return; // 过期响应直接丢
                advice = Object.assign({version}, data);
                if (!data.ok) lastError = data.message || data.reason || '';
                render();
                if (config.autoSelect && data.ok && myTurn()) selectRecommended();
            })
            .catch((err) => {
                adviseInFlight = false;
                markServiceOk(false);
                lastError = String(err && err.message ? err.message : err);
                render();
            });
    }

    function ingest(state) {
        post('/ingest', {state, game_id: currentGameId()})
            .then(() => markServiceOk(true))
            .catch(() => markServiceOk(false));
    }

    function engineSpec() {
        return config.engine === 'douzero' ? 'douzero:WP' : 'heuristic';
    }

    function currentGameId() {
        const root = document.getElementById('ddz');
        return root && root.dataset ? root.dataset.game || null : null;
    }

    function post(path, body) {
        return fetch(API + path, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        }).then((response) => response.json());
    }

    function markServiceOk(ok) {
        if (serviceOk === ok) return;
        serviceOk = ok;
        render();
    }

    // ---------------------------------------------------------------- 面板

    let host = null;
    let shadow = null;
    let ui = {};

    function ensurePanel() {
        if (host) return;
        host = document.createElement('div');
        host.id = 'ddz-copilot';
        host.style.cssText = 'position:fixed;z-index:2147483000;right:16px;bottom:16px;';
        shadow = host.attachShadow({mode: 'open'});
        shadow.innerHTML = TEMPLATE;
        applyStyles(shadow, STYLES);
        document.documentElement.appendChild(host);

        ui = {
            panel: shadow.querySelector('.panel'),
            banner: shadow.querySelector('.banner'),
            body: shadow.querySelector('.body'),
            toggle: shadow.querySelector('.collapse'),
            selectBtn: shadow.querySelector('.select'),
            autoBox: shadow.querySelector('.auto'),
            engines: Array.from(shadow.querySelectorAll('.engine')),
            header: shadow.querySelector('.header'),
            footer: shadow.querySelector('.footer'),
        };

        if (config.collapsed) ui.panel.classList.add('collapsed');
        ui.autoBox.checked = !!config.autoSelect;
        place();

        ui.toggle.addEventListener('click', () => {
            config.collapsed = !config.collapsed;
            ui.panel.classList.toggle('collapsed', config.collapsed);
            store.set('ddz-copilot-config', config);
        });
        ui.autoBox.addEventListener('change', () => {
            config.autoSelect = ui.autoBox.checked;
            store.set('ddz-copilot-config', config);
        });
        ui.selectBtn.addEventListener('click', () => { selectRecommended(); });
        ui.engines.forEach((button) => button.addEventListener('click', () => {
            const wanted = button.dataset.engine;
            if (config.engine === wanted) return;
            config.engine = wanted;
            store.set('ddz-copilot-config', config);
            advice = null;                 // 换引擎就重新问，别拿旧引擎的结论糊弄
            lastError = '';
            syncEngineButtons();
            scheduleAdvise();
            render();
        }));
        syncEngineButtons();
        makeDraggable();
        render();
    }

    function syncEngineButtons() {
        ui.engines.forEach((button) => {
            button.classList.toggle('active', button.dataset.engine === config.engine);
        });
    }

    function place() {
        if (config.left !== null && config.top !== null) {
            host.style.left = config.left + 'px';
            host.style.top = config.top + 'px';
            host.style.right = 'auto';
            host.style.bottom = 'auto';
        }
    }

    function makeDraggable() {
        let drag = null;
        ui.header.addEventListener('pointerdown', (event) => {
            if (event.target.closest('button, input, label')) return;
            const rect = host.getBoundingClientRect();
            drag = {x: event.clientX - rect.left, y: event.clientY - rect.top};
            ui.header.setPointerCapture(event.pointerId);
            event.preventDefault();
        });
        ui.header.addEventListener('pointermove', (event) => {
            if (!drag) return;
            const left = Math.max(0, Math.min(window.innerWidth - 60, event.clientX - drag.x));
            const top = Math.max(0, Math.min(window.innerHeight - 40, event.clientY - drag.y));
            host.style.left = left + 'px';
            host.style.top = top + 'px';
            host.style.right = 'auto';
            host.style.bottom = 'auto';
            config.left = Math.round(left);
            config.top = Math.round(top);
        });
        ui.header.addEventListener('pointerup', () => {
            if (!drag) return;
            drag = null;
            store.set('ddz-copilot-config', config);
        });
    }

    function render() {
        if (!host) return;
        const state = latestState;
        const data = advice && state && advice.version === state.version ? advice : null;

        // 横幅
        let banner = '等待牌局…';
        let kind = '';
        if (serviceOk === false) {
            banner = '服务未启动：先跑 python -m ddz.service';
            kind = 'bad';
        } else if (state) {
            if (state.phase === 'finished') {
                banner = '本局已结束';
            } else if (state.seat === null) {
                banner = '旁观中';
            } else if (myTurn()) {
                banner = '轮到你出牌';
                kind = 'good';
            } else if (state.turn !== null && state.players && state.players[state.turn]) {
                banner = `等待 ${state.players[state.turn].username} 出牌…`;
            } else {
                banner = '等待中…';
            }
        }
        ui.banner.textContent = banner;
        ui.banner.className = 'banner ' + (kind || (state && !myTurn() ? 'muted' : ''));

        // 身体
        ui.body.replaceChildren();
        const usable = !!data && data.ok;
        ui.selectBtn.disabled = !usable || !myTurn();

        if (usable) {
            if (data.bidding) {
                ui.body.append(biddingBlock(data.bidding));
            } else if (data.recommended) {
                ui.body.append(recommendBlock(data.recommended));
                if (data.alternatives && data.alternatives.length) {
                    ui.body.append(alternativesBlock(data.alternatives));
                }
            }
        } else if (data && data.reason === 'no_engine') {
            const note = el('div', 'note', data.message || 'DouZero 未就绪');
            note.append(el('div', 'note', '点上面的「启发式」就能继续用。'));
            ui.body.append(note);
        } else if (data && data.reason && data.reason !== 'not_my_turn') {
            ui.body.append(el('div', 'note', data.message || data.reason));
        } else if (!data && myTurn() && serviceOk !== false) {
            ui.body.append(el('div', 'note', adviseInFlight ? '正在计算…' : '准备建议…'));
        }

        // 记牌器与威胁：没轮到我时也有内容可看
        const counterSource = data || advice;
        if (counterSource && counterSource.card_counter) {
            ui.body.append(counterBlock(counterSource.card_counter));
        }
        if (counterSource && counterSource.threats && counterSource.threats.length) {
            ui.body.append(threatBlock(counterSource.threats));
        }
        if (counterSource && counterSource.hand_analysis) {
            ui.body.append(handBlock(counterSource.hand_analysis));
        }

        // 页脚
        const engine = counterSource && counterSource.engine ? counterSource.engine : null;
        const timing = counterSource && counterSource.timing_ms ? counterSource.timing_ms.total : null;
        const stale = state && advice && advice.version !== state.version;
        const parts = [
            engine && engine.name ? engine.name : '未连接',
            engine && engine.device ? engine.device : '',
            timing !== null && timing !== undefined ? `${Math.round(timing)}ms` : '',
            stale ? '建议已过期' : '',
        ].filter(Boolean);
        ui.footer.textContent = parts.join(' · ');
        ui.panel.classList.toggle('stale', !!stale);
    }

    /*
     * 样式走 CSSOM（constructable stylesheet），而不是 <style> 标签：
     * 游戏站点若带 style-src 严格 CSP，注入的 <style> 会被直接拦掉，面板就裸奔了。
     * adoptedStyleSheets 属于 CSSOM 操作，不受 CSP 的 style-src 约束。
     */
    function applyStyles(root, css) {
        try {
            const sheet = new CSSStyleSheet();
            sheet.replaceSync(css);
            root.adoptedStyleSheets = [sheet];
            return;
        } catch (e) { /* 老浏览器回退到 <style> */ }
        const style = document.createElement('style');
        style.textContent = css;
        root.append(style);
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function cardEl(id) {
        const img = document.createElement('img');
        img.src = `/images/games/doudizhu/${id}.webp`;
        img.alt = cardName(id);
        img.title = cardName(id);
        img.className = 'card-img';
        return img;
    }

    function cardName(id) {
        if (id === 52) return '小王';
        if (id === 53) return '大王';
        return `${RANKS[id >> 2]}${['♠', '♥', '♣', '♦'][id & 3]}`;
    }

    function recommendBlock(rec) {
        const box = el('div', 'block recommend');
        const head = el('div', 'row');
        if (rec.cards && rec.cards.length) {
            const cards = el('div', 'cards');
            rec.cards.forEach((id) => cards.append(cardEl(id)));
            head.append(cards);
        }
        head.append(el('div', 'desc', rec.description || ''));
        box.append(head);
        if (rec.rationale && rec.rationale.length) {
            const list = el('ul', 'reasons');
            rec.rationale.forEach((text) => list.append(el('li', '', text)));
            box.append(list);
        }
        return box;
    }

    function alternativesBlock(alts) {
        const box = el('div', 'block alternatives');
        box.append(el('div', 'label', '备选'));
        alts.forEach((alt) => {
            const row = el('div', 'alt');
            const cards = el('div', 'cards small');
            (alt.cards || []).forEach((id) => cards.append(cardEl(id)));
            if (!alt.cards || !alt.cards.length) cards.append(el('span', 'pass', '不出'));
            row.append(cards, el('span', 'alt-desc', alt.description || ''));
            const reason = (alt.rationale && alt.rationale[0]) || '';
            if (reason) row.append(el('span', 'alt-reason', reason));
            box.append(row);
        });
        return box;
    }

    function biddingBlock(bidding) {
        const box = el('div', 'block bidding');
        box.append(el('div', 'big', `建议：${bidding.label}`));
        (bidding.rationale || []).forEach((text) => box.append(el('div', 'note', text)));
        box.append(el('div', 'note', `可叫：${(bidding.legal || []).map((v) => (v === 0 ? '不叫' : `${v} 分`)).join(' / ')}`));
        return box;
    }

    function counterBlock(counter) {
        const box = el('div', 'block counter');
        box.append(el('div', 'label', `未见 ${counter.unseen_total} 张`));
        const grid = el('div', 'grid');
        const bombs = new Set(counter.possible_bombs || []);
        for (let rank = 14; rank >= 0; rank--) {
            const left = (counter.unseen_by_rank || [])[rank] || 0;
            const cell = el('div', 'cell' + (left === 0 ? ' zero' : '') + (bombs.has(rank) ? ' bomb' : ''),
                `${RANKS[rank]}${left}`);
            if (bombs.has(rank)) cell.title = '4 张都没出现，可能有炸弹';
            grid.append(cell);
        }
        box.append(grid);
        if (counter.rocket_possible) box.append(el('div', 'note', '双王未见，小心王炸'));
        return box;
    }

    function threatBlock(list) {
        const box = el('div', 'block threats');
        list.forEach((threat) => box.append(el('div', `threat ${threat.level}`, threat.text)));
        return box;
    }

    function handBlock(analysis) {
        const box = el('div', 'block hand');
        const bits = [`${analysis.n_plays} 手`];
        if (analysis.control && analysis.control.length) bits.push(`控制：${analysis.control.join('、')}`);
        if (analysis.bombs && analysis.bombs.length) bits.push(`炸弹：${analysis.bombs.join('、')}`);
        box.append(el('div', 'label', `我的手牌：${bits.join(' · ')}`));
        return box;
    }

    // ---------------------------------------------------------------- 帮我选中

    /*
     * 只做"选中"，绝不点出牌、绝不调接口。做法（PLAN §0.1 核实过）：
     *   - 先向 .ddz-table 派发 contextmenu 清空已有选择；
     *   - 再对每张建议牌派发 pointerdown + pointerup（合成 pointerId，页面里 setPointerCapture
     *     会抛异常但已被吞掉，drag 已经生效）；
     *   - 每次都会重绘手牌 DOM（replaceChildren），所以每轮都要重新查询节点；
     *     按 data-card 找牌与显示顺序无关，所以「从大到小 / 从小到大」两种排序都对。
     */
    async function selectRecommended() {
        if (!advice || !advice.ok || !advice.recommended) return;
        if (!latestState || latestState.phase !== 'playing' || !myTurn()) return;

        handEl = document.getElementById('ddz-hand');
        const table = document.querySelector('.ddz-table');
        if (!handEl || !table) return;

        const cards = advice.recommended.cards || [];
        if (!cards.length) return;             // 「不出」不需要选中任何牌

        table.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));
        await nextFrame();

        for (const id of cards) {
            const node = handEl.querySelector(`.ddz-card[data-card="${id}"]`);
            if (!node) continue;               // 手牌里没有这张（或已经打掉了）
            firePointer(node, 'pointerdown');
            await nextFrame();
            firePointer(node, 'pointerup');
            await nextFrame();
        }
    }

    function firePointer(node, type) {
        const rect = node.getBoundingClientRect();
        const event = new PointerEvent(type, {
            bubbles: true,
            cancelable: true,
            pointerId: 0,
            button: 0,
            buttons: type === 'pointerdown' ? 1 : 0,
            clientX: rect.left + rect.width / 2,
            clientY: rect.top + rect.height / 2,
        });
        try {
            node.dispatchEvent(event);
        } catch (e) { /* 页面处理器里的异常不该影响我们 */ }
    }

    function nextFrame() {
        return new Promise((resolve) => requestAnimationFrame(() => resolve()));
    }

    // ---------------------------------------------------------------- 启动

    function boot() {
        ensurePanel();
        bootstrap();
        // 建议是"按需"的：轮到自己时页面本来就在 900ms 轮询，不需要自己再加定时器。
        // 只有服务掉线时才需要自己探活，好把"服务未启动"显示出来。
        setInterval(() => {
            if (!latestState || serviceOk === false) {
                nativeFetch(API + '/health').then((r) => markServiceOk(r.ok)).catch(() => markServiceOk(false));
            }
        }, HEALTH_MS);
    }

    const STYLES = `
            :host { all: initial; }
            .panel {
                font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
                width: 288px; background: rgba(24, 27, 32, .96); color: #e8eaed;
                border: 1px solid rgba(255,255,255,.12); border-radius: 10px;
                box-shadow: 0 8px 28px rgba(0,0,0,.45); overflow: hidden;
                backdrop-filter: blur(6px); user-select: none;
            }
            .panel.stale { opacity: .55; }
            .header {
                display: flex; align-items: center; gap: 6px; padding: 7px 10px;
                background: rgba(255,255,255,.06); cursor: move; font-weight: 600;
            }
            .header .title { flex: 1; font-size: 12px; letter-spacing: .3px; }
            .header button {
                all: unset; cursor: pointer; padding: 0 6px; border-radius: 4px;
                color: #cfd3d8; font-size: 13px; line-height: 18px;
            }
            .header button:hover { background: rgba(255,255,255,.14); }
            .banner { padding: 6px 10px; font-size: 12px; background: rgba(255,255,255,.04); }
            .banner.good { background: rgba(56, 142, 60, .35); color: #d7ffd9; }
            .banner.bad { background: rgba(198, 40, 40, .4); color: #ffe1e1; }
            .banner.muted { color: #9aa0a6; }
            .panel.collapsed .body, .panel.collapsed .footer, .panel.collapsed .toolbar { display: none; }
            .body { padding: 8px 10px 4px; display: flex; flex-direction: column; gap: 8px; max-height: 60vh; overflow-y: auto; }
            .block { border-top: 1px solid rgba(255,255,255,.08); padding-top: 7px; }
            .block:first-child { border-top: 0; padding-top: 0; }
            .label { color: #9aa0a6; font-size: 11px; margin-bottom: 3px; }
            .big { font-size: 15px; font-weight: 700; color: #ffd54f; }
            .note { color: #bdc1c6; font-size: 12px; }
            .cards { display: flex; flex-wrap: wrap; gap: 2px; }
            .cards.small .card-img { height: 30px; }
            .card-img { height: 42px; border-radius: 3px; display: block; }
            .desc { font-weight: 700; color: #ffd54f; margin-top: 4px; }
            .reasons { margin: 5px 0 0; padding-left: 16px; color: #c8ccd1; }
            .reasons li { margin: 1px 0; }
            .alt { display: flex; align-items: center; gap: 5px; margin: 3px 0; }
            .alt-desc { color: #e8eaed; }
            .alt-reason { color: #8f9499; font-size: 11px; margin-left: auto; text-align: right; }
            .pass { color: #9aa0a6; padding: 4px 6px; }
            .grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 3px; }
            .cell {
                text-align: center; font-size: 11px; padding: 2px 0; border-radius: 3px;
                background: rgba(255,255,255,.06); color: #dfe1e5;
            }
            .cell.zero { opacity: .3; }
            .cell.bomb { background: rgba(198, 40, 40, .5); color: #fff; }
            .threat { font-size: 12px; padding: 2px 0; }
            .threat.info { color: #9aa0a6; }
            .threat.warn { color: #ffcc80; }
            .threat.danger { color: #ff8a80; font-weight: 600; }
            .toolbar {
                display: flex; align-items: center; gap: 8px; padding: 6px 10px;
                border-top: 1px solid rgba(255,255,255,.08);
            }
            .toolbar button {
                all: unset; cursor: pointer; padding: 3px 10px; border-radius: 5px;
                background: #2f6feb; color: #fff; font-size: 12px;
            }
            .toolbar.engines { gap: 4px; padding-bottom: 2px; }
            .toolbar.engines button {
                flex: 1; text-align: center; background: rgba(255,255,255,.08);
                color: #c8ccd1; border: 1px solid transparent;
            }
            .toolbar.engines button.active {
                background: rgba(47,111,235,.28); color: #fff; border-color: #2f6feb;
            }
            .toolbar button[disabled] { background: rgba(255,255,255,.12); color: #9aa0a6; cursor: default; }
            .toolbar label { display: flex; align-items: center; gap: 4px; color: #9aa0a6; font-size: 11px; cursor: pointer; }
            .footer { padding: 5px 10px 7px; color: #8f9499; font-size: 11px; }
    `;

    const TEMPLATE = `
        <div class="panel">
            <div class="header">
                <span class="title">斗地主 Copilot</span>
                <button class="collapse" title="折叠/展开">–</button>
            </div>
            <div class="banner"></div>
            <div class="body"></div>
            <div class="toolbar engines">
                <button class="engine" data-engine="heuristic">启发式</button>
                <button class="engine" data-engine="douzero">DouZero</button>
            </div>
            <div class="toolbar">
                <button class="select" disabled>帮我选中</button>
                <label><input type="checkbox" class="auto"> 轮到我时自动选</label>
            </div>
            <div class="footer"></div>
        </div>
    `;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot, {once: true});
    } else {
        boot();
    }
})();
