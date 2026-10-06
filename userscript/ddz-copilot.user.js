// ==UserScript==
// @name         斗地主 Copilot
// @namespace    ddz-copilot
// @version      0.4.2
// @description  真实对局中实时给出可解释的出牌建议；可选代打（叫分/出牌、续局各自独立开关）
// @author       you
// @include      /^https?:\/\/[^/]+\/games\/doudizhu/
// @run-at       document-start
// @grant        none
// @noframes
// ==/UserScript==

/*
 * 设计约束（改动前先读一遍）：
 *
 * 1. 页面状态在 IIFE 闭包里，外面拿不到 → 唯一的信息来源是拦截 window.fetch。
 *    **绝不改写请求或响应**：只是 clone 一份自己看。另外兜底读 #ddz-initial。
 * 2. **代打是后来加的，PLAN §3.6 里"只建议、绝不点出牌"那句已经被用户明确推翻**，
 *    别再照那句改回去。现在的底线是：代打只**驱动页面自己的按钮**，绝不自己构造
 *    play/pass/bid 打给游戏接口、不改轮询频率、不碰任何协议。四条护栏见「自动执行」那一段。
 *    （两个开关：`auto` 管叫分+出牌，`autoContinue` 管续局，互不影响。）
 * 3. 「帮我选中」是手动版：只替你在手牌上做选择，点不点出牌由你决定。
 * 4. 服务没起或解析失败时，面板要显示「服务未启动 / 建议暂不可用」，不能静默什么都不显示。
 *    但**别在没在算的时候说"正在算"**——状态行的每种说法都必须对应真实状态。
 */

(() => {
    'use strict';

    //: 面板上会显示它——"我到底更新成功没有"要能一眼看出来（必须与头部 @version 一致）
    const VERSION = '0.4.2';
    const API = 'http://127.0.0.1:17876';
    const HEALTH_MS = 5000;
    const DEBOUNCE_MS = 150;
    //: 不变量心跳的间隔（**必须大于 DEBOUNCE_MS**，见启动处那段注释）
    const HEARTBEAT_MS = 1000;
    //: 建议请求多久没回来就当它挂了。服务端自己 400ms 就会超时回落到启发式，
    //: 所以正常绝不会摸到这个值；它防的是连接被吞掉这种"永远不回来"的情况。
    const ADVISE_TIMEOUT_MS = 4000;
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

    // engine: 'heuristic' | 'douzero'（两个引擎选项）
    // auto: 代打开关——叫分 + 出牌；autoContinue: 终局后自己点「再来一局」，**独立开关**
    //       （两者互不影响：只续局不代打是合法组合）；delayMs 是出手前的可视延迟
    const config = Object.assign(
        {collapsed: false, engine: 'heuristic', left: null, top: null,
         auto: true, autoContinue: true, delayMs: 700},
        store.get('ddz-copilot-config', {}),
    );

    // ---------------------------------------------------------------- 运行时状态

    let latestState = null;      // 平台最新一份 state
    let lastVersion = -1;
    let advice = null;           // 最新一条建议（带 version）
    let adviseInFlight = false;
    let adviseTimer = null;
    let lastError = '';
    let serviceOk = null;        // health 探活的结果：null 未知 / true / false。**只看它**
    let requestFailures = 0;     // 连续失败的建议请求数（偶发一次不算服务挂了）
    let ingestedVersion = -1;
    let handEl = null;
    let lastActedVersion = -1;   // 已经代为出手过的局面（防重复点击）
    let continuedOnce = false;   // 续局只点一次
    let adviseWatchdog = null;   // 在飞请求的看门狗：挂死了要能自己解套
    let adviseToken = 0;         // 回执令牌：只有最新那次请求的收尾算数
    let missedSchedules = 0;     // 心跳补排了几次（>0 就说明有路径漏排了）
    let pending = null;          // 待执行动作：{version, timer, label}

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
        cancelPending();                           // 局面变了：上一个待执行动作作废
        if (state.version < lastVersion) {
            // version 只在一局之内递增；退回去说明换局了（整页跳转之外的情况也兜住）
            advice = null;
            ingestedVersion = -1;
            lastError = '';
            continuedOnce = false;   // 新的一局：续局标记复位（不靠整页跳转也能接着自动续）
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
            if (config.autoContinue) scheduleContinue();
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
        if (latestState.phase === 'finished' || gameLooksOver()) return;   // 已经结束了
        if (!myTurn()) return;                                       // 没轮到：显示等待
        if (advice && advice.version === latestState.version) return; // 这个局面已经有建议了
        adviseTimer = setTimeout(requestAdvise, DEBOUNCE_MS);
    }

    /** 收尾只认**最新**那次请求的回执：看门狗判超时后重发过的话，
     *  姗姗来迟的旧响应不许把新请求的在飞状态清掉。 */
    function finishAdvise(token) {
        if (token !== adviseToken) return;
        adviseInFlight = false;
        clearTimeout(adviseWatchdog);
        adviseWatchdog = null;
    }

    function requestAdvise() {
        if (!latestState || !myTurn()) return;
        if (latestState.phase === 'finished' || gameLooksOver()) return;
        if (advice && advice.version === latestState.version) return;   // 这个局面已经有建议了
        // 有请求在飞：**不能就这么算了**。它回来时会自己再排一次（见 .then 里的 scheduleAdvise），
        // 但要是它挂死了（连接被吞、服务端卡住），就必须靠下面的看门狗解套，
        // 否则面板会永远停在「正在算…」——这正是"有概率卡住、过一会又好了"的成因。
        if (adviseInFlight) return;
        const version = latestState.version;
        const token = ++adviseToken;
        adviseInFlight = true;
        render();          // 立刻把"正在算…"显示出来：此刻确实有一个请求在飞
        adviseWatchdog = setTimeout(() => {
            if (token !== adviseToken) return;
            finishAdvise(token);
            lastError = '建议请求超时，正在重试';
            render();
            scheduleAdvise();
        }, ADVISE_TIMEOUT_MS);
        post('/advise', {
            session,
            game_id: currentGameId(),
            state: latestState,
            options: {agent: engineSpec()},
        })
            .then((data) => {
                finishAdvise(token);
                markServiceOk(true);
                lastError = '';
                if (data && (!latestState || latestState.version === version)) {
                    advice = Object.assign({version}, data);
                    if (!data.ok) lastError = data.message || data.reason || '';
                    render();
                    autoAct(data);
                }
                // 一问一答期间的任何情况都在这里收口：局面翻页了就立刻为新局面再算一次，
                // 已经不缺建议时 scheduleAdvise 自己会短路，不会空转。
                scheduleAdvise();
            })
            .catch((err) => {
                finishAdvise(token);
                requestFailures += 1;
                lastError = `建议请求失败（第 ${requestFailures} 次）：${String(err && err.message ? err.message : err)}`;
                render();
                // 浏览器复用已被服务端关掉的 keep-alive 连接时会偶发这种失败：
                // 退避重试几次，别急着说"服务未启动"。
                if (requestFailures <= 3) setTimeout(requestAdvise, 300 * requestFailures);
                else probeHealth();
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
        if (ok) requestFailures = 0;
        if (serviceOk !== ok) {
            serviceOk = ok;
            render();
        }
        // 只要确认服务在，就补算当前局面。**不能只在"状态翻转"时补**：连续失败几次后
        // 重试会放弃，那时 serviceOk 一直是真的，翻转判断就永远不会再触发，
        // 这个局面就被永远留在"没有建议"上了。重复调用会被 scheduleAdvise 自己短路。
        if (ok) scheduleAdvise();
    }

    /** health 探活：它才是"服务在不在"的权威，单次 /advise 失败不作数。 */
    function probeHealth() {
        nativeFetch(API + '/health', {cache: 'no-store'})
            .then((response) => markServiceOk(response.ok))
            .catch(() => markServiceOk(false));
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
            mini: shadow.querySelector('.mini'),
            body: shadow.querySelector('.body'),
            toggle: shadow.querySelector('.collapse'),
            selectBtn: shadow.querySelector('.select'),
            masterBtn: shadow.querySelector('.auto-master'),
            nextBtn: shadow.querySelector('.auto-next'),
            status: shadow.querySelector('.status'),
            engines: Array.from(shadow.querySelectorAll('.engine')),
            header: shadow.querySelector('.header'),
            ver: shadow.querySelector('.ver'),
            footer: shadow.querySelector('.footer'),
        };

        if (ui.ver) ui.ver.textContent = 'v' + VERSION;
        if (config.collapsed) ui.panel.classList.add('collapsed');
        ui.masterBtn.addEventListener('click', () => {
            config.auto = !config.auto;
            if (!config.auto) cancelPending();
            store.set('ddz-copilot-config', config);
            render();
            if (config.auto) scheduleAdvise();   // 刚打开时可能已经轮到我了
        });
        place();

        ui.toggle.addEventListener('click', () => {
            config.collapsed = !config.collapsed;
            ui.panel.classList.toggle('collapsed', config.collapsed);
            store.set('ddz-copilot-config', config);
        });
        ui.nextBtn.addEventListener('click', () => {
            config.autoContinue = !config.autoContinue;
            store.set('ddz-copilot-config', config);
            render();
            if (config.autoContinue && latestState && latestState.phase === 'finished') {
                scheduleContinue();   // 打开时这局可能已经结束了
            }
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
        // 终局可能有两副面孔：state 说结束了，或者页面已经把结算画面摆出来了
        const over = !!state && (state.phase === 'finished' || gameLooksOver());

        // 横幅
        let banner = '等待牌局…';
        let kind = '';
        if (serviceOk === false) {
            banner = '服务未启动：先跑 python -m ddz.service';
            kind = 'bad';
        } else if (!state) {
            banner = '等待牌局…';
        } else if (state) {
            if (over) {
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
        ui.banner.className = 'banner ' + (kind || (state && !over && !myTurn() ? 'muted' : ''));

        // 折叠时的建议条：收起来了，建议也得看得见
        ui.mini.replaceChildren();
        if (config.collapsed && data && data.ok) {
            if (data.bidding) {
                ui.mini.append(el('span', 'desc', `建议：${data.bidding.label}`));
            } else if (data.recommended) {
                const cards = el('div', 'cards small');
                (data.recommended.cards || []).forEach((id) => cards.append(cardEl(id)));
                if (!data.recommended.cards || !data.recommended.cards.length) {
                    cards.append(el('span', 'pass', '不出'));
                }
                ui.mini.append(cards, el('span', 'desc', data.recommended.description || ''));
            }
        } else if (config.collapsed && data && data.ok && data.solver && data.solver.used) {
            ui.mini.append(el('span', 'desc', '残局求解 · 必胜'));
        } else if (config.collapsed && data && data.reason === 'no_engine') {
            ui.mini.append(el('span', 'note', 'DouZero 未就绪，点开切回启发式'));
        }

        // 身体
        ui.body.replaceChildren();
        const usable = !!data && data.ok;
        ui.selectBtn.disabled = !usable || !myTurn();

        if (usable) {
            if (data.bidding) {
                ui.body.append(biddingBlock(data.bidding));
            } else if (data.recommended) {
                ui.body.append(recommendBlock(data.recommended));
                const verdict = solverVerdict(data.solver);
                if (verdict) ui.body.append(verdict);
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
        } else if (over) {
            // 结算画面：这里绝不能再写"正在计算"
            ui.body.append(el('div', 'note', config.autoContinue ? '本局已结束，准备续局…' : '本局已结束'));
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
        if (counterSource && counterSource.disagreement) {
            ui.body.append(disagreementBlock(counterSource.disagreement));
        }

        // 总开关 + 状态行
        ui.masterBtn.textContent = `自动打牌：${config.auto ? '开' : '关'}`;
        ui.masterBtn.classList.toggle('on', !!config.auto);
        ui.nextBtn.textContent = `自动续局：${config.autoContinue ? '开' : '关'}`;
        ui.nextBtn.classList.toggle('on', !!config.autoContinue);
        if (!config.auto) {
            ui.status.textContent = '纯建议模式（不代打）';
            ui.status.className = 'status';
        } else if (serviceOk === false) {
            // 服务都没了还说"正在算…"是这个面板最误导人的地方：它压根没在算。
            ui.status.textContent = '服务未启动，代打暂停';
            ui.status.className = 'status bad';
        } else if (pending) {
            ui.status.textContent = `⏳ ${pending.label}`;
            ui.status.className = 'status';
        } else if (lastError) {
            ui.status.textContent = `⚠ ${lastError}`;
            ui.status.className = 'status';
        } else if (over) {
            ui.status.textContent = !config.autoContinue
                ? '本局已结束（续局已关）'
                : (continuedOnce ? '已点续局' : '准备续局…');
            ui.status.className = config.autoContinue ? 'status active' : 'status';
        } else if (!data && myTurn()) {
            // 只有**真有请求在飞**才说"正在算"；没在算却说正在算，就是面板在骗人
            ui.status.textContent = adviseInFlight ? '自动 · 正在算…' : '自动 · 等待建议…';
            ui.status.className = 'status active';
        } else {
            ui.status.textContent = '自动 · 叫分 / 出牌 / 续局';
            ui.status.className = 'status active';
        }

        // 页脚
        const engine = counterSource && counterSource.engine ? counterSource.engine : null;
        const timing = counterSource && counterSource.timing_ms ? counterSource.timing_ms.total : null;
        const stale = state && advice && advice.version !== state.version;
        const solvedBySolver = counterSource && counterSource.solver && counterSource.solver.used;
        // 拿不到引擎名 ≠ 服务断了。没有引擎名（还没算过、或这份响应里没人出建议）时就写
        // "会话配的是哪个引擎"；只有探活失败才写"未连接"——和横幅"服务未启动"同口径。
        const configured = config.engine === 'douzero' ? 'douzero:WP' : 'heuristic';
        const parts = [
            solvedBySolver ? '残局求解'
                : (engine && engine.name ? engine.name
                    : (serviceOk === false ? '未连接' : configured)),
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

    /** 残局求解的结论：**证明过**的必胜/必败，和"估计"不是一回事，所以单独一行。 */
    function solverVerdict(solver) {
        if (!solver || !solver.proved) return null;
        const box = el('div', 'block verdict');
        if (solver.winning) {
            box.append(el('div', 'verdict-line win',
                `残局求解：这一手必胜（搜了 ${solver.nodes} 个局面，${Math.round(solver.ms)}ms）`));
        } else {
            box.append(el('div', 'verdict-line lose',
                '残局求解：本局已必败——对方不出错就赢不了，尽力争取他失误'));
        }
        return box;
    }

    /** 策略分歧：两个引擎首选不一致，把两边都摆出来让人自己判断。 */
    function disagreementBlock(info) {
        const box = el('div', 'block disagree');
        box.append(el('div', 'label', '策略分歧'));
        box.append(el('div', 'note', `DouZero：${info.engine.description}`));
        box.append(el('div', 'note', `启发式：${info.heuristic.description}`));
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

    // ---------------------------------------------------------------- 自动执行

    /*
     * 代打的全部动作都在这里，规矩：
     *   1. 只按自己的建议动手，建议必须属于**当前** version，过期就不动；
     *   2. 出手前留一段可视延迟（默认 700ms），期间你关掉开关或在牌桌上点一下都能取消；
     *   3. 选完牌先核对 DOM 里真的选中了建议的牌，对不上就放弃、绝不提交；
     *   4. 局面一变（version 前进）立刻取消待执行动作。
     */

    /** 结算弹窗可能比状态晚一拍才渲染出来，所以续局要试几次。 */
    function scheduleContinue(attempt = 0) {
        if (!config.autoContinue || continuedOnce || attempt > 8) return;
        setTimeout(() => {
            if (continuedOnce) return;
            autoContinue();
            if (!continuedOnce) scheduleContinue(attempt + 1);
        }, attempt === 0 ? 1200 : 500);
    }

    function cancelPending() {
        if (pending) {
            clearTimeout(pending.timer);
            pending = null;
        }
    }

    /** 排一个待执行动作。``run()`` 返回 false 表示没做成（DOM 还没就绪之类），会重试几次。 */
    function schedule(label, version, run, attempt = 0) {
        if (attempt === 0) cancelPending();
        pending = {version, label};
        pending.timer = setTimeout(() => {
            const action = pending;
            pending = null;
            if (!action) return;
            if (!latestState || latestState.version !== action.version) return;  // 局面已经走了
            let ok = false;
            try {
                ok = run() !== false;
            } catch (err) {
                lastError = String(err && err.message ? err.message : err);
                render();
            }
            // 没做成多半是页面还没把按钮/手牌渲染出来——隔一会儿再试，直到局面变化
            if (!ok && attempt < 6) schedule(label, version, run, attempt + 1);
        }, attempt === 0 ? Math.max(0, config.delayMs || 0) : 250);
        if (attempt === 0) render();
    }

    function actionButtons() {
        const bar = document.getElementById('ddz-actions');
        return bar ? Array.from(bar.querySelectorAll('button')) : [];
    }

    function visibleButton(predicate) {
        return actionButtons().find((b) => {
            if (b.disabled || b.hidden) return false;
            const style = window.getComputedStyle(b);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            return predicate(b.textContent.trim());
        }) || null;
    }

    function autoAct(data) {
        if (!config.auto) return;
        if (!data || !data.ok || !latestState) return;
        if (latestState.version === lastActedVersion) return;   // 这个局面已经动过手
        if (latestState.phase === 'finished') return;

        if (data.bidding) {
            const label = data.bidding.label;                   // 「不叫」或「N 分」
            schedule(`自动叫分：${label}`, latestState.version, () => {
                const button = visibleButton((text) => text === label);
                if (!button) return;
                lastActedVersion = latestState.version;
                button.click();
            });
            return;
        }

        if (data.recommended) {
            const description = data.recommended.description;
            schedule(`自动出牌：${description}`, latestState.version, () => {
                const played = executeRecommended(data.recommended);
                if (played) lastActedVersion = latestState.version;
            });
        }
    }

    /** 按建议动手：不出就点「不出」，出牌就先选牌再核对，最后点「出牌」。 */
    function executeRecommended(recommended) {
        if (!latestState || latestState.phase !== 'playing' || !myTurn()) return false;

        if (!recommended.cards || !recommended.cards.length) {
            const passButton = visibleButton((text) => text.indexOf('不出') !== -1);
            if (!passButton) return false;
            passButton.click();
            return true;
        }

        handEl = document.getElementById('ddz-hand');
        const table = document.querySelector('.ddz-table');
        if (!handEl || !table) return false;

        table.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));
        for (const id of recommended.cards) {
            const node = handEl.querySelector(`.ddz-card[data-card="${id}"]`);
            if (!node) return false;                            // 手牌里没有这张，别瞎点
            firePointer(node, 'pointerdown');
            firePointer(node, 'pointerup');
        }

        // 核对：DOM 里真的选中了这些牌吗？
        const selected = Array.from(handEl.querySelectorAll('.ddz-card.ddz-selected'))
            .map((node) => Number(node.dataset.card)).sort((a, b) => a - b);
        const wanted = recommended.cards.slice().sort((a, b) => a - b);
        if (selected.join(',') !== wanted.join(',')) {
            lastError = '选中的牌与建议不一致，放弃这一手';
            render();
            return false;
        }

        // 「出牌」按钮必须在**选完牌之后**重新查：它是 renderActions() 新建出来的，
        // 选牌前那一份是 disabled 的（没选牌时页面本来就不让你出）。
        const playButton = visibleButton((text) => text === '出牌');
        if (!playButton) {
            lastError = '没找到可用的「出牌」按钮';
            render();
            return false;
        }

        // 多种读法时，把「按哪种牌型打出」的下拉框拨到我们要的那种
        const chooser = document.querySelector('#ddz-actions select.ddz-choice');
        if (chooser && recommended.platform_key) {
            const option = Array.from(chooser.options).find((o) => o.value === recommended.platform_key);
            if (!option) {
                lastError = '页面上没有建议的那种牌型读法，放弃这一手';
                render();
                return false;
            }
            chooser.value = option.value;
            chooser.dispatchEvent(new Event('change', {bubbles: true}));
        }

        playButton.click();
        return true;
    }

    /** 是不是"续局"按钮（和参考脚本同一套文本约定）。 */
    function isContinueText(text) {
        return (text.indexOf('→') !== -1 || text.indexOf('再来一局') !== -1
            || text.indexOf('再来一场比赛') !== -1) && text.indexOf('发送') === -1;
    }

    function continueButton() {
        const boxes = [document.getElementById('ddz-result'), document.getElementById('ddz-actions')];
        for (const box of boxes) {
            if (!box) continue;
            const button = Array.from(box.querySelectorAll('button')).find((b) => {
                // **必须真的可见**：平台可能把「再来一局」一直留在 DOM 里（游戏中隐藏着），
                // 只看 disabled 的话，面板会全程以为这局已经打完了。
                if (b.disabled || b.hidden) return false;
                const style = window.getComputedStyle(b);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                return isContinueText(b.textContent.trim());
            });
            if (button) return button;
        }
        return null;
    }

    /** 页面自己已经说了"这局结束了"：结算弹窗里出现了「再来一局」。
     *
     *  **不能只信 state.phase**——平台在结算时可能就不再推终局 state 了，那样面板会一直停在
     *  最后一帧"轮到我"的老局面上，向一个永远不会来的建议反复发请求（用户看到的就是
     *  "自动 · 正在算"卡在结算画面，而且自动续局也不点）。DOM 是这里唯一可靠的事实来源。 */
    function gameLooksOver() {
        return !!continueButton();
    }

    /** 终局后自动进入下一局 / 下一场。 */
    function autoContinue() {
        if (!config.autoContinue || continuedOnce) return;
        const button = continueButton();
        if (button) {
            continuedOnce = true;
            button.click();
        }
    }

    // ---------------------------------------------------------------- 帮我选中

    /*
     * 这一段只做"选中"：它自己绝不点出牌、绝不调接口（代打在「自动执行」那一段）。
     * 做法（PLAN §0.1 核实过）：
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
        probeHealth();
        setInterval(() => {
            if (serviceOk !== true || !latestState) probeHealth();
        }, HEALTH_MS);
        // 不变量心跳：**只要"轮到我、这个局面还没有建议"，就必须有请求在飞或在排队**。
        // 光靠"状态来了就排一次、响应回来再排一次"是不够的——任何一条路径漏排，面板就会
        // 永远停在"正在算…/等待建议…"上（用户真踩到了）。这一跳把漏排兜住：scheduleAdvise
        // 自己会把"已经有建议 / 没轮到我 / 旁观"短路掉，所以正常情况它是空转。
        // 间隔必须**大于**防抖时间，否则会一次次把定时器重置掉、请求永远发不出去。
        setInterval(() => {
            if (!latestState) return;
            // 结算画面出来了但平台没推终局 state：该续局就续局，别干等
            if (config.autoContinue && !continuedOnce && gameLooksOver()) {
                scheduleContinue();
                return;
            }
            if (!myTurn()) return;
            if (latestState.phase === 'finished' || gameLooksOver()) return;
            if (advice && advice.version === latestState.version) return;
            if (adviseInFlight) return;
            // 走到这儿 = 有人漏排了。补上，并在控制台留一笔——真再出现"卡住"，
            // 打开 F12 看有没有这行就知道是不是这条路径。
            missedSchedules += 1;
            console.warn(`[ddz-copilot] 漏排的建议请求，已自动补上（第 ${missedSchedules} 次）`);
            scheduleAdvise();
        }, HEARTBEAT_MS);
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
            /* 折叠时只留横幅的话，轮到我了也只看得见"轮到你出牌"——建议本身必须还在 */
            .mini { display: none; }
            .panel.collapsed .mini {
                display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
                padding: 6px 10px; background: rgba(255,255,255,.03);
            }
            .mini .card-img { height: 30px; }
            .mini .desc { margin-top: 0; font-size: 12px; }
            .mini .note { font-size: 11px; }
            .verdict-line { font-size: 12px; padding: 3px 6px; border-radius: 4px; }
            .verdict-line.win { background: rgba(56,142,60,.3); color: #d7ffd9; font-weight: 600; }
            .verdict-line.lose { background: rgba(198,40,40,.28); color: #ffe1e1; font-weight: 600; }
            .disagree .note { color: #ffcc80; }
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
            .toolbar.toggles { flex-wrap: wrap; gap: 8px; padding-top: 2px; }
            .toolbar.toggles label { color: #cfd3d8; }
            .toolbar.master { padding: 6px 10px 2px; gap: 6px; }
            .toolbar.master button {
                flex: 1; text-align: center; font-size: 13px; padding: 6px 10px;
                background: rgba(255,255,255,.12); color: #c8ccd1;
            }
            .toolbar.master button.on { background: #2f6feb; color: #fff; font-weight: 600; }
            .status { padding: 3px 10px 0; font-size: 11px; color: #ffcc80; min-height: 14px; }
            .status.active { color: #81c995; }
            .status.bad { color: #ff8a80; font-weight: 600; }
            .header .ver { color: #8f9499; font-weight: 400; font-size: 11px; }
            .footer { padding: 5px 10px 7px; color: #8f9499; font-size: 11px; }
    `;

    const TEMPLATE = `
        <div class="panel">
            <div class="header">
                <span class="title">斗地主 Copilot <span class="ver"></span></span>
                <button class="collapse" title="折叠/展开">–</button>
            </div>
            <div class="banner"></div>
            <div class="mini"></div>
            <div class="body"></div>
            <div class="toolbar engines">
                <button class="engine" data-engine="heuristic">启发式</button>
                <button class="engine" data-engine="douzero">DouZero</button>
            </div>
            <div class="toolbar master">
                <button class="auto-master">自动打牌：开</button>
                <button class="auto-next">自动续局：开</button>
            </div>
            <div class="toolbar">
                <button class="select" disabled>帮我选中</button>
            </div>
            <div class="status"></div>
            <div class="footer"></div>
        </div>
    `;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot, {once: true});
    } else {
        boot();
    }
})();
