(() => {
    'use strict';
    const root = document.getElementById('ddz');
    if (!root) return;
    const R = window.DoudizhuRules;
    const initial = JSON.parse(document.getElementById('ddz-initial').textContent);
    const isAdmin = initial.admin;
    const gameId = root.dataset.game, token = root.dataset.token, loggedIn = root.dataset.loggedIn === '1';
    const $ = id => document.getElementById(id);
    const SUITS = ['♠', '♥', '♣', '♦'];
    const store = {
        get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
        set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } },
    };

    let state = initial.state;
    const bottomSeat = state.seat === null ? 0 : state.seat;
    let selected = new Set();
    let hints = null, hintIndex = -1, choice = null;
    let busy = false, deadlineAt = 0, pollTimer = null, drag = null, resultShown = false;
    let sortDesc = store.get('ddz-sort') !== 'asc';
    // Opening a finished game from the history shows the table first; the result pops up only live.
    const openedFinished = state.phase === 'finished';
    const baseTitle = document.title;
    // Super users receive every hand but see other players' cards only after switching this on.
    let showAll = false;
    // Replay of a finished game: `state` is then a reconstruction at replay.index, and the real
    // final state waits in replay.final. Hands are always face up in a replay.
    const replay = {active: false, final: null, steps: null, index: 0, playing: false, timer: null,
        speed: Number(store.get('ddz-replay-speed')) || 1};
    const showHands = () => replay.active || state.phase === 'finished' || (!!state.hands && showAll);

    const posOf = seat => seat === bottomSeat ? 'bottom' : seat === (bottomSeat + 1) % 3 ? 'right' : 'left';
    const nameOf = seat => state.players[seat].username;
    const isPlayer = () => state.seat !== null;
    const myTurn = () => isPlayer() && state.turn === state.seat && state.phase !== 'finished';
    const toBeat = () => state.phase === 'playing' && !state.leading ? state.last.pattern : null;

    function cardName(id) {
        if (id === 52) return '小王';
        if (id === 53) return '大王';
        return SUITS[id & 3] + R.RANK_NAMES[R.rank(id)];
    }
    function el(tag, className, text) {
        const e = document.createElement(tag);
        if (className) e.className = className;
        if (text !== undefined) e.textContent = text;
        return e;
    }
    function cardEl(id, extra) {
        const d = el('div', 'ddz-card' + (extra ? ' ' + extra : ''));
        d.dataset.card = id;
        const img = el('img');
        img.src = `/images/games/doudizhu/${id}.webp`;
        img.alt = cardName(id);
        img.draggable = false;
        d.title = cardName(id);
        d.append(img);
        return d;
    }
    function backEl(extra) {
        const d = el('div', 'ddz-card ddz-back' + (extra ? ' ' + extra : ''));
        const img = el('img');
        img.src = '/images/games/doudizhu/back.webp';
        img.alt = '牌背';
        img.draggable = false;
        d.append(img);
        return d;
    }
    // Display order: by rank then suit, big cards first unless the player chose ascending.
    function ordered(cards) {
        const out = R.sortCards(cards);
        return sortDesc ? out.reverse() : out;
    }
    function cardRow(cards, className) {
        const row = el('div', 'ddz-cards ' + (className || ''));
        ordered(cards).forEach(id => row.append(cardEl(id)));
        return row;
    }

    // ------------------------------------------------------------------ network

    function toast(message, kind) {
        const t = $('ddz-toast');
        t.textContent = message;
        t.className = 'ddz-toast ' + (kind || '');
        t.hidden = false;
        clearTimeout(toast.timer);
        toast.timer = setTimeout(() => { t.hidden = true; }, 2600);
    }

    async function api(action, extra) {
        const body = new URLSearchParams({_token: token, action, game: gameId, version: String(state.version),
            chat_after: String(lastChatId), ...(extra || {})});
        const response = await fetch('/games/doudizhu/api', {method: 'POST', body, credentials: 'same-origin'});
        let data = null;
        try { data = await response.json(); } catch (e) { /* not JSON */ }
        if (!response.ok || !data) throw new Error(data && data.error ? data.error : '网络错误，请稍后重试。');
        if (data.chat) receiveChat(data.chat, true);
        return data;
    }

    function accept(next) {
        if (replay.active) {
            // Keep the replay on screen; a newer final state takes over when it ends.
            if (!next.unchanged) replay.final = next;
            return;
        }
        if (next.unchanged) {
            if (next.remaining !== null) deadlineAt = performance.now() + next.remaining;
            return;
        }
        const changed = next.version !== state.version;
        state = next;
        deadlineAt = state.remaining === null ? 0 : performance.now() + state.remaining;
        if (changed) {
            hints = null;
            hintIndex = -1;
        }
        render();
    }

    async function act(action, extra) {
        if (busy) return;
        busy = true;
        renderActions();
        try {
            const data = await api(action, extra);
            if (data.error) toast(data.error, 'error');
            if (data.state) {
                if (!data.error && action === 'play') selected.clear();
                accept(data.state);
            }
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            busy = false;
            renderActions();
        }
    }

    // Players keep polling (slowly) after the game so post-game chat still arrives.
    const shouldPoll = () => loggedIn && (state.phase !== 'finished' || isPlayer());
    async function poll() {
        clearTimeout(pollTimer);
        if (!shouldPoll()) return;
        try {
            if (!busy) {
                const data = await api('state');
                if (data.state) accept(data.state);
            }
        } catch (e) {
            // Transient failures are retried by the next poll.
        }
        if (shouldPoll()) pollTimer = setTimeout(poll, document.hidden || state.phase === 'finished' ? 3000 : 900);
    }
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

    // ------------------------------------------------------------------ rendering

    function render() {
        const known = new Set(state.hand || []);
        selected = new Set([...selected].filter(id => known.has(id)));
        renderInfo();
        for (let seat = 0; seat < 3; seat++) {
            renderProfile(seat);
            renderPlay(seat);
            if (posOf(seat) !== 'bottom') renderOpponentHand(seat);
        }
        renderHand();
        renderActions();
        renderLog();
        renderCounter();
        renderFairness();
        renderResult();
        renderMatch();
        document.title = myTurn() ? `【轮到你】${baseTitle}` : baseTitle;
        tick();
    }

    function renderInfo() {
        const bottom = $('ddz-bottom-cards');
        bottom.replaceChildren();
        // Before a landlord is chosen, super users with 显示他人手牌 on peek at the bottom cards.
        const peek = !state.bottom && showAll && !replay.active && state.secret_bottom;
        const cards = state.bottom || (peek ? state.secret_bottom : null);
        for (let i = 0; i < 3; i++) bottom.append(cards ? cardEl(cards[i], 'ddz-small' + (peek ? ' ddz-peek' : '')) : backEl('ddz-small'));
        bottom.title = peek ? '底牌（仅管理员可见）' : '';
        const info = $('ddz-info');
        info.replaceChildren();
        const item = (label, value) => {
            const span = el('span', 'ddz-info-item');
            span.append(el('small', '', label), el('strong', '', value));
            info.append(span);
        };
        if (state.match) item('记分比赛', `第 ${state.match.round}/${state.match.rounds} 局`);
        item('底分', state.bid ? `${state.bid} 分` : '叫分中');
        item('倍数', `×${state.multiplier}`);
        item('炸弹', String(state.bombs));
        if (state.redeals) info.append(el('span', 'ddz-info-note', `已流局 ${state.redeals} 次`));
    }

    function profileOf(seat) {
        return root.querySelector(`.ddz-profile[data-seat="${seat}"]`);
    }

    function renderProfile(seat) {
        const box = profileOf(seat), p = state.players[seat];
        box.classList.toggle('ddz-turn', state.turn === seat);
        box.classList.toggle('ddz-landlord', p.role === 'landlord');
        const role = box.querySelector('.ddz-role');
        role.hidden = !p.role;
        role.textContent = p.role === 'landlord' ? '地主' : '农民';
        role.className = 'ddz-role ' + (p.role === 'landlord' ? 'ddz-role-landlord' : 'ddz-role-farmer');
        const count = box.querySelector('.ddz-count');
        count.textContent = `${p.count} 张`;
        count.classList.toggle('ddz-warn', state.phase === 'playing' && p.count > 0 && p.count <= 2);
        box.querySelector('.ddz-auto').hidden = !p.auto;
        const bubble = box.querySelector('.ddz-bubble');
        bubble.hidden = p.bid === null;
        bubble.textContent = p.bid === 0 ? '不叫' : `${p.bid} 分`;
        const reserve = box.querySelector('.ddz-reserve');
        reserve.hidden = state.phase !== 'playing' || replay.active; // the log does not record clocks
        reserve.textContent = `局时 ${Math.ceil(state.reserve[seat] / 1000)}s`;
        reserve.classList.toggle('ddz-reserve-empty', state.reserve[seat] <= 0);
        bubble.classList.toggle('ddz-bubble-pass', p.bid === 0);
    }

    function renderPlay(seat) {
        const area = root.querySelector(`.ddz-play[data-seat="${seat}"]`);
        area.replaceChildren();
        const entry = state.phase === 'playing' || state.phase === 'finished' ? state.table[seat] : null;
        if (entry === 'pass') {
            area.append(el('div', 'ddz-pass', '不出'));
        } else if (entry) {
            const row = cardRow(entry.cards, 'ddz-played');
            area.append(row, el('div', 'ddz-pattern', R.describe(entry.pattern)));
        }
        area.classList.toggle('ddz-latest', !!(state.last && state.last.seat === seat && entry && entry !== 'pass'));
    }

    function renderOpponentHand(seat) {
        const area = root.querySelector(`.ddz-opp-hand[data-seat="${seat}"]`);
        area.replaceChildren();
        if (showHands()) {
            // Rows of ten keep a revealed hand inside the narrow seat column.
            const cards = ordered(state.hands[seat]);
            for (let i = 0; i < cards.length; i += 10) {
                const row = el('div', 'ddz-cards ddz-mini');
                cards.slice(i, i + 10).forEach(id => row.append(cardEl(id)));
                area.append(row);
            }
        } else if (state.players[seat].count) {
            const stack = el('div', 'ddz-stack');
            stack.append(backEl('ddz-small'), el('span', 'ddz-stack-count', String(state.players[seat].count)));
            area.append(stack);
        }
    }

    function interactive() {
        return isPlayer() && state.phase !== 'finished' && !replay.active;
    }

    function renderHand() {
        const hand = $('ddz-hand');
        hand.replaceChildren();
        let cards = state.hand;
        if (!cards && showHands()) cards = state.hands[bottomSeat];
        hand.classList.toggle('ddz-readonly', !interactive());
        if (cards) {
            ordered(cards).forEach(id => {
                const c = cardEl(id);
                if (selected.has(id)) c.classList.add('ddz-selected');
                hand.append(c);
            });
        }
        layoutHand();
    }

    function layoutHand() {
        const hand = $('ddz-hand'), cards = hand.children;
        if (!cards.length) return;
        const width = hand.clientWidth, cw = cards[0].offsetWidth;
        const step = cards.length > 1 ? Math.min(cw * 0.5, (width - cw) / (cards.length - 1)) : 0;
        for (let i = 0; i < cards.length; i++) cards[i].style.marginLeft = i ? `${step - cw}px` : '0';
    }
    window.addEventListener('resize', layoutHand);

    function button(label, className, onClick, disabled) {
        const b = el('button', 'btn ' + className, label);
        b.type = 'button';
        b.disabled = !!disabled || busy;
        b.addEventListener('click', onClick);
        return b;
    }

    // Buttons that only navigate or show/hide panels. Unlike game actions they must not be
    // disabled while a request is in flight: the result popup is built once, often during the
    // very request whose move ended the game, and would otherwise stay disabled for good.
    function navButton(label, className, onClick) {
        const b = el('button', 'btn ' + className, label);
        b.type = 'button';
        b.addEventListener('click', onClick);
        return b;
    }

    function selectedPattern() {
        const cards = [...selected];
        if (!cards.length) return {options: [], cards};
        return {options: R.playable(cards, toBeat()), cards, readings: R.classify(cards)};
    }

    function renderActions() {
        const bar = $('ddz-actions');
        if (replay.active || state.phase === 'finished') {
            // The replay bar is built once per mode and then updated in place, so the slider
            // keeps working while it is being dragged.
            const mode = replay.active ? 'replay' : 'final';
            if (replayBar.el && replayBar.el.parentNode === bar && replayBar.mode === mode) {
                replayBar.update();
                return;
            }
            bar.replaceChildren();
            if (canReplay()) {
                bar.append(replayBar.build());
                replayBar.mode = mode;
            }
            if (!replay.active) bar.append(...nextButtons('ddz-btn'), navButton('查看结果', 'btn-light ddz-btn', () => { $('ddz-result').hidden = false; }));
            return;
        }
        bar.replaceChildren();
        const main = el('div', 'ddz-action-main');
        if (myTurn() && state.phase === 'bidding') {
            main.append(button('不叫', 'btn-light ddz-btn', () => act('bid', {value: '0'}), state.must_bid));
            for (let v = 1; v <= 3; v++) {
                main.append(button(`${v} 分`, 'btn-warning ddz-btn', () => act('bid', {value: String(v)}), v <= state.bid));
            }
            if (state.must_bid) main.append(el('span', 'ddz-hint-text', '已连续流局，本轮必须叫分'));
        } else if (myTurn() && state.phase === 'playing') {
            const sel = selectedPattern();
            main.append(button('不出', 'btn-light ddz-btn', () => { selected.clear(); act('pass'); }, state.leading));
            main.append(button('提示', 'btn-info ddz-btn', showHint));
            if (sel.options.length > 1) {
                const select = el('select', 'custom-select custom-select-sm ddz-choice');
                sel.options.forEach(p => {
                    const o = el('option', '', R.describe(p));
                    o.value = R.patternKey(p);
                    select.append(o);
                });
                if (!sel.options.some(p => R.patternKey(p) === choice)) choice = R.patternKey(sel.options[0]);
                select.value = choice;
                select.title = '这手牌有多种理解，选择按哪种牌型打出';
                select.addEventListener('change', () => { choice = select.value; });
                main.append(select);
            }
            main.append(button('出牌', 'btn-warning ddz-btn ddz-btn-play', playSelected, !sel.options.length));
            let label = '';
            if (sel.cards.length) {
                label = sel.options.length ? R.describe(sel.options.find(p => R.patternKey(p) === choice) || sel.options[0])
                    : sel.readings.length ? '管不上' : '不是合法牌型';
            } else if (!state.leading && !R.hints(state.hand, toBeat()).length) {
                label = '没有牌大过上家';
            }
            if (label) main.append(el('span', 'ddz-hint-text' + (sel.cards.length && !sel.options.length ? ' ddz-bad' : ''), label));
        } else if (state.turn !== null) {
            const who = state.turn === state.seat ? '你' : nameOf(state.turn);
            main.append(el('span', 'ddz-wait', state.phase === 'bidding' ? `等待 ${who} 叫分…` : `等待 ${who} 出牌…`));
        }
        bar.append(main);
        const side = el('div', 'ddz-action-side');
        if (isAdmin && state.hands) {
            side.append(button(showAll ? '隐藏他人手牌' : '显示他人手牌', 'btn-sm ddz-btn-superuser' + (showAll ? ' active' : ''), () => {
                showAll = !showAll;
                render();
            }));
        }
        if (isPlayer()) {
            const chat = button('💬', 'btn-outline-light btn-sm', toggleChatPop);
            chat.title = '发言';
            side.append(chat);
            const auto = state.players[state.seat].auto;
            side.append(button(auto ? '取消托管' : '托管', auto ? 'btn-danger btn-sm' : 'btn-outline-light btn-sm',
                () => act('auto', {on: auto ? '0' : '1'})));
            side.append(button(sortDesc ? '从大到小' : '从小到大', 'btn-outline-light btn-sm', () => {
                sortDesc = !sortDesc;
                store.set('ddz-sort', sortDesc ? 'desc' : 'asc');
                render();
            }));
        }
        bar.append(side);
    }

    function showHint() {
        if (!hints) hints = R.hints(state.hand, toBeat());
        if (!hints.length) {
            toast('没有能大过上家的牌，只能不出。');
            return;
        }
        hintIndex = (hintIndex + 1) % hints.length;
        selected = new Set(hints[hintIndex]);
        renderHand();
        renderActions();
    }

    function playSelected() {
        const sel = selectedPattern();
        if (!sel.options.length) return;
        const p = sel.options.find(o => R.patternKey(o) === choice) || sel.options[0];
        act('play', {cards: R.sortCards(sel.cards).join(','), choice: R.patternKey(p)});
    }

    // ------------------------------------------------------------------ card selection

    const handEl = $('ddz-hand');
    const handCards = () => [...handEl.children];
    // Overlapping cards: the one under x is the last whose left edge is at or before it.
    function indexAt(x) {
        const cards = handCards();
        let index = 0;
        cards.forEach((c, i) => { if (c.getBoundingClientRect().left <= x) index = i; });
        return index;
    }
    function markSweep() {
        const [a, b] = drag ? [Math.min(drag.start, drag.cur), Math.max(drag.start, drag.cur)] : [1, 0];
        handCards().forEach((c, i) => c.classList.toggle('ddz-sweep', i >= a && i <= b));
    }
    handEl.addEventListener('pointerdown', e => {
        const card = e.target.closest('.ddz-card');
        if (!card || !interactive() || e.button !== 0) return;
        e.preventDefault();
        const start = handCards().indexOf(card);
        drag = {start, cur: start, removing: selected.has(Number(card.dataset.card))};
        handEl.setPointerCapture(e.pointerId);
        markSweep();
    });
    handEl.addEventListener('pointermove', e => {
        if (!drag) return;
        const cur = indexAt(e.clientX);
        if (cur !== drag.cur) {
            drag.cur = cur;
            markSweep();
        }
    });
    function endDrag(apply) {
        if (!drag) return;
        const [a, b] = [Math.min(drag.start, drag.cur), Math.max(drag.start, drag.cur)];
        const swept = handCards().slice(a, b + 1).map(c => Number(c.dataset.card));
        const removing = drag.removing;
        drag = null;
        markSweep();
        if (!apply) return;
        if (swept.length === 1) {
            if (selected.has(swept[0])) selected.delete(swept[0]); else selected.add(swept[0]);
        } else if (removing) {
            swept.forEach(id => selected.delete(id));
        } else {
            swept.forEach(id => selected.delete(id));
            const follow = state.phase === 'playing' && state.last && state.last.seat !== state.seat ? state.last.pattern : null;
            R.smartSelect(swept, follow).forEach(id => selected.add(id));
        }
        hintIndex = -1;
        renderHand();
        renderActions();
    }
    handEl.addEventListener('pointerup', () => endDrag(true));
    handEl.addEventListener('pointercancel', () => endDrag(false));
    const table = root.querySelector('.ddz-table');
    table.addEventListener('contextmenu', e => {
        if (!interactive()) return;
        e.preventDefault();
        selected.clear();
        renderHand();
        renderActions();
    });
    table.addEventListener('dblclick', e => {
        if (!interactive() || e.target.closest('.ddz-card, button, select, a')) return;
        selected.clear();
        renderHand();
        renderActions();
    });

    // ------------------------------------------------------------------ side panels

    function clockText(ms) {
        const s = Math.max(0, Math.round(ms / 1000));
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }

    function renderLog() {
        const box = $('ddz-log');
        const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
        box.replaceChildren();
        const start = state.started;
        let round = 1;
        state.log.forEach(entry => {
            const row = el('div', 'ddz-log-row');
            row.append(el('span', 'ddz-log-time', clockText(entry.t - start)));
            if (entry.kind === 'redeal') {
                row.append(el('span', 'ddz-log-text text-muted', `无人叫分，第 ${++round} 次重新洗牌发牌`));
                box.append(row);
                return;
            }
            const who = el('span', 'ddz-log-who' + (state.landlord === entry.seat ? ' ddz-log-landlord' : ''), nameOf(entry.seat));
            row.append(who);
            const text = el('span', 'ddz-log-text');
            if (entry.kind === 'bid') text.textContent = entry.value ? `叫 ${entry.value} 分` : '不叫';
            else if (entry.kind === 'landlord') {
                text.textContent = `成为地主（${entry.value} 分），底牌 `;
                text.append(cardRow(entry.cards, 'ddz-log-cards'));
            } else if (entry.kind === 'play') {
                text.append(cardRow(entry.cards, 'ddz-log-cards'), el('span', 'ddz-log-pattern', R.describe(entry.pattern)));
            } else if (entry.kind === 'pass') text.textContent = '不出';
            else if (entry.kind === 'auto_on') text.textContent = entry.timeout ? '超时，进入托管' : '开启托管';
            else if (entry.kind === 'auto_off') text.textContent = '取消托管';
            else if (entry.kind === 'finish') text.textContent = '出完所有牌';
            if (entry.auto) text.append(el('span', 'badge badge-warning ml-1', '托管'));
            row.append(text);
            box.append(row);
        });
        $('ddz-log-count').textContent = `${state.log.filter(e => e.kind === 'play').length} 手`;
        if (atBottom) box.scrollTop = box.scrollHeight;
    }

    function renderCounter() {
        const left = [...new Array(13).fill(4), 1, 1];
        state.log.forEach(e => { if (e.kind === 'play') e.cards.forEach(c => left[R.rank(c)]--); });
        if (state.hand) state.hand.forEach(c => left[R.rank(c)]--);
        const box = $('ddz-counter');
        box.replaceChildren();
        for (let r = 14; r >= 0; r--) {
            // All four of a rank still unseen: someone else may hold them as a bomb.
            const bomb = r < 13 && left[r] === 4;
            const cell = el('div', 'ddz-counter-cell' + (left[r] === 0 ? ' ddz-counter-zero' : '') + (bomb ? ' ddz-counter-bomb' : ''));
            if (bomb) cell.title = '4 张都未出现，可能有炸弹';
            cell.append(el('div', 'ddz-counter-rank', R.RANK_NAMES[r]), el('div', 'ddz-counter-num', String(left[r])));
            box.append(cell);
        }
    }

    async function sha256(text) {
        if (!window.crypto || !crypto.subtle) return null;
        const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
        return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
    }

    let fairnessKey = null;
    function renderFairness() {
        const key = state.fairness.commitments.join() + (state.fairness.deals ? ':done' : '');
        if (key === fairnessKey) return;
        fairnessKey = key;
        const box = $('ddz-fairness');
        box.replaceChildren();
        box.append(el('p', 'mb-2', '服务器用密码学安全随机数（CSPRNG）做 Fisher–Yates 洗牌，54! 种牌序等概率出现；座位和首个叫分者同样随机。'
            + '发牌前先公布牌序的 SHA-256 承诺，终局后公开完整牌序和随机盐，可自行复核发牌未被改动。'));
        const list = el('ol', 'pl-3 mb-0 ddz-commitments');
        state.fairness.commitments.forEach((c, i) => {
            const li = el('li');
            li.append(el('code', '', c.slice(0, 16) + '…'));
            li.title = c;
            const deal = state.fairness.deals && state.fairness.deals[i];
            if (deal) {
                const status = el('span', 'ml-1 text-muted', '校验中…');
                li.append(status);
                sha256(deal.deck.join(',') + '|' + deal.salt).then(h => {
                    if (h === null) status.textContent = '（浏览器不支持校验）';
                    else if (h === deal.commitment) { status.textContent = '✓ 牌序与承诺一致'; status.className = 'ml-1 text-success'; }
                    else { status.textContent = '✗ 不一致'; status.className = 'ml-1 text-danger'; }
                });
                const details = el('details', 'mt-1');
                details.append(el('summary', '', '查看牌序'));
                details.append(el('div', 'text-monospace text-break', `deck = ${deal.deck.join(',')}`),
                    el('div', 'text-monospace text-break', `salt = ${deal.salt}`),
                    el('div', 'text-muted', `sha256(deck.join(",") + "|" + salt)；座位 i 依次得到第 17i 到 17i+16 张，最后 3 张为底牌。`));
                li.append(details);
            }
            list.append(li);
        });
        box.append(list);
    }

    function renderResult() {
        const box = $('ddz-result');
        if (replay.active) { box.hidden = true; return; }
        if (state.phase !== 'finished') { box.hidden = true; return; }
        if (resultShown) return;
        resultShown = true;
        const r = state.result;
        box.replaceChildren();
        const panel = el('div', 'ddz-result-panel');
        const mineWon = isPlayer() ? r.deltas[state.seat] > 0 : null;
        panel.append(el('div', 'ddz-result-title' + (mineWon === false ? ' ddz-lose' : ''),
            mineWon === null ? (r.landlord_won ? '地主获胜' : '农民获胜') : mineWon ? '你赢了！' : '你输了'));
        const tags = [r.landlord_won ? '地主胜' : '农民胜'];
        if (r.spring) tags.push('春天 +1 倍');
        if (r.anti_spring) tags.push('反春 +1 倍');
        panel.append(el('div', 'ddz-result-sub', `${tags.join(' · ')} · 底分 ${r.base} × 倍数 ${r.multiplier}`));
        const tbl = el('table', 'table table-sm mb-3');
        const tb = el('tbody');
        state.players.forEach((p, seat) => {
            const tr = el('tr');
            const name = el('td');
            name.append(profileOf(seat).querySelector('.ddz-name').cloneNode(true));
            tr.append(name, el('td', '', p.role === 'landlord' ? '地主' : '农民'),
                el('td', 'text-right ' + (r.deltas[seat] > 0 ? 'text-success' : 'text-danger'), (r.deltas[seat] > 0 ? '+' : '') + r.deltas[seat]));
            tb.append(tr);
        });
        tbl.append(tb);
        panel.append(tbl);
        const last = state.fairness.deals && state.fairness.deals[state.fairness.deals.length - 1];
        if (last) {
            const init = el('details', 'mb-3 ddz-initial-hands');
            init.append(el('summary', '', '查看开局手牌'));
            for (let seat = 0; seat < 3; seat++) {
                const row = el('div', 'd-flex align-items-center mb-1');
                row.append(el('span', 'ddz-initial-name', nameOf(seat)), cardRow(last.deck.slice(17 * seat, 17 * seat + 17), 'ddz-log-cards'));
                init.append(row);
            }
            const row = el('div', 'd-flex align-items-center');
            row.append(el('span', 'ddz-initial-name', '底牌'), cardRow(last.deck.slice(51), 'ddz-log-cards'));
            init.append(row);
            panel.append(init);
        }
        if (state.match) panel.append(matchTable());
        const actions = el('div', 'd-flex justify-content-center');
        actions.append(...nextButtons('mr-2'), navButton('查看牌桌', 'btn-light', () => { box.hidden = true; }));
        panel.append(actions);
        // Players of a match move on to the next game by themselves after a short pause, unless
        // they touch the popup or the table (for instance 查看牌桌 to look over the hand): then
        // they stay, and the 「第 N 局 →」 buttons take them on when they are ready.
        if (state.match && state.match.next_game && isPlayer() && !openedFinished) {
            const note = el('p', 'text-center text-muted small mt-2 mb-0');
            panel.append(note);
            let left = 5, timer = null;
            const next = state.match.next_game, round = state.match.round + 1;
            const cancel = () => {
                clearTimeout(timer);
                note.textContent = `第 ${round} 局已经开始，准备好后点「第 ${round} 局 →」进入。`;
                table.removeEventListener('pointerdown', cancel, true);
            };
            const step = () => {
                note.textContent = `${left} 秒后自动进入第 ${round} 局（点击任意处取消）`;
                if (left-- <= 0) location.href = `/games/doudizhu/game/${next}`;
                else timer = setTimeout(step, 1000);
            };
            table.addEventListener('pointerdown', cancel, true);
            step();
        }
        box.append(panel);
        box.hidden = openedFinished;
    }

    // After a game: next game of the match, the match result, or a new single game.
    function nextButtons(extra) {
        const go = url => () => { location.href = url; };
        const m = state.match;
        if (!m) return [navButton('再来一局', 'btn-warning ' + extra, go('/games/doudizhu?queue=1'))];
        if (m.next_game) return [navButton(`第 ${m.round + 1} 局 →`, 'btn-warning ' + extra, go(`/games/doudizhu/game/${m.next_game}`))];
        if (m.finished) return [navButton('再来一场比赛', 'btn-warning ' + extra, go('/games/doudizhu?queue=match')),
            navButton('比赛结果', 'btn-light ' + extra, go(`/games/doudizhu/match/${m.id}`))];
        return [];
    }

    // Per-game scores of the match so far, totals, and rating changes once settled.
    function matchTable() {
        const m = state.match;
        const table = el('table', 'table table-sm ddz-match-table mb-3');
        const head = el('tr');
        head.append(el('th', '', '玩家 \\ 局'));
        for (let r = 0; r < m.rounds; r++) {
            const th = el('th', 'text-right');
            th.title = `第 ${r + 1} 局`;
            if (m.games[r]) {
                const a = el('a', r + 1 === m.round ? 'font-weight-bold' : '', String(r + 1));
                a.href = `/games/doudizhu/game/${m.games[r]}`;
                th.append(a);
            } else th.textContent = String(r + 1);
            head.append(th);
        }
        head.append(el('th', 'text-right', '总分'));
        if (m.rating_after) head.append(el('th', 'text-right', 'Rating'));
        const thead = el('thead');
        thead.append(head);
        const tbody = el('tbody');
        m.players.forEach((name, seat) => {
            const tr = el('tr');
            tr.append(el('td', '', (m.places ? `#${m.places[seat]} ` : '') + name));
            for (let r = 0; r < m.rounds; r++) {
                const d = m.deltas[r] ? m.deltas[r][seat] : null;
                tr.append(el('td', 'text-right ' + (d > 0 ? 'text-success' : d < 0 ? 'text-danger' : 'text-muted'), d === null ? '—' : (d > 0 ? '+' : '') + d));
            }
            tr.append(el('td', 'text-right font-weight-bold', String(m.totals[seat])));
            if (m.rating_after) {
                const diff = m.rating_after[seat] - m.rating_before[seat];
                tr.append(el('td', 'text-right', `${m.rating_after[seat]} (${diff >= 0 ? '+' : ''}${diff})`));
            }
            tbody.append(tr);
        });
        table.append(thead, tbody);
        const wrap = el('div', 'table-responsive');
        wrap.append(table);
        return wrap;
    }

    function renderMatch() {
        const box = $('ddz-match');
        if (!box) return;
        box.hidden = !state.match;
        if (!state.match) return;
        const body = box.querySelector('.card-body');
        body.replaceChildren(matchTable());
        const link = el('a', 'small', '比赛详情 →');
        link.href = `/games/doudizhu/match/${state.match.id}`;
        body.append(link);
    }

    // ------------------------------------------------------------------ chat

    let lastChatId = 0;
    let muted = store.get('ddz-chat-mute') === '1';
    const hiddenForMe = m => muted && m.seat !== state.seat;

    function chatRow(m) {
        const row = el('div', 'ddz-chat-row' + (m.seat === state.seat ? ' ddz-chat-mine' : ''));
        row.dataset.seat = m.seat;
        row.hidden = hiddenForMe(m);
        row.append(el('span', 'ddz-chat-time', clockText(m.t - state.started)),
            el('span', 'ddz-chat-who' + (state.landlord === m.seat ? ' ddz-log-landlord' : ''), m.username),
            el('span', 'ddz-chat-text', m.text));
        return row;
    }

    // Speech bubble beside the speaker's avatar for a few seconds.
    function say(m) {
        const bubble = profileOf(m.seat).querySelector('.ddz-say');
        bubble.textContent = m.text;
        bubble.hidden = false;
        clearTimeout(bubble.timer);
        bubble.timer = setTimeout(() => { bubble.hidden = true; }, 4500);
    }

    const allChat = [];
    function receiveChat(messages, live) {
        const fresh = messages.filter(m => m.id > lastChatId);
        if (fresh.length) lastChatId = fresh[fresh.length - 1].id;
        allChat.push(...fresh);
        // During a replay the chat panel shows the replayed conversation; it catches up on exit.
        if (replay.active) return;
        const list = $('ddz-chat-list');
        const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 30;
        for (const m of fresh) {
            list.append(chatRow(m));
            if (live && !hiddenForMe(m)) say(m);
        }
        chatListDone(list, atBottom || live);
    }

    function chatListDone(list, scroll) {
        if (!list.querySelector('.ddz-chat-row')) list.replaceChildren(el('div', 'text-muted small ddz-chat-empty', '还没有人发言。'));
        else list.querySelector('.ddz-chat-empty')?.remove();
        if (scroll) list.scrollTop = list.scrollHeight;
    }

    function showChat(messages) {
        const list = $('ddz-chat-list');
        list.replaceChildren(...messages.map(chatRow));
        chatListDone(list, true);
    }

    // ------------------------------------------------------------------ replay

    // Everything a replay needs is in a finished game's state: each deal's revealed deck (the
    // starting hands), the move log with times, and the chat.
    function canReplay() {
        const f = replay.active ? replay.final : state;
        return f.phase === 'finished' && !!f.fairness.deals && f.fairness.deals.length > 0;
    }

    function replaySteps(f) {
        const steps = f.log.map(entry => ({t: entry.t, entry}));
        allChat.filter(m => m.t <= (f.result ? f.result.finished : Infinity) + 60000)
            .forEach(msg => steps.push({t: msg.t, msg}));
        // Stable sort: on equal times moves come before chat, as they were logged first.
        return steps.sort((a, b) => a.t - b.t);
    }

    const dealHands = deck => [0, 1, 2].map(i => R.sortCards(deck.slice(17 * i, 17 * i + 17)));

    // The table after the first k steps, shaped like a server state so the renderers draw it.
    function snapshot(k) {
        const f = replay.final, deals = f.fairness.deals;
        let deal = 0, hands = dealHands(deals[0].deck), bids = [null, null, null], auto = [false, false, false];
        const S = {...f, phase: 'bidding', turn: deals[0].first_bidder, remaining: null, reserve_in_remaining: 0,
            bid: 0, landlord: null, must_bid: false, bottom: null, hand: null, last: null, table: [null, null, null],
            multiplier: 1, bombs: 0, log: [], result: null, redeals: 0};
        for (const step of replay.steps.slice(0, k)) {
            const e = step.entry;
            if (!e) continue;
            S.log.push(e);
            if (e.kind === 'bid') {
                bids[e.seat] = e.value;
                if (e.value > S.bid) S.bid = e.value;
                S.turn = (e.seat + 1) % 3;
            } else if (e.kind === 'redeal') {
                deal = Math.min(deal + 1, deals.length - 1);
                hands = dealHands(deals[deal].deck);
                bids = [null, null, null];
                S.bid = 0;
                S.redeals++;
                S.turn = deals[deal].first_bidder;
            } else if (e.kind === 'landlord') {
                S.landlord = e.seat;
                S.bid = e.value;
                S.bottom = e.cards;
                hands[e.seat] = R.sortCards(hands[e.seat].concat(e.cards));
                S.phase = 'playing';
                S.turn = e.seat;
                S.redeals = 0;
            } else if (e.kind === 'play') {
                const played = new Set(e.cards);
                hands[e.seat] = hands[e.seat].filter(c => !played.has(c));
                if (!S.last || S.last.seat === e.seat) S.table = [null, null, null];
                S.table = S.table.slice();
                S.table[e.seat] = {cards: e.cards, pattern: e.pattern};
                S.last = {seat: e.seat, pattern: e.pattern};
                if (e.pattern.type === 'bomb' || e.pattern.type === 'rocket') { S.bombs++; S.multiplier++; }
                S.turn = (e.seat + 1) % 3;
            } else if (e.kind === 'pass') {
                S.table = S.table.slice();
                S.table[e.seat] = 'pass';
                S.turn = (e.seat + 1) % 3;
            } else if (e.kind === 'auto_on' || e.kind === 'auto_off') {
                auto[e.seat] = e.kind === 'auto_on';
            } else if (e.kind === 'finish') {
                S.phase = 'finished';
                S.turn = null;
                S.result = f.result;
                S.multiplier = f.result.multiplier;
            }
        }
        S.hands = hands;
        S.leading = S.phase === 'playing' && (!S.last || S.last.seat === S.turn);
        S.players = f.players.map((p, i) => ({...p, count: hands[i].length, auto: auto[i],
            bid: S.phase === 'bidding' ? bids[i] : null,
            role: S.landlord === null ? null : S.landlord === i ? 'landlord' : 'farmer'}));
        return S;
    }

    function showStep(k, animate) {
        replay.index = k;
        state = snapshot(k);
        const messages = replay.steps.slice(0, k).filter(st => st.msg).map(st => st.msg);
        showChat(messages.filter(m => !hiddenForMe(m)));
        const step = replay.steps[k - 1];
        // Bubbles belong to the moment they were said: a jump clears them.
        if (!animate) root.querySelectorAll('.ddz-say').forEach(b => { b.hidden = true; });
        if (animate && step && step.msg && !hiddenForMe(step.msg)) say(step.msg);
        render();
    }

    function startReplay(from) {
        if (!replay.active) {
            replay.final = state;
            replay.steps = replaySteps(state);
            replay.active = true;
        }
        showStep(from === undefined ? 0 : from, false);
    }

    function stopReplay() {
        clearTimeout(replay.timer);
        replay.playing = false;
        replay.active = false;
        state = replay.final;
        showChat(allChat);
        root.querySelectorAll('.ddz-say').forEach(b => { b.hidden = true; });
        render();
    }

    // Real gaps between steps, clamped so long thinks don't drag and instant 托管 moves stay visible.
    function stepDelay(k) {
        const prev = replay.steps[k - 1], cur = replay.steps[k];
        const gap = prev ? cur.t - prev.t : 0;
        const [lo, hi] = cur.msg ? [300, 2000] : [600, 2500];
        return Math.min(Math.max(gap, lo), hi) / replay.speed;
    }

    function scheduleReplay() {
        clearTimeout(replay.timer);
        if (!replay.playing) return;
        if (replay.index >= replay.steps.length) {
            replay.playing = false;
            replay.timer = setTimeout(stopReplay, 1500 / replay.speed);
            return;
        }
        replay.timer = setTimeout(() => {
            showStep(replay.index + 1, true);
            scheduleReplay();
        }, stepDelay(replay.index));
    }

    function playPause() {
        if (replay.playing) {
            replay.playing = false;
            clearTimeout(replay.timer);
            renderActions();
            return;
        }
        if (!replay.active || replay.index >= replay.steps.length) startReplay(0);
        replay.playing = true;
        renderActions();
        scheduleReplay();
    }

    function seek(k) {
        clearTimeout(replay.timer);
        replay.playing = false;
        if (!replay.active) startReplay(k);
        else showStep(Math.max(0, Math.min(k, replay.steps.length)), false);
    }

    const replayBar = {
        el: null, mode: null,
        stepCount: () => replay.active ? replay.steps.length : replaySteps(state).length,
        build() {
            const bar = el('div', 'ddz-replay');
            const ctl = (label, title, onClick) => {
                const b = navButton(label, 'btn-sm btn-outline-light ddz-replay-btn', onClick);
                b.title = title;
                return b;
            };
            const at = () => replay.active ? replay.index : this.stepCount();
            this.play = ctl('▶', '回放整局', playPause);
            bar.append(ctl('⏮', '从头开始', () => seek(0)), ctl('◀', '上一步', () => seek(at() - 1)),
                this.play, ctl('▶|', '下一步', () => seek(at() + 1)));
            this.slider = el('input', 'custom-range ddz-replay-slider');
            this.slider.type = 'range';
            this.slider.min = '0';
            this.slider.setAttribute('aria-label', '回放进度');
            this.slider.addEventListener('input', () => seek(Number(this.slider.value)));
            this.pos = el('span', 'ddz-replay-pos');
            this.speeds = el('div', 'btn-group btn-group-sm ddz-replay-speeds');
            for (const v of [0.5, 1, 2, 4]) {
                const b = navButton(`${v}x`, '', () => {
                    replay.speed = v;
                    store.set('ddz-replay-speed', String(v));
                    this.update();
                    if (replay.playing) scheduleReplay();
                });
                b.dataset.speed = v;
                this.speeds.append(b);
            }
            this.exit = ctl('退出回放', '回到终局', stopReplay);
            bar.append(this.slider, this.pos, this.speeds, this.exit);
            this.el = bar;
            this.update();
            return bar;
        },
        update() {
            const steps = this.stepCount(), index = replay.active ? replay.index : steps;
            this.play.textContent = replay.playing ? '⏸' : '▶';
            this.play.title = replay.playing ? '暂停' : replay.active ? '继续回放' : '回放整局';
            this.slider.max = String(steps);
            this.slider.value = String(index);
            this.pos.textContent = replay.active ? `${index} / ${steps}` : '回放';
            this.speeds.querySelectorAll('button').forEach(b => {
                b.className = 'btn ' + (Number(b.dataset.speed) === replay.speed ? 'btn-warning' : 'btn-outline-light');
            });
            this.exit.hidden = !replay.active;
        },
    };

    function renderMuteButton() {
        const b = $('ddz-chat-mute');
        b.textContent = muted ? '取消屏蔽' : '屏蔽他人发言';
        b.title = muted ? '重新显示其他玩家的发言' : '不再显示其他玩家的发言（只影响你自己）';
    }
    $('ddz-chat-mute').addEventListener('click', () => {
        muted = !muted;
        store.set('ddz-chat-mute', muted ? '1' : '0');
        renderMuteButton();
        root.querySelectorAll('.ddz-chat-row').forEach(row => {
            row.hidden = muted && Number(row.dataset.seat) !== state.seat;
        });
        if (muted) root.querySelectorAll('.ddz-say').forEach(b => { b.hidden = true; });
    });

    // Compact chat panel on the table, so players can talk without scrolling away from the game.
    function toggleChatPop(open) {
        const pop = $('ddz-chat-pop');
        if (!pop) return;
        pop.hidden = typeof open === 'boolean' ? !open : !pop.hidden;
        if (!pop.hidden) $('ddz-chat-pop-input').focus({preventScroll: true});
    }

    let sending = false;
    async function sendChat(text, input) {
        text = text.trim();
        if (!text || sending) return;
        sending = true;
        try {
            await api('chat', {text});
            if (input) input.value = '';
            toggleChatPop(false);
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            sending = false;
        }
    }
    [['ddz-chat-form', 'ddz-chat-input'], ['ddz-chat-pop', 'ddz-chat-pop-input']].forEach(([formId, inputId]) => {
        const form = $(formId);
        if (!form) return;
        form.addEventListener('submit', e => {
            e.preventDefault();
            sendChat($(inputId).value, $(inputId));
        });
        form.querySelectorAll('[data-phrase]').forEach(b => b.addEventListener('click', () => sendChat(b.dataset.phrase)));
    });
    if ($('ddz-chat-pop')) {
        $('ddz-chat-pop').addEventListener('keydown', e => { if (e.key === 'Escape') toggleChatPop(false); });
        // Right-click and double-click on the table clear the card selection, not the chat panel.
        $('ddz-chat-pop').addEventListener('contextmenu', e => e.stopPropagation());
        $('ddz-chat-pop').addEventListener('dblclick', e => e.stopPropagation());
    }

    function tick() {
        for (let seat = 0; seat < 3; seat++) {
            const clock = profileOf(seat).querySelector('.ddz-clock');
            const on = state.turn === seat && deadlineAt > 0;
            clock.hidden = !on;
            if (on) {
                // The deadline includes the player's reserve (局时): count down the move time first, then the reserve.
                const left = deadlineAt - performance.now(), reserve = state.reserve_in_remaining;
                const onReserve = reserve > 0 && left <= reserve;
                const s = Math.max(0, Math.ceil((onReserve ? left : left - reserve) / 1000));
                clock.textContent = onReserve ? `局${s}` : String(s);
                clock.title = onReserve ? '正在使用局时' : '本步剩余时间';
                const urgent = s <= 5 && (onReserve || !reserve);
                clock.classList.toggle('ddz-clock-reserve', onReserve && !urgent);
                clock.classList.toggle('ddz-clock-urgent', urgent);
            }
        }
    }
    setInterval(tick, 250);

    deadlineAt = state.remaining === null ? 0 : performance.now() + state.remaining;
    render();
    // Every viewer loads the whole deck up front, so later revealing cards (a super user's
    // 显示他人手牌, the bottom cards) never fetches anything that could show what was looked at,
    // and every viewer's page makes the same requests.
    const deckImages = [];
    for (let id = 0; id <= 53; id++) {
        const img = new Image();
        img.src = `/images/games/doudizhu/${id}.webp`;
        deckImages.push(img);
    }
    receiveChat(initial.chat, false);
    renderMuteButton();
    if (shouldPoll()) pollTimer = setTimeout(poll, 900);
})();
