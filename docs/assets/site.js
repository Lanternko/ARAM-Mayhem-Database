(async () => {
// GitHub Pages deep-link bootstrap: lightweight path shells (and 404.html)
    // stash the intended URL then bounce to /.  Restore before any route read
    // so shareable paths like /column/sprees-not-snowball open the right view.
    // Also restore locale: stubs set aram-spa-lang so /en… survives the bounce
    // even if path restore is delayed or partial.
    let pendingBootLang = null;  // 'en' | 'zh' | 'zh-CN' | null — applied before first paint work
    try {
        const pending = sessionStorage.getItem('aram-spa-path');
        if (pending) {
            sessionStorage.removeItem('aram-spa-path');
            const here = location.pathname + location.search + location.hash;
            if (pending !== here) history.replaceState(null, '', pending);
        }
        const spaLang = sessionStorage.getItem('aram-spa-lang');
        if (spaLang === 'en' || spaLang === 'zh' || spaLang === 'zh-CN') {
            pendingBootLang = spaLang;
            sessionStorage.removeItem('aram-spa-lang');
        }
    } catch {}
    // URL locale prefix is authoritative (works for direct History visits too).
    try {
        let p = location.pathname || '/';
        if (p.length > 1 && p.endsWith('/')) p = p.slice(0, -1) || '/';
        if (p === '/en' || p.startsWith('/en/')) pendingBootLang = 'en';
        else if (p === '/zh-CN' || p.startsWith('/zh-CN/')) pendingBootLang = 'zh-CN';
    } catch {}
    // Flip chrome strings NOW (script is at end of <body>) so /en or /zh-CN shares
    // never flash the wrong shell while payload/init continues.  Full
    // applyLanguage() still runs later for data-bound copy.
    if (pendingBootLang === 'en' || pendingBootLang === 'zh-CN') {
        try {
            document.documentElement.lang = pendingBootLang === 'en' ? 'en' : 'zh-Hans';
            if (pendingBootLang === 'en') {
                document.querySelectorAll('[data-i18n-zh]').forEach(el => {
                    const val = el.getAttribute('data-i18n-en');
                    if (val != null) el.textContent = val;
                });
                document.querySelectorAll('.chip[data-label-en]').forEach(chip => {
                    const val = chip.getAttribute('data-label-en');
                    if (val != null) chip.textContent = val;
                });
            }
            const toggleLabel = document.getElementById('lang-toggle-label');
            if (toggleLabel) {
                toggleLabel.textContent = pendingBootLang === 'en' ? 'English' : '简体中文';
            }
            const toggle = document.getElementById('lang-toggle');
            if (toggle) {
                const lab = pendingBootLang === 'en' ? 'English' : '简体中文';
                toggle.title = lab;
                toggle.setAttribute('aria-label', 'Language / 語言: ' + lab);
            }
            if (pendingBootLang === 'en') {
                const search = document.getElementById('search');
                if (search) {
                    search.placeholder = 'Search champions (ZH / EN)';
                    search.setAttribute('aria-label', 'Search champions');
                }
                const shownUnit = document.getElementById('shown-unit');
                if (shownUnit) shownUnit.textContent = 'shown';
                document.querySelectorAll('.tier-count-unit').forEach(el => {
                    el.textContent = 'shown';
                });
                const recMode = document.getElementById('recommend-mode');
                if (recMode && recMode.getAttribute('aria-pressed') !== 'true') {
                    recMode.textContent = 'Teammate mode: Off';
                }
                const emptyTitle = document.getElementById('empty-title');
                if (emptyTitle) emptyTitle.textContent = 'No champions match the current filters';
                const emptyCopy = document.getElementById('empty-copy');
                if (emptyCopy) {
                    emptyCopy.textContent =
                        'Try a different role, or search by Chinese / English champion name.';
                }
            }
        } catch {}
    }

    async function loadSitePayload(url) {
        // Resolve against window.location.origin — NOT document.baseURI.
        // Local previews (and production HTML) ship
        //   <base href="https://arammeta.com/">
        // for SPA path shells. Root-relative fetch('/api/...') is resolved
        // against that base in browsers, so localhost would silently load the
        // production payload (and miss local draftModel). Absolute-origin URLs
        // also fix /zh-CN/… and /en/… shells (relative "api/..." would 404 as
        // /zh-CN/api/...). Keep ?v= cache-bust query from the build.
        let resolved;
        if (/^https?:\/\//i.test(String(url))) {
            resolved = String(url);
        } else {
            const path = '/' + String(url).replace(/^\.?\//, '');
            resolved = `${window.location.origin}${path}`;
        }
        // Default HTTP cache: tier-list.json is multi-MB; `no-cache` forced a
        // revalidation on every visit and dominated /zh-CN bounce load time.
        // Build stamps the URL with ?v=YYYYMMDD so publishes still bust cache.
        const response = await fetch(resolved);
        if (!response.ok) {
            throw new Error(`payload ${response.status}: ${resolved}`);
        }
        return await response.json();
    }
    const DATA = await loadSitePayload("api/tier-list.json?v=20260715-1783933964");
    const CHAMP_DETAIL_FIELDS = [
        'bot', 'sets', 'items', 'singleItems', 'boots', 'spells',
        'itemClusters', 'augTypes',
    ];
    const champDetailLoads = new Map();

    function champHasDetail(info) {
        return !DATA.detailBase || CHAMP_DETAIL_FIELDS.some(key => (
            Object.prototype.hasOwnProperty.call(info || {}, key)
        ));
    }

    async function ensureChampDetail(cid) {
        const key = String(cid);
        const info = (DATA.champs || {})[key];
        if (!info || champHasDetail(info)) return info;
        if (champDetailLoads.has(key)) return champDetailLoads.get(key);

        const base = String(DATA.detailBase || '').replace(/\/$/, '');
        const version = DATA.detailVersion
            ? `?v=${encodeURIComponent(String(DATA.detailVersion))}`
            : '';
        const request = loadSitePayload(`${base}/${encodeURIComponent(key)}.json${version}`)
            .then(detail => {
                if (!detail || typeof detail !== 'object' || Array.isArray(detail)) {
                    throw new Error(`invalid champion detail payload: ${key}`);
                }
                Object.assign(info, detail);
                // The startup pass may have marked the summary-only object as
                // rehydrated. Let newly merged item rows run through once.
                _rehydratedChamps.delete(key);
                rehydrateChamp(key);
                const card = document.querySelector(`.champ[data-cid="${key}"]`);
                if (card) enrichChampCard(card);
                return info;
            })
            .finally(() => champDetailLoads.delete(key));
        champDetailLoads.set(key, request);
        return request;
    }
    // Empirical secondary artifacts: fetch in parallel (not waterfall).  Names
    // for zh-CN are optional for first paint (t2s fallback works); load them
    // after the main payload without blocking the dual-API path when unused.
    let ARCHFIT = null;
    let NAMES_ZH_CN = null;
    function attachCnNames() {
        if (!NAMES_ZH_CN) return;
        const augsMap = NAMES_ZH_CN.augs || {};
        const augs = DATA.augs || {};
        for (const id of Object.keys(augsMap)) {
            const a = augs[id];
            if (!a) continue;
            const row = augsMap[id];
            if (row && row.n) a.name_cn = row.n;
            if (row && row.d) a.desc_cn = row.d;
            if (row && row.s) a.set_cn = row.s;
        }
        const itemsMap = NAMES_ZH_CN.items || {};
        const descsMap = NAMES_ZH_CN.itemDescs || {};
        const lut = DATA.itemLut || {};
        for (const id of Object.keys(itemsMap)) {
            const m = lut[id];
            if (!m) continue;
            m.c = itemsMap[id];
            if (descsMap[id]) m.dc = descsMap[id];
        }
    }
    function mergeArchFit(src) {
        if (!src || !src.champs || !DATA.champs) return;
        for (const cid in src.champs) {
            if (DATA.champs[cid]) DATA.champs[cid].archFit = src.champs[cid];
        }
    }
    function mergeAxes(src) {
        if (!src || !src.champs || !DATA.champs) return;
        for (const cid in src.champs) {
            const c = DATA.champs[cid], a = src.champs[cid];
            if (c && c.comp && a) {
                // Tempo card: early/late end-game WR (≤16min / ≥22min).
                // Keep scaling/snowball for the scatter article + archive metrics.
                c.comp.scaling = a.scaling; c.comp.snowball = a.snowball;
                c.comp.early_wr = a.early_wr; c.comp.late_wr = a.late_wr;
                // Radar polygon: damage/tank/cc + gold + fight presence.
                c.comp.e_damage = a.e_damage; c.comp.e_tank = a.e_tank; c.comp.e_cc = a.e_cc;
                c.comp.e_gold = a.e_gold; c.comp.e_participate = a.e_participate;
            }
        }
    }
    // Prefer loading names when the boot locale is already zh-CN (share links);
    // otherwise defer so / and /en don't wait on ~110KB of CN dictionaries.
    const wantCnNamesEarly = pendingBootLang === 'zh-CN';
    // Arch-fit + empirical axes only feed the detail panel / draft radars, so
    // they must NOT gate first render: blocking here used to add a full extra
    // request round-trip AFTER the multi-MB main payload before any wiring ran.
    // Merge on arrival, drop the percentile caches they feed, and repaint any
    // surface that already consumed the stale values.
    const onSecondaryDataMerged = () => {
        // try-guarded: on the /zh-CN boot path this can fire while the CN-name
        // await is still parked BEFORE the let-declarations below (TDZ).
        // Losing one refresh there is harmless — nothing has rendered yet.
        try {
            _compNormCache = null;
            _stageTempoCache = null;
            if (detailSelected) {
                const champ = document.querySelector(`.champ[data-cid="${detailSelected}"]`);
                if (champ) openDetailForChamp(champ, true);
            }
            if (document.querySelector('.view-draft.is-active')) renderDraft();
        } catch {}
    };
    const secondaryFetches = [
        loadSitePayload('api/champ-archetype-fit.json').then(d => { ARCHFIT = d; mergeArchFit(d); onSecondaryDataMerged(); }).catch(() => { ARCHFIT = null; }),
        loadSitePayload('api/champ-empirical-axes.json').then(d => { mergeAxes(d); onSecondaryDataMerged(); }).catch(() => {}),
    ];
    if (wantCnNamesEarly) {
        // CN names ARE first-paint copy for /zh-CN shares — keep that one blocking.
        await loadSitePayload('api/names-zh-cn.json').then(d => { NAMES_ZH_CN = d; attachCnNames(); }).catch(() => { NAMES_ZH_CN = null; });
    } else {
        // Non-blocking: official CN names arrive after first interactive frame.
        loadSitePayload('api/names-zh-cn.json').then(d => {
            NAMES_ZH_CN = d;
            attachCnNames();
        }).catch(() => { NAMES_ZH_CN = null; });
    }
    // Per-champ draft lock-in crop map (from tools/draft-slot-crop.html).
    // Non-blocking; re-render slots when it arrives.
    let DRAFT_SLOT_CROPS = null;
    // High-res splash map (Universe 1920×1080+ vs Data Dragon 1215×717).
    let DRAFT_SPLASH_HD = null;
    function refreshDraftSlotsIfOpen() {
        if (document.querySelector('.view-draft')) {
            try { renderDraftSlots('ally'); renderDraftSlots('enemy'); } catch {}
        }
    }
    loadSitePayload('api/draft-slot-crops.json').then(d => {
        DRAFT_SLOT_CROPS = d || null;
        refreshDraftSlotsIfOpen();
    }).catch(() => { DRAFT_SLOT_CROPS = null; });
    loadSitePayload('api/draft-splash-hd.json').then(d => {
        DRAFT_SPLASH_HD = d || null;
        refreshDraftSlotsIfOpen();
    }).catch(() => { DRAFT_SPLASH_HD = null; });
    const pct = x => (x * 100).toFixed(1) + '%';
    const signed = x => (x >= 0 ? '+' : '') + (x * 100).toFixed(1) + '%';
    const escHtml = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

    // INP: hand the main thread back between chunks of startup work so no single
    // task blocks input for the whole 17 MB payload's post-parse processing.
    // Prefer the native scheduler yield (keeps this task's continuation ahead of
    // unrelated tasks); fall back to a macrotask on browsers without it.
    const yieldToMain = () => (
        (typeof scheduler !== 'undefined' && scheduler.yield)
            ? scheduler.yield()
            : new Promise(r => setTimeout(r, 0))
    );
    // Run in browser idle time when available (background enrichment), else soon.
    const whenIdle = (cb) => (
        (typeof requestIdleCallback === 'function')
            ? requestIdleCallback(cb, { timeout: 500 })
            : setTimeout(cb, 0)
    );

    // Capability dims we percentile-rank per champion (the building blocks of comp fit).
    const COMP_STAT_KEYS = ['front', 'engage', 'poke', 'magic', 'phys', 'sustain', 'cc', 'wave', 'damage', 'scaling', 'snowball', 'early_wr', 'late_wr', 'e_damage', 'e_tank', 'e_cc', 'e_gold', 'e_participate'];
    // The 6 comp archetypes. Each is a weighted blend of the capabilities that comp is
    // built around — a champion's fit = how much it supplies those, ranked across all champs.
    // The 6 aren't mutually exclusive (a champ can fit several); together they span most comps.
    const COMP_FIT_DEFS = [
        { key: 'dive',      w: { engage: 0.45, cc: 0.3, front: 0.25 }, zh: { name: '衝排',     desc: '高CC、能先手切入，適合衝臉/開團陣' },     en: { name: 'Dive',         desc: 'hard CC and engage — fits a dive / all-in comp' } },
        { key: 'poke',      w: { poke: 0.7, wave: 0.3 },               zh: { name: '消耗',     desc: '遠程消耗、清線，適合 poke 拉扯陣' },       en: { name: 'Poke',         desc: 'ranged harass and waveclear — fits a poke / siege comp' } },
        { key: 'adc',       w: { phys: 0.6, damage: 0.4 },             zh: { name: '物理後排', desc: '物理持續輸出核心，適合保護後排的物理 carry 陣' }, en: { name: 'AD carry',  desc: 'sustained physical DPS — fits a protect-the-carry comp' } },
        { key: 'mage',      w: { magic: 0.6, damage: 0.4 },            zh: { name: '法師核心', desc: '主要法傷輸出，適合圍繞他的法師核心陣' },   en: { name: 'Mage core',    desc: 'magic damage core — fits an AP-centric comp' } },
        { key: 'sustain',   w: { sustain: 0.6, poke: 0.25, cc: 0.15 }, zh: { name: '續航',     desc: '高回復、拉打耗血，適合持久續航陣' },       en: { name: 'Sustain',      desc: 'healing and kiting — fits an attrition / sustain comp' } },
        { key: 'frontback', w: { front: 0.4, damage: 0.35, cc: 0.25 }, zh: { name: '前後排',   desc: '穩固前排＋輸出，適合站樁前後排陣' },       en: { name: 'Front-to-back', desc: 'tanky front and a carry — fits a stand-and-deliver comp' } },
    ];
    const COMP_FIT_ADVICE_THRESHOLD = 0.6;  // fit percentile (0-1) needed to surface a comp as advice (heuristic fallback)
    const COMP_FIT_EMP_POS = 0.4;   // empirical delta pp to call a comp a fit ("build this around him")
    const COMP_FIT_EMP_NEG = -0.4;  // empirical delta pp to call a comp one to avoid (redundant teammates)
    // Raw-ability bars shown beside the radar (champion capability percentiles, not comp fit).
    // Six-axis ability radar (percentiles). Early/late tempo lives in the side card,
    // not on the polygon — gold + fight presence are more readable on a radar.
    const ABILITY_BARS = [
        { key: 'e_damage',      zh: '傷害', en: 'Damage' },
        { key: 'e_tank',        zh: '坦度', en: 'Tank' },
        { key: 'e_cc',          zh: '控場', en: 'CC' },
        { key: 'sustain',       zh: '恢復', en: 'Sustain' },
        { key: 'e_participate', zh: '參團', en: 'Fight' },
        { key: 'e_gold',        zh: '金錢', en: 'Gold' },
    ];
    // Per-dim sorted value list across all champions, computed once (DATA.champs is static).
    let _compNormCache = null;
    function compNormStats() {
        if (_compNormCache) return _compNormCache;
        const cols = {};
        COMP_STAT_KEYS.forEach(k => { cols[k] = []; });
        Object.values(DATA.champs || {}).forEach(info => {
            const comp = info && info.comp;
            if (!comp) return;
            COMP_STAT_KEYS.forEach(k => { cols[k].push(Number(comp[k] || 0)); });
        });
        COMP_STAT_KEYS.forEach(k => cols[k].sort((a, b) => a - b));
        _compNormCache = cols;
        return _compNormCache;
    }
    // Radar geometry: modest L/R pad (long EN labels clamp themselves).
    // Keep pad tight so the hex sits left instead of floating in empty space.
    const RADAR_PAD_X = 28;
    const RADAR_VB_W = 380 + RADAR_PAD_X * 2; // 436
    const RADAR_VB_H = 320;
    const RADAR_CX = 190 + RADAR_PAD_X;
    const RADAR_CY = 158;
    const RADAR_R = 105;

    /** Split long EN axis labels for multi-line SVG text (e.g. "Champ strength"). */
    function radarLabelLines(label) {
        const s = String(label || '').trim();
        if (!s) return [''];
        // Prefer "Champ strength" → Champ / strength (case-preserving first word)
        if (/^champ\s+strength$/i.test(s)) {
            const parts = s.split(/\s+/);
            return [parts[0], parts.slice(1).join(' ')];
        }
        // Other multi-word Latin labels ≥ 10 chars: first word / rest
        if (/\s/.test(s) && s.length >= 10 && !/[\u4e00-\u9fff]/.test(s)) {
            const parts = s.split(/\s+/).filter(Boolean);
            if (parts.length >= 2) return [parts[0], parts.slice(1).join(' ')];
        }
        return [s];
    }

    /** Build <tspan> stack for multi-line radar labels; each line re-sets x for text-anchor. */
    function radarLabelTspans(lines, lx, fontSize) {
        const x = lx.toFixed(1);
        const dy = (fontSize * 1.15).toFixed(1);
        return lines.map((line, i) => (
            `<tspan x="${x}"${i === 0 ? '' : ` dy="${dy}"`}>${escHtml(line)}</tspan>`
        )).join('');
    }

    // axes: heuristic mode [{label, pct 0-1}]; signed mode (opts.signed) [{label, delta pp}].
    // Signed mode draws a dashed 0pp baseline ring; out=fits (blue), in=avoid (red); scale pp at full radius.
    function compRadarSvg(axes, ariaLabel, opts) {
        opts = opts || {};
        const signed = !!opts.signed, scale = opts.scale || 2;
        const cx = RADAR_CX, cy = RADAR_CY, R = RADAR_R, n = axes.length;
        const ang = i => (-90 + i * (360 / n)) * Math.PI / 180;
        const at = (i, r) => [cx + r * Math.cos(ang(i)), cy + r * Math.sin(ang(i))];
        const ringPts = f => axes.map((_, i) => at(i, R * f).map(v => v.toFixed(1)).join(',')).join(' ');
        const grid = [0.25, 0.5, 0.75, 1].map(f =>
            `<polygon points="${ringPts(f)}" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="1"/>`).join('');
        const spokes = axes.map((_, i) => {
            const [x, y] = at(i, R);
            return `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,0.08)" stroke-width="1"/>`;
        }).join('');
        const frac = a => signed
            ? Math.max(0.02, Math.min(1, 0.5 + Math.max(-1, Math.min(1, (a.delta || 0) / scale)) * 0.5))
            : Math.max(0, Math.min(1, a.pct || 0));
        const baseline = signed
            ? `<polygon points="${ringPts(0.5)}" fill="none" stroke="rgba(255,255,255,0.32)" stroke-width="1" stroke-dasharray="3 3"/>`
            : '';
        const dataPts = axes.map((a, i) => at(i, R * frac(a)));
        const dataPoly = dataPts.map(p => p.map(v => v.toFixed(1)).join(',')).join(' ');
        const dotCol = a => signed ? ((a.delta || 0) >= 0 ? '#3aa0ff' : '#e2574b') : '#3aa0ff';
        const dots = dataPts.map((p, i) => `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="3.2" fill="${dotCol(axes[i])}"/>`).join('');
        const fontSize = 15;
        const labels = axes.map((a, i) => {
            // Side spokes: pull labels slightly inward so long text stays inside pad.
            const cosA = Math.cos(ang(i));
            const labelR = R + 22 - (Math.abs(cosA) > 0.55 ? 6 : 0);
            let [lx, ly] = at(i, labelR);
            const anchor = Math.abs(lx - cx) < 1 ? 'middle' : (lx > cx ? 'start' : 'end');
            const valTxt = signed ? ((a.delta || 0) >= 0 ? '+' : '') + (a.delta || 0).toFixed(1) : String(Math.round((a.pct || 0) * 100));
            const valCol = signed ? ((a.delta || 0) >= 0 ? '#7fc8ff' : '#f0998a') : '#7fc8ff';
            const lines = radarLabelLines(a.label);
            const longest = lines.reduce((m, L) => Math.max(m, L.length), 0);
            const estW = Math.max(32, (longest + String(valTxt).length + 1) * 8.6);
            const edgePad = 4;
            if (anchor === 'end') lx = Math.max(lx, estW + edgePad);
            if (anchor === 'start') lx = Math.min(lx, RADAR_VB_W - estW - edgePad);
            // Multi-line: baseline at first line; shift up so stack is centered on spoke tip.
            const y0 = ly + 4 - ((lines.length - 1) * fontSize * 1.15) / 2;
            const body = radarLabelTspans(lines, lx, fontSize);
            const val = `<tspan fill="${valCol}"> ${valTxt}</tspan>`;
            // Put the numeric value on the last line of the stack.
            const bodyWithVal = lines.length <= 1
                ? `${body}${val}`
                : radarLabelTspans(lines.slice(0, -1), lx, fontSize)
                    + `<tspan x="${lx.toFixed(1)}" dy="${(fontSize * 1.15).toFixed(1)}">${escHtml(lines[lines.length - 1])}</tspan>${val}`;
            return `<text x="${lx.toFixed(1)}" y="${y0.toFixed(1)}" font-size="${fontSize}" font-weight="600" text-anchor="${anchor}" fill="#c2c7ce">${bodyWithVal}</text>`;
        }).join('');
        const fillCol = signed ? 'rgba(120,130,140,0.16)' : 'rgba(58,160,255,0.18)';
        const strokeCol = signed ? 'rgba(160,170,180,0.85)' : '#3aa0ff';
        return `<svg class="comp-radar" viewBox="0 0 ${RADAR_VB_W} ${RADAR_VB_H}" width="100%" role="img" aria-label="${escHtml(ariaLabel)}">${grid}${baseline}${spokes}<polygon points="${dataPoly}" fill="${fillCol}" stroke="${strokeCol}" stroke-width="2"/>${dots}${labels}</svg>`;
    }

    /**
     * Dual-team radar: same axes, two polygons (ally theme accent / enemy gray).
     * series: [{ axes: [{label,pct}], stroke, fill, dot }]
     * Labels use axis names only (values live in the bar compare below).
     */
    function compRadarOverlaySvg(series, ariaLabel) {
        const first = (series && series[0] && series[0].axes) || [];
        const n = first.length;
        if (!n) return '';
        const cx = RADAR_CX, cy = RADAR_CY, R = RADAR_R;
        const ang = i => (-90 + i * (360 / n)) * Math.PI / 180;
        const at = (i, r) => [cx + r * Math.cos(ang(i)), cy + r * Math.sin(ang(i))];
        const ringPts = f => first.map((_, i) => at(i, R * f).map(v => v.toFixed(1)).join(',')).join(' ');
        const grid = [0.25, 0.5, 0.75, 1].map(f =>
            `<polygon points="${ringPts(f)}" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="1"/>`).join('');
        const spokes = first.map((_, i) => {
            const [x, y] = at(i, R);
            return `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,0.08)" stroke-width="1"/>`;
        }).join('');
        const polys = (series || []).map(s => {
            const axes = s.axes || [];
            const pts = axes.map((a, i) => {
                const f = Math.max(0, Math.min(1, Number(a.pct) || 0));
                return at(i, R * f).map(v => v.toFixed(1)).join(',');
            }).join(' ');
            const stroke = s.stroke || '#3aa0ff';
            const fill = s.fill || 'rgba(58,160,255,0.16)';
            const dots = axes.map((a, i) => {
                const f = Math.max(0, Math.min(1, Number(a.pct) || 0));
                const [x, y] = at(i, R * f);
                return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" fill="${s.dot || stroke}"/>`;
            }).join('');
            return `<polygon points="${pts}" fill="${fill}" stroke="${stroke}" stroke-width="2.2"/>${dots}`;
        }).join('');
        const fontSize = 15;
        const labels = first.map((a, i) => {
            const cosA = Math.cos(ang(i));
            // Pull side labels inward; multi-line "Champ strength" fits tighter.
            const labelR = R + 20 - (Math.abs(cosA) > 0.55 ? 6 : 0);
            let [lx, ly] = at(i, labelR);
            const anchor = Math.abs(lx - cx) < 1 ? 'middle' : (lx > cx ? 'start' : 'end');
            const lines = radarLabelLines(a.label);
            const longest = lines.reduce((m, L) => Math.max(m, L.length), 0);
            // Width estimate uses longest line only (wrap shortens horizontal span).
            const estW = Math.max(28, longest * 8.6);
            const edgePad = 4;
            if (anchor === 'end') lx = Math.max(lx, estW + edgePad);
            if (anchor === 'start') lx = Math.min(lx, RADAR_VB_W - estW - edgePad);
            const y0 = ly + 4 - ((lines.length - 1) * fontSize * 1.15) / 2;
            return (
                `<text x="${lx.toFixed(1)}" y="${y0.toFixed(1)}" font-size="${fontSize}" font-weight="600" text-anchor="${anchor}" fill="#d4d8de">`
                + radarLabelTspans(lines, lx, fontSize)
                + `</text>`
            );
        }).join('');
        return `<svg class="comp-radar is-overlay" viewBox="0 0 ${RADAR_VB_W} ${RADAR_VB_H}" width="100%" role="img" aria-label="${escHtml(ariaLabel || '')}">${grid}${spokes}${polys}${labels}</svg>`;
    }
    // Percentile rank 0-1: fraction of champions this value beats (robust to outliers).
    function compNorm(comp, key) {
        const arr = compNormStats()[key];
        const n = arr ? arr.length : 0;
        if (n < 2) return 0;
        const v = Number((comp && comp[key]) || 0);
        let lt = 0;
        while (lt < n && arr[lt] < v) lt += 1;  // count champions strictly weaker
        return Math.max(0, Math.min(1, lt / (n - 1)));
    }
    // Early/late tempo ranks: score = late_wr − early_wr (+ = late-leaning).
    // PR 40–60 (inclusive) → balanced; outside → early/late with direction rank.
    const STAGE_PR_LO = 40;
    const STAGE_PR_HI = 60;
    let _stageTempoCache = null;
    function stageTempoStats() {
        if (_stageTempoCache) return _stageTempoCache;
        const rows = [];
        Object.entries(DATA.champs || {}).forEach(([cid, info]) => {
            const c = info && info.comp;
            if (!c) return;
            const e = Number(c.early_wr), l = Number(c.late_wr);
            if (!Number.isFinite(e) || !Number.isFinite(l)) return;
            rows.push({ cid: String(cid), score: l - e });
        });
        rows.sort((a, b) => a.score - b.score || a.cid.localeCompare(b.cid)); // early → late
        const byCid = {};
        const n = rows.length;
        rows.forEach((r, i) => {
            const pr = n <= 1 ? 50 : Math.round((i / (n - 1)) * 100);
            byCid[r.cid] = {
                score: r.score,
                pr,
                earlyRank: i + 1,  // 1 = most early
                lateRank: n - i,   // 1 = most late
                n,
            };
        });
        _stageTempoCache = { byCid, n };
        return _stageTempoCache;
    }
    function stageTempoFor(cid) {
        if (cid == null || cid === '') return null;
        return stageTempoStats().byCid[String(cid)] || null;
    }
    // Raw comp-fit score: weighted blend of a champion's capability percentiles.
    function compFitRaw(def, capPct) {
        let s = 0;
        for (const k in def.w) s += def.w[k] * (capPct[k] || 0);
        return s;
    }
    function compCapPct(comp) {
        const cap = {};
        COMP_STAT_KEYS.forEach(k => { cap[k] = compNorm(comp, k); });
        return cap;
    }
    // Per-comp sorted fit scores across all champions, computed once (DATA.champs is static).
    let _compFitCache = null;
    function compFitStats() {
        if (_compFitCache) return _compFitCache;
        const cols = {};
        COMP_FIT_DEFS.forEach(d => { cols[d.key] = []; });
        Object.values(DATA.champs || {}).forEach(info => {
            const comp = info && info.comp;
            if (!comp) return;
            const cap = compCapPct(comp);
            COMP_FIT_DEFS.forEach(d => { cols[d.key].push(compFitRaw(d, cap)); });
        });
        COMP_FIT_DEFS.forEach(d => cols[d.key].sort((a, b) => a - b));
        _compFitCache = cols;
        return _compFitCache;
    }
    // Ability profile tab: 6-axis capability radar (left) + skill-scaling & early/late
    // stage (right). Comp-archetype fit used to own this tab; it was demoted because
    // most champions sit near 0pp and the radar looked empty — abilities + tempo are
    // what players actually read. Strong archetype avoid/build signals still surface
    // as a one-line footer when |delta| clears the empirical thresholds.
    function buildCompFit(info, cid) {
        const copy = tr();
        const comp = (info && info.comp) || {};
        const cap = compCapPct(comp);
        const abilityAxes = ABILITY_BARS.map(a => ({
            label: pickLang(a.zh, a.en),
            pct: cap[a.key] || 0,
        }));
        const radar = compRadarSvg(abilityAxes, copy.compFitTitle);

        // Skill-scaling ("operation coefficient"): WR(high-skill) − WR(low-skill).
        const ss = info && info.skillScaling;
        let skillCard = `<div class="cf-side-card cf-side-muted"><div class="cf-side-kicker">${escHtml(copy.compSkillTitle)}</div><div class="cf-side-empty">${escHtml(copy.compSkillMissing)}</div></div>`;
        if (ss && typeof ss.pp === 'number') {
            const strong = Math.abs(ss.z || 0) >= 2 && Math.abs(ss.pp) >= 2;
            const pos = ss.pp >= 0;
            const ppTxt = (pos ? '+' : '') + ss.pp.toFixed(1) + 'pp';
            const col = !strong ? '#9aa3ad' : (pos ? '#3aa0ff' : '#e2574b');
            const lbl = !strong ? copy.compSkillNeutral : (pos ? copy.compSkillHigh : copy.compSkillLow);
            const gamesTxt = ss.g ? copy.compSkillGames(ss.g) : '';
            skillCard = `
                <div class="cf-side-card" title="${escHtml(copy.compSkillTip)}">
                    <div class="cf-side-kicker">${escHtml(copy.compSkillTitle)}</div>
                    <div class="cf-side-value" style="color:${col}">${escHtml(ppTxt)}</div>
                    <div class="cf-side-label" style="color:${col}">${escHtml(lbl)}</div>
                    ${gamesTxt ? `<div class="cf-side-sub">${escHtml(gamesTxt)}</div>` : ''}
                </div>`;
        }

        // Early/late tempo = win rate when the game ends early (≤16min) vs late (≥22min).
        // Bars + numbers are absolute WR%. Label uses all-champ PR of (late−early):
        // PR 40–60 = balanced; outside that band → early/late + direction rank (#1 most extreme).
        const earlyWr = Number(comp.early_wr);
        const lateWr = Number(comp.late_wr);
        const hasStageWr = Number.isFinite(earlyWr) && Number.isFinite(lateWr);
        const earlyPct = hasStageWr ? Math.round(earlyWr * 100) : 0;
        const latePct = hasStageWr ? Math.round(lateWr * 100) : 0;
        const stageMeta = hasStageWr ? stageTempoFor(cid) : null;
        let stageKey = 'balanced';
        if (stageMeta) {
            if (stageMeta.pr < STAGE_PR_LO) stageKey = 'early';
            else if (stageMeta.pr > STAGE_PR_HI) stageKey = 'late';
        }
        const stageRank = stageMeta
            ? (stageKey === 'early' ? stageMeta.earlyRank
                : stageKey === 'late' ? stageMeta.lateRank : null)
            : null;
        const stageTitle = !hasStageWr ? copy.compSkillMissing
            : stageKey === 'early' ? copy.compStageEarly
            : stageKey === 'late' ? copy.compStageLate
            : copy.compStageBalanced;
        const stageTitleHtml = stageRank != null
            ? `${escHtml(stageTitle)}<span class="cf-stage-rank">#${stageRank}</span>`
            : escHtml(stageTitle);
        const stageDesc = !hasStageWr ? ''
            : stageKey === 'early' ? copy.compStageEarlyDesc
            : stageKey === 'late' ? copy.compStageLateDesc
            : copy.compStageBalancedDesc;
        // Hover tip: gap + PR rail + PR/#rank-of-N. Type / WR already on card.
        let stageTipHtml = '';
        if (hasStageWr) {
            const gapPp = (earlyWr - lateWr) * 100; // + = early stronger
            const gapTxt = (gapPp >= 0 ? '+' : '') + gapPp.toFixed(1) + ' pp';
            const gapCls = Math.abs(gapPp) < 0.05 ? 'is-even'
                : (gapPp > 0 ? 'is-early' : 'is-late');
            const pr = stageMeta ? stageMeta.pr : 50;
            const nAll = stageMeta ? stageMeta.n : 0;
            const prTxt = copy.compStageTipPr ? copy.compStageTipPr(pr) : `PR ${pr}`;
            // Direction rank with pool size (e.g. #17/173); omit when balanced.
            const rankTxt = stageRank != null && nAll
                ? (copy.compStageTipRank
                    ? copy.compStageTipRank(stageRank, nAll)
                    : `#${stageRank}/${nAll}`)
                : '';
            const markerLeft = Math.max(2, Math.min(98, pr));
            const footLines = Array.isArray(copy.compStageTipFootLines)
                ? copy.compStageTipFootLines
                : [copy.compStageTipFoot || copy.compStageTip || ''];
            const footHtml = footLines.filter(Boolean).map(line => (
                `<div class="cf-stage-tip-foot-line">${escHtml(line)}</div>`
            )).join('');
            stageTipHtml = `
                <div class="cf-stage-tip" role="tooltip">
                    <div class="cf-stage-tip-gap ${gapCls}">
                        <span>${escHtml(copy.compStageTipGap || 'Gap')}</span>
                        <b>${escHtml(gapTxt)}</b>
                    </div>
                    <div class="cf-stage-tip-rail" aria-hidden="true">
                        <span class="cf-stage-tip-rail-end is-early">${escHtml(copy.compStageEarlyAxis)}</span>
                        <div class="cf-stage-tip-rail-track">
                            <span class="cf-stage-tip-rail-band"></span>
                            <span class="cf-stage-tip-rail-dot" style="left:${markerLeft}%"></span>
                        </div>
                        <span class="cf-stage-tip-rail-end is-late">${escHtml(copy.compStageLateAxis)}</span>
                    </div>
                    <div class="cf-stage-tip-meta">
                        <span class="cf-stage-tip-pr">${escHtml(prTxt)}</span>
                        ${rankTxt ? `<span class="cf-stage-tip-rank">${escHtml(rankTxt)}</span>` : ''}
                    </div>
                    ${footHtml ? `<div class="cf-stage-tip-foot">${footHtml}</div>` : ''}
                </div>`;
        }
        const stageCard = `
            <div class="cf-side-card cf-stage-host cf-stage-host-${stageKey}" tabindex="0"
                 aria-label="${escHtml(copy.compStageTitle + ' · ' + stageTitle + (stageRank != null ? ' #' + stageRank : ''))}">
                <div class="cf-side-kicker">${escHtml(copy.compStageTitle)}</div>
                <div class="cf-side-value cf-stage-${stageKey}">${stageTitleHtml}</div>
                ${stageDesc ? `<div class="cf-side-sub">${escHtml(stageDesc)}</div>` : ''}
                <div class="cf-stage-bars">
                    <div class="ab-row"><span class="ab-label">${escHtml(copy.compStageEarlyAxis)}</span><span class="ab-bar ab-bar-early"><span style="width:${earlyPct}%"></span></span><span class="ab-val">${hasStageWr ? earlyPct + '%' : '—'}</span></div>
                    <div class="ab-row"><span class="ab-label">${escHtml(copy.compStageLateAxis)}</span><span class="ab-bar ab-bar-late"><span style="width:${latePct}%"></span></span><span class="ab-val">${hasStageWr ? latePct + '%' : '—'}</span></div>
                </div>
                ${stageTipHtml}
            </div>`;

        // Optional: only surface clear empirical archetype signals (most champs are ~0pp).
        let adviceHtml = '';
        const af = info && info.archFit;
        if (af && af.qualified && af.fit) {
            const signedPp = d => ((d >= 0 ? '+' : '') + d.toFixed(1) + 'pp');
            const fits = COMP_FIT_DEFS.map(def => ({ def, name: pickLang(def.zh.name, def.en.name), delta: Number((af.fit[def.key] || {}).delta || 0) }));
            const best = [...fits].sort((a, b) => b.delta - a.delta).filter(f => f.delta >= COMP_FIT_EMP_POS).slice(0, 1);
            const avoid = [...fits].sort((a, b) => a.delta - b.delta).filter(f => f.delta <= COMP_FIT_EMP_NEG).slice(0, 1);
            const items = [];
            best.forEach(f => items.push(
                `<div class="comp-advice-item"><span class="ca-tag">${escHtml(copy.compFitPrefer)}：${escHtml(pickLang(f.def.zh.name, f.def.en.name))} ${signedPp(f.delta)}</span><span class="ca-desc">${escHtml(pickLang(f.def.zh.desc, f.def.en.desc))}</span></div>`));
            avoid.forEach(f => items.push(
                `<div class="comp-advice-item"><span class="ca-tag ca-tag-avoid">${escHtml(copy.compFitAvoid)}：${escHtml(pickLang(f.def.zh.name, f.def.en.name))} ${signedPp(f.delta)}</span><span class="ca-desc">${escHtml(copy.compFitAvoidDesc)}</span></div>`));
            if (items.length) adviceHtml = `<div class="comp-advice"><div class="cf-cap">${escHtml(copy.compFitFootnote)}</div>${items.join('')}</div>`;
        }

        return `
            <div class="detail-section">
                <div class="detail-section-head">
                    <h3>${escHtml(copy.compFitTitle)}</h3>
                    <span class="section-meta">${escHtml(copy.compFitMeta)}</span>
                </div>
                <div class="comp-fit-main">
                    <div class="comp-fit-radar">${radar}</div>
                    <div class="comp-fit-side">
                        ${skillCard}
                        ${stageCard}
                    </div>
                </div>
                ${adviceHtml}
            </div>
        `;
    }
    const ROLE_LABELS = {"zh": {"Assassin": "刺客", "Fighter": "戰士", "Mage": "法師", "Marksman": "射手", "Support": "輔助", "Tank": "坦克"}, "en": {"Assassin": "Assassin", "Fighter": "Fighter", "Mage": "Mage", "Marksman": "Marksman", "Support": "Support", "Tank": "Tank"}};
    // itemId → Assassin|Fighter|Mage|Marksman|Support|Tank  (shell-injected from
    // CDragon item styles; empty object when catalogue unavailable).
    const ITEM_FILTER_ROLES = {"2049":["Mage"],"2050":["Mage"],"2051":["Tank"],"2065":["Support"],"2501":["Fighter"],"2502":["Tank"],"2503":["Mage"],"2504":["Tank"],"2510":["Mage"],"2512":["Marksman"],"2517":["Fighter","Marksman"],"2520":["Marksman"],"2522":["Mage"],"2523":["Marksman"],"2524":["Support"],"2525":["Tank"],"2526":["Support"],"2530":["Support"],"3001":["Tank"],"3002":["Tank"],"3003":["Mage"],"3004":["Assassin","Fighter","Marksman"],"3011":["Support"],"3026":["Fighter"],"3031":["Fighter","Marksman"],"3032":["Marksman"],"3033":["Fighter","Marksman"],"3036":["Fighter","Marksman"],"3039":["Fighter"],"3040":["Mage"],"3042":["Assassin","Fighter","Marksman"],"3046":["Fighter","Marksman"],"3050":["Support","Tank"],"3053":["Fighter"],"3065":["Tank"],"3068":["Tank"],"3071":["Fighter"],"3072":["Fighter","Marksman"],"3073":["Fighter"],"3074":["Fighter"],"3075":["Tank"],"3078":["Fighter","Marksman"],"3083":["Tank"],"3084":["Tank"],"3085":["Marksman"],"3087":["Mage","Marksman"],"3089":["Mage"],"3091":["Fighter","Marksman"],"3094":["Marksman"],"3095":["Marksman"],"3097":["Marksman"],"3100":["Mage"],"3102":["Mage"],"3107":["Support"],"3109":["Support","Tank"],"3110":["Tank"],"3112":["Mage"],"3115":["Mage","Marksman"],"3116":["Fighter","Mage"],"3118":["Mage"],"3119":["Tank"],"3121":["Tank"],"3124":["Marksman"],"3128":["Mage"],"3131":["Marksman"],"3135":["Mage"],"3137":["Mage"],"3139":["Marksman"],"3142":["Assassin"],"3143":["Tank"],"3146":["Fighter","Mage"],"3152":["Mage"],"3153":["Fighter","Marksman"],"3156":["Fighter"],"3157":["Mage"],"3161":["Fighter"],"3165":["Mage"],"3177":["Assassin","Fighter","Marksman"],"3179":["Assassin"],"3181":["Fighter"],"3184":["Fighter","Marksman"],"3190":["Support","Tank"],"3193":["Tank"],"3222":["Support"],"3302":["Marksman"],"3430":["Mage","Marksman"],"3504":["Support"],"3508":["Marksman"],"3742":["Tank"],"3748":["Fighter","Tank"],"3814":["Assassin"],"4004":["Assassin"],"4005":["Mage","Support"],"4010":["Fighter"],"4011":["Support"],"4012":["Tank"],"4013":["Fighter"],"4014":["Marksman"],"4015":["Mage"],"4016":["Mage"],"4017":["Marksman"],"4401":["Tank"],"4402":["Support"],"4403":["Mage","Marksman"],"4628":["Mage"],"4629":["Mage"],"4633":["Fighter","Mage"],"4636":["Mage"],"4637":["Mage"],"4643":["Tank"],"4644":["Fighter"],"4645":["Mage"],"4646":["Mage"],"6035":["Fighter"],"6333":["Fighter"],"6609":["Fighter"],"6610":["Fighter"],"6616":["Support"],"6617":["Support"],"6620":["Support"],"6621":["Support"],"6630":["Fighter"],"6631":["Fighter"],"6632":["Marksman"],"6653":["Mage"],"6655":["Mage"],"6656":["Fighter"],"6657":["Fighter","Mage"],"6662":["Tank"],"6664":["Tank"],"6665":["Tank"],"6667":["Tank"],"6671":["Marksman"],"6672":["Marksman"],"6673":["Fighter","Marksman"],"6675":["Marksman"],"6676":["Fighter","Marksman"],"6691":["Assassin"],"6692":["Assassin","Fighter"],"6693":["Assassin"],"6694":["Assassin","Fighter"],"6695":["Assassin","Fighter"],"6696":["Assassin","Fighter"],"6697":["Assassin"],"6698":["Assassin","Fighter"],"6699":["Assassin"],"6700":["Fighter"],"6701":["Assassin"],"8001":["Tank"],"8010":["Mage"],"8020":["Tank"],"123430":["Mage","Marksman"],"124011":["Support"],"126697":["Assassin","Fighter"],"222051":["Tank"],"222065":["Support"],"222502":["Tank"],"222503":["Mage"],"222504":["Tank"],"222510":["Marksman"],"222512":["Marksman"],"222517":["Fighter"],"222522":["Mage"],"222523":["Marksman"],"222524":["Tank"],"222525":["Tank"],"222526":["Tank"],"222530":["Tank"],"223001":["Tank"],"223002":["Tank"],"223003":["Mage"],"223004":["Marksman"],"223011":["Support"],"223026":["Fighter"],"223031":["Marksman"],"223032":["Marksman"],"223033":["Marksman"],"223036":["Marksman"],"223039":["Marksman"],"223040":["Mage"],"223042":["Marksman"],"223046":["Marksman"],"223050":["Tank"],"223053":["Fighter"],"223057":["Marksman"],"223065":["Tank"],"223068":["Tank"],"223069":["Tank"],"223071":["Marksman"],"223072":["Fighter"],"223073":["Marksman"],"223074":["Marksman"],"223075":["Tank"],"223078":["Marksman"],"223084":["Tank"],"223085":["Marksman"],"223087":["Marksman"],"223089":["Mage"],"223091":["Marksman"],"223094":["Marksman"],"223095":["Marksman"],"223100":["Marksman"],"223102":["Fighter"],"223107":["Support"],"223109":["Tank"],"223110":["Tank"],"223112":["Mage"],"223115":["Marksman"],"223116":["Fighter"],"223118":["Mage"],"223119":["Tank"],"223121":["Tank"],"223124":["Marksman"],"223135":["Mage"],"223137":["Mage"],"223139":["Fighter"],"223142":["Assassin"],"223143":["Tank"],"223146":["Mage"],"223152":["Mage"],"223153":["Marksman"],"223156":["Fighter"],"223157":["Fighter"],"223161":["Fighter"],"223165":["Fighter"],"223172":["Marksman"],"223177":["Fighter"],"223181":["Fighter"],"223184":["Marksman"],"223185":["Assassin"],"223190":["Support"],"223193":["Tank"],"223222":["Support"],"223302":["Marksman"],"223504":["Support"],"223508":["Marksman"],"223742":["Tank"],"223748":["Marksman"],"223814":["Assassin"],"224004":["Assassin"],"224005":["Support"],"224401":["Tank"],"224403":["Mage","Marksman"],"224628":["Mage"],"224629":["Fighter"],"224633":["Fighter"],"224636":["Mage"],"224637":["Mage"],"224644":["Fighter"],"224645":["Mage"],"224646":["Mage"],"226035":["Fighter"],"226333":["Fighter"],"226609":["Fighter"],"226610":["Fighter"],"226616":["Support"],"226617":["Support"],"226620":["Support"],"226621":["Support"],"226630":["Fighter"],"226631":["Marksman"],"226632":["Marksman"],"226653":["Mage"],"226655":["Mage"],"226656":["Fighter"],"226657":["Fighter"],"226662":["Marksman"],"226664":["Tank"],"226665":["Tank"],"226667":["Tank"],"226671":["Marksman"],"226672":["Marksman"],"226673":["Marksman"],"226675":["Marksman"],"226676":["Marksman"],"226691":["Assassin"],"226692":["Fighter"],"226693":["Assassin"],"226694":["Marksman"],"226695":["Assassin"],"226696":["Assassin"],"226697":["Assassin"],"226698":["Assassin"],"226699":["Assassin"],"226701":["Assassin"],"228001":["Tank"],"228002":["Mage"],"228003":["Marksman"],"228004":["Tank"],"228005":["Marksman"],"228006":["Marksman"],"228008":["Marksman"],"228020":["Fighter"],"322065":["Support"],"322526":["Tank"],"322530":["Tank"],"323002":["Tank"],"323003":["Mage"],"323004":["Marksman"],"323040":["Mage"],"323042":["Marksman"],"323050":["Tank"],"323075":["Tank"],"323107":["Support"],"323109":["Tank"],"323110":["Tank"],"323119":["Tank"],"323121":["Tank"],"323190":["Support"],"323222":["Support"],"323504":["Support"],"324005":["Support"],"326616":["Support"],"326617":["Support"],"326620":["Support"],"326621":["Support"],"326657":["Fighter"],"328020":["Tank"],"443054":["Marksman"],"443055":["Marksman"],"443056":["Fighter"],"443058":["Tank"],"443059":["Tank"],"443060":["Mage","Marksman"],"443061":["Marksman"],"443062":["Fighter"],"443063":["Tank"],"443064":["Mage","Marksman"],"443069":["Marksman"],"443079":["Tank"],"443080":["Fighter"],"443081":["Marksman"],"443083":["Tank"],"443090":["Marksman"],"443193":["Tank"],"444636":["Mage"],"444637":["Mage"],"444644":["Fighter"],"446632":["Marksman"],"446656":["Fighter"],"446667":["Tank"],"446671":["Marksman"],"446691":["Assassin"],"447100":["Mage"],"447101":["Marksman"],"447102":["Marksman"],"447103":["Fighter"],"447104":["Support"],"447105":["Mage"],"447106":["Marksman"],"447107":["Mage"],"447108":["Mage"],"447109":["Fighter"],"447110":["Fighter"],"447111":["Fighter"],"447112":["Mage"],"447113":["Mage"],"447114":["Marksman"],"447115":["Assassin"],"447116":["Fighter"],"447118":["Mage"],"447119":["Marksman"],"447120":["Marksman"],"447121":["Fighter"],"447122":["Marksman"],"447123":["Support"],"663039":["Marksman"],"663056":["Fighter"],"663058":["Tank"],"663059":["Tank"],"663060":["Mage","Marksman"],"663146":["Mage"],"663172":["Marksman"],"663193":["Tank"],"664011":["Support"],"664403":["Mage","Marksman"],"664644":["Fighter"],"667101":["Assassin"],"667109":["Fighter"],"667112":["Mage"],"667666":["Marksman"],"994403":["Assassin","Fighter","Mage","Marksman","Support","Tank"],"3076":["Tank"],"3123":["Assassin","Fighter","Marksman"],"3916":["Mage","Support"]};
    const ITEM_FILTER_ROLE_ORDER = ['Assassin', 'Fighter', 'Mage', 'Marksman', 'Support', 'Tank'];
    // 「常見」= high pick-rate on this champion (matches common-trap force floor).
    const SINGLE_ITEM_COMMON_MIN_PICK = 0.10;
    // Session-sticky single-item filter ('' | role key | 'common').
    let singleItemFilter = '';
    const ROLE_BADGE_ICONS = {
        Assassin: `
            <svg viewBox="0 0 16 16" aria-hidden="true">
                <path fill="currentColor" d="M8 1.7c1.1 1.72 3.46 4.16 3.46 6.62A3.46 3.46 0 1 1 4.54 8.32C4.54 5.86 6.9 3.42 8 1.7Z"/>
            </svg>
        `,
        Fighter: `
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <rect x="10.3" y="4" width="3.4" height="16" rx="1.2" transform="rotate(-45 12 12)" fill="currentColor"/>
                <rect x="10.3" y="4" width="3.4" height="16" rx="1.2" transform="rotate(45 12 12)" fill="currentColor"/>
            </svg>
        `,
        Mage: `
            <svg viewBox="0 0 16 16" aria-hidden="true">
                <path fill="currentColor" d="M8 1.7 9.28 6.72 14.3 8l-5.02 1.28L8 14.3 6.72 9.28 1.7 8l5.02-1.28L8 1.7Z"/>
            </svg>
        `,
        Marksman: `
            <svg viewBox="0 0 16 16" aria-hidden="true">
                <path fill="currentColor" d="m4.05 11.95 1.1-3.1 2 2 4.52-4.52.95.95-4.52 4.52 1.99 1.99-3.09 1.11-3.22 1.1 1.12-3.05Z"/>
            </svg>
        `,
        Support: `
            <svg viewBox="0 0 16 16" aria-hidden="true">
                <path fill="currentColor" d="M6.75 2.75h2.5v4h4v2.5h-4v4h-2.5v-4h-4v-2.5h4v-4Z"/>
            </svg>
        `,
        Tank: `
            <svg viewBox="0 0 16 16" aria-hidden="true">
                <path fill="currentColor" d="M8 1.65 12.55 3v3.46c0 2.86-1.7 5.44-4.31 6.53L8 13.08l-.24-.09C5.15 11.9 3.45 9.32 3.45 6.46V3L8 1.65Zm0 1.52L4.95 4.08v2.38c0 2.16 1.23 4.11 3.05 5.06 1.82-.95 3.05-2.9 3.05-5.06V4.08L8 3.17Z"/>
            </svg>
        `,
    };
    // Chain-link glyph for the article copy-link button.
    const LINK_ICON = `
        <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
            <path fill="currentColor" d="M7.05 8.95a3.06 3.06 0 0 0 4.33 0l2.12-2.12a3.06 3.06 0 1 0-4.33-4.33L8.1 3.57a.75.75 0 1 0 1.06 1.06l1.07-1.07a1.56 1.56 0 1 1 2.21 2.21l-2.12 2.12a1.56 1.56 0 0 1-2.21 0 .75.75 0 0 0-1.06 1.06Zm1.9-1.9a3.06 3.06 0 0 0-4.33 0L2.5 9.17a3.06 3.06 0 1 0 4.33 4.33l1.07-1.07a.75.75 0 1 0-1.06-1.06l-1.07 1.07a1.56 1.56 0 1 1-2.21-2.21l2.12-2.12a1.56 1.56 0 0 1 2.21 0 .75.75 0 0 0 1.06-1.06Z"/>
        </svg>
    `;
    const BASE_TITLE = document.title;
    const HEADER_TITLE_ZH = "arammeta";
    const HEADER_TITLE_EN = "arammeta";
    const SHORT_PATCH_ZH = "26.13";
    const DATE_STR_ZH = "更新於 2026-07-15";
    const BUILD_DATE = "2026-07-15";
    const PATCH_LABEL = "patch 26.13";
    const TOTAL_GAMES = "553,231";
    const LANG_KEY = 'aram-mayhem-site-lang';
    const THEME_KEY = 'aram-mayhem-site-theme';
    // Primary tabs: home (英雄) / augments / draft / game / changes / column.
    const VIEWS = ['home', 'augments', 'draft', 'game', 'changes', 'column'];
    // Column articles.  Bilingual; `body_*` is trusted HTML, everything else is
    // escaped at render time.  Add new entries here — newest first.
    const ARTICLES = [
        {
            id: 'sprees-not-snowball',
            date: '2026-07-07',
            kicker_zh: '功率曲線', kicker_en: 'Power curve',
            cover_motif: 'diverge', cover_accent: '#e2574b',
            cover_zh: '連殺|≠滾雪球', cover_en: 'SPREES|≠ SNOWBALL',
            title_zh: '你以為的滾雪球，不是滾雪球',
            title_en: 'Kill sprees do not equal snowballing',
            summary_zh: '連殺榜和「把比賽提早收掉」的相關性 ρ=−0.03，趨近於零。最快關門的是約瑞科和一排零連殺輔助；連殺王圖奇、劫反而贏得慢。',
            summary_en: 'Correlation between the kill-spree axis and actually closing games early: rho = −0.03, essentially zero. The fastest closers are Yorick and a row of zero-spree supports, while spree kings like Twitch and Zed win slow.',
            body_zh: `<p>上一篇〈英雄定位圖〉的 X 軸叫「滾雪球」——平均最大連殺與多殺的加權。當時我們埋了一句但書：「滾雪球量的是人頭爆發，不直接等於把比賽提早結束」。這次直接把這句話抓來驗證，結果但書才是主角。</p>
<p>先定義「收官速度」：</p>
<p style="text-align:center;font-size:15px;margin:16px 0"><b>收官速度 ＝ 該英雄勝局的中位時長 − 全體勝局中位（17.6 分）</b></p>
<p>負值代表贏得比平均快。如果連殺真的會滾成提早勝利，連殺軸和收官速度應該高度相關 —— 實測 <b>Spearman ρ = −0.03</b>（n=173），<b>兩者完全無關</b>。連殺榜量的是「個人擊殺高光」，收官是「全隊」的事，資料說這是兩個獨立的能力。</p>
<h2>誰真的把勝利收得快</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Yorick.png" alt="約瑞科"><div class="art-meta"><div class="nm">約瑞科</div><div class="sb">勝局中位 16.7 分 · 42% 在 16 分內收掉 · 16,413 場</div></div><span class="lf" style="color:#3aa0ff">−52<small>s</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Yasuo.png" alt="犽宿"><div class="art-meta"><div class="nm">犽宿</div><div class="sb">勝局中位 16.9 分 · 40% 在 16 分內 · 80,180 場</div></div><span class="lf" style="color:#3aa0ff">−40<small>s</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Jinx.png" alt="吉茵珂絲"><div class="art-meta"><div class="nm">吉茵珂絲</div><div class="sb">勝局中位 16.9 分 · 40% 在 16 分內 · 94,147 場</div></div><span class="lf" style="color:#3aa0ff">−38<small>s</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Gwen.png" alt="關"><div class="art-meta"><div class="nm">關</div><div class="sb">勝局中位 17.0 分 · 39% 在 16 分內 · 28,063 場</div></div><span class="lf" style="color:#3aa0ff">−34<small>s</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Sylas.png" alt="賽勒斯"><div class="art-meta"><div class="nm">賽勒斯</div><div class="sb">勝局中位 17.0 分 · 38% 在 16 分內 · 59,989 場</div></div><span class="lf" style="color:#3aa0ff">−33<small>s</small></span></div>
<p>榜首是<b>約瑞科</b> —— 連殺榜第 147 名。他推塔、不收人頭，但他的勝局比全體中位快了近一分鐘。在大亂鬥，把兵線和塔滾起來，比把人頭滾起來收得快。吉茵珂絲是榜上唯一的連殺大戶（第 20 名）：她是少數兩邊都佔的例外，不是規則。</p>
<h2>連殺王，卻贏得慢</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Twitch.png" alt="圖奇"><div class="art-meta"><div class="nm">圖奇</div><div class="sb">連殺榜 #21 · 勝局中位 18.1 分 · 82,953 場</div></div><span class="lf" style="color:#e2574b">+33<small>s</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Gangplank.png" alt="剛普朗克"><div class="art-meta"><div class="nm">剛普朗克</div><div class="sb">連殺榜 #9 · 勝局中位 17.9 分 · 70,797 場</div></div><span class="lf" style="color:#e2574b">+23<small>s</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Zed.png" alt="劫"><div class="art-meta"><div class="nm">劫</div><div class="sb">連殺榜 #18 · 勝局中位 17.9 分 · 78,616 場</div></div><span class="lf" style="color:#e2574b">+23<small>s</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Talon.png" alt="塔隆"><div class="art-meta"><div class="nm">塔隆</div><div class="sb">連殺榜 #22 · 勝局中位 17.9 分 · 40,747 場</div></div><span class="lf" style="color:#e2574b">+20<small>s</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Mel.png" alt="梅爾"><div class="art-meta"><div class="nm">梅爾</div><div class="sb">連殺榜 #3 · 勝局中位 17.8 分 · 85,538 場</div></div><span class="lf" style="color:#e2574b">+16<small>s</small></span></div>
<p>刺客和收割型射手農得出大連殺，但連殺沒有轉換成節奏 —— 他們的勝局反而拖得比平均長。最極端的是<b>薩科</b>：連殺不低（前 1/4），卻是全英雄倒數第 2 慢（+54s），而且等一下他還會在「最後期」榜再出現一次。</p>
<h2>零連殺、最快關門的輔助</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Taric.png" alt="塔里克"><div class="art-meta"><div class="nm">塔里克</div><div class="sb">連殺榜 #166／173 · 勝局中位 17.1 分 · 21,564 場</div></div><span class="lf" style="color:#3aa0ff">−30<small>s</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Sona.png" alt="索娜"><div class="art-meta"><div class="nm">索娜</div><div class="sb">連殺榜 #162 · 勝局中位 17.1 分 · 55,464 場</div></div><span class="lf" style="color:#3aa0ff">−28<small>s</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Ivern.png" alt="埃爾文"><div class="art-meta"><div class="nm">埃爾文</div><div class="sb">連殺榜 #171 · 勝局中位 17.1 分 · 20,796 場</div></div><span class="lf" style="color:#3aa0ff">−24<small>s</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Lulu.png" alt="露璐"><div class="art-meta"><div class="nm">露璐</div><div class="sb">連殺榜 #172 · 勝局中位 17.3 分 · 39,842 場</div></div><span class="lf" style="color:#3aa0ff">−15<small>s</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Janna.png" alt="珍娜"><div class="art-meta"><div class="nm">珍娜</div><div class="sb">連殺榜 #169 · 勝局中位 17.4 分 · 41,546 場</div></div><span class="lf" style="color:#3aa0ff">−12<small>s</small></span></div>
<p>這排是整篇的關鍵證據：塔里克、索娜、埃爾文的連殺全在倒數 12 名內，但他們的勝局收得比全體快 —— 因為<b>被輔助餵起來的隊伍會快速關門，連殺記在隊友頭上</b>。「滾雪球」是隊伍的事，連殺榜只記個人。</p>
<h2>那，真正的前期怪／後期怪是誰？</h2>
<p>比較乾淨的量法是把兩頭直接拉開：<b>左邊是 16 分內結束的局的勝率，右邊是拖過 22 分的局的勝率</b>（中間段刻意丟掉）。</p>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Ornn.png" alt="鄂爾"><div class="art-meta"><div class="nm">鄂爾</div><div class="sb">41.0% → 57.1% · 22,905 場</div></div><span class="lf" style="color:#3aa0ff">+16.1<small>pp</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Shaco.png" alt="薩科"><div class="art-meta"><div class="nm">薩科</div><div class="sb">38.0% → 51.0% · 40,300 場</div></div><span class="lf" style="color:#3aa0ff">+13.0<small>pp</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Twitch.png" alt="圖奇"><div class="art-meta"><div class="nm">圖奇</div><div class="sb">41.6% → 54.4% · 38,266 場</div></div><span class="lf" style="color:#3aa0ff">+12.8<small>pp</small></span></div>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Seraphine.png" alt="瑟菈紛"><div class="art-meta"><div class="nm">瑟菈紛</div><div class="sb">63.4% → 50.1% · 40,015 場</div></div><span class="lf" style="color:#e2574b">−13.3<small>pp</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Hwei.png" alt="赫威"><div class="art-meta"><div class="nm">赫威</div><div class="sb">61.8% → 49.3% · 27,456 場</div></div><span class="lf" style="color:#e2574b">−12.4<small>pp</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Sion.png" alt="賽恩"><div class="art-meta"><div class="nm">賽恩</div><div class="sb">63.6% → 51.9% · 31,327 場</div></div><span class="lf" style="color:#e2574b">−11.7<small>pp</small></span></div>
<p><b>鄂爾</b>是全場最大爬坡：他的神器升級等於白送隊友上千金的裝備屬性，快局 41% 慘輸、拖過 22 分變 57.1%，而且他同時是全場第 4 慢收官（+48s）—— 標準的「拖到後期才贏」。經典模式的大型資料集（8M 場、鑽石以上）也把鄂爾標成平手局爬坡最猛的英雄之一，兩個完全不同的資料集對到同一隻。<b>瑟菈紛</b>則是最大前期怪：16 分內結束的局她贏將近三分之二（63.4%），拖過 22 分就掉回五五波。而<b>薩科、圖奇</b>在這張榜再次出現 —— 連殺農得多、贏得慢、越後期越強，三個訊號指向同一個結論：他們的連殺是「拖出來的」，不是「滾出來的」。</p>
<h2>一個誠實的但書</h2>
<p>收官速度的效果量<b>不大</b>：全英雄的勝局中位只在全體中位 ±1 分鐘內晃 —— 時長是 10 個人共同決定的，單一英雄的影響天生會被稀釋。它也是<b>描述性</b>統計：長局不一定是他「拖」出來的，可能只是勢均力敵的局才拖得長。另外，前後期傾向會隨版本窗口變：卡薩丁上個窗口讀起來近乎平坦，這個窗口是 −3.3pp 的輕微前期傾向 —— 整條軸跨版本很穩（r≈0.86–0.93），但中期尖峰型的個別英雄要小心讀。</p>
<p>資料：本機 games.db · queue 2400（Mayhem）· 版本 16.11–16.13 · 951,931 場（收集器邊跑邊寫，兩次掃描差 38 場）· 每英雄勝、敗局各 ≥500 場 · 收官速度＝勝局中位時長 − 全體勝局中位 17.6 分 · 連殺軸 vs 收官速度 Spearman ρ=−0.03 · 前後期＝WR(≥22 分) − WR(≤16 分)。</p>`,
            body_en: `<p>The previous champion-map article used an X axis called "snowball" — a weighted average of largest killing spree and multi-kill. We buried a caveat back then: "snowball measures kill bursts, it does not directly mean ending games early." This time we put that sentence on trial, and the caveat turned out to be the whole story.</p>
<p>First, define win tempo:</p>
<p style="text-align:center;font-size:15px;margin:16px 0"><b>Win tempo = median duration of that champion's WINS − global median win (17.6 min)</b></p>
<p>Negative = wins faster than average. If sprees really rolled into early wins, the spree axis and win tempo should correlate strongly. Measured: <b>Spearman rho = −0.03</b> (n=173) — <b>no relationship at all</b>. The spree axis measures personal kill highlights; closing a game is a team property. The data says these are two independent skills.</p>
<h2>Who actually closes games fast</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Yorick.png" alt="Yorick"><div class="art-meta"><div class="nm">Yorick</div><div class="sb">median win 16.7 min · 42% closed by 16 min · 16,413 games</div></div><span class="lf" style="color:#3aa0ff">−52<small>s</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Yasuo.png" alt="Yasuo"><div class="art-meta"><div class="nm">Yasuo</div><div class="sb">median win 16.9 min · 40% by 16 min · 80,180 games</div></div><span class="lf" style="color:#3aa0ff">−40<small>s</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Jinx.png" alt="Jinx"><div class="art-meta"><div class="nm">Jinx</div><div class="sb">median win 16.9 min · 40% by 16 min · 94,147 games</div></div><span class="lf" style="color:#3aa0ff">−38<small>s</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Gwen.png" alt="Gwen"><div class="art-meta"><div class="nm">Gwen</div><div class="sb">median win 17.0 min · 39% by 16 min · 28,063 games</div></div><span class="lf" style="color:#3aa0ff">−34<small>s</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Sylas.png" alt="Sylas"><div class="art-meta"><div class="nm">Sylas</div><div class="sb">median win 17.0 min · 38% by 16 min · 59,989 games</div></div><span class="lf" style="color:#3aa0ff">−33<small>s</small></span></div>
<p>The fastest closer is <b>Yorick</b> — spree rank 147. He pushes towers, not kill feeds, and his wins land almost a full minute under the global median. In ARAM, snowballing waves and towers closes games faster than snowballing kills. Jinx is the one big spree champ on the list (rank 20): she is the exception that holds both ends, not the rule.</p>
<h2>Spree kings who win slow</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Twitch.png" alt="Twitch"><div class="art-meta"><div class="nm">Twitch</div><div class="sb">spree rank #21 · median win 18.1 min · 82,953 games</div></div><span class="lf" style="color:#e2574b">+33<small>s</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Gangplank.png" alt="Gangplank"><div class="art-meta"><div class="nm">Gangplank</div><div class="sb">spree rank #9 · median win 17.9 min · 70,797 games</div></div><span class="lf" style="color:#e2574b">+23<small>s</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Zed.png" alt="Zed"><div class="art-meta"><div class="nm">Zed</div><div class="sb">spree rank #18 · median win 17.9 min · 78,616 games</div></div><span class="lf" style="color:#e2574b">+23<small>s</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Talon.png" alt="Talon"><div class="art-meta"><div class="nm">Talon</div><div class="sb">spree rank #22 · median win 17.9 min · 40,747 games</div></div><span class="lf" style="color:#e2574b">+20<small>s</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Mel.png" alt="Mel"><div class="art-meta"><div class="nm">Mel</div><div class="sb">spree rank #3 · median win 17.8 min · 85,538 games</div></div><span class="lf" style="color:#e2574b">+16<small>s</small></span></div>
<p>Assassins and cleanup carries farm huge sprees, but the sprees do not convert into tempo — their wins actually run longer than average. The extreme case is <b>Shaco</b>: top-quarter sprees, yet the 2nd-slowest closer of all champions (+54s) — and he is about to show up again on the late-game board below.</p>
<h2>Zero sprees, fastest closers: the supports</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Taric.png" alt="Taric"><div class="art-meta"><div class="nm">Taric</div><div class="sb">spree rank #166 of 173 · median win 17.1 min · 21,564 games</div></div><span class="lf" style="color:#3aa0ff">−30<small>s</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Sona.png" alt="Sona"><div class="art-meta"><div class="nm">Sona</div><div class="sb">spree rank #162 · median win 17.1 min · 55,464 games</div></div><span class="lf" style="color:#3aa0ff">−28<small>s</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Ivern.png" alt="Ivern"><div class="art-meta"><div class="nm">Ivern</div><div class="sb">spree rank #171 · median win 17.1 min · 20,796 games</div></div><span class="lf" style="color:#3aa0ff">−24<small>s</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Lulu.png" alt="Lulu"><div class="art-meta"><div class="nm">Lulu</div><div class="sb">spree rank #172 · median win 17.3 min · 39,842 games</div></div><span class="lf" style="color:#3aa0ff">−15<small>s</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Janna.png" alt="Janna"><div class="art-meta"><div class="nm">Janna</div><div class="sb">spree rank #169 · median win 17.4 min · 41,546 games</div></div><span class="lf" style="color:#3aa0ff">−12<small>s</small></span></div>
<p>This row is the key evidence: Taric, Sona and Ivern all sit in the bottom 12 of the spree board, yet their wins close faster than average — because <b>a team fed by its enchanter closes fast while the kills land on someone else's scoreboard</b>. Snowballing is a team property; the spree board only credits individuals.</p>
<h2>So who are the real early and late champions?</h2>
<p>The cleaner measure pulls the two ends apart directly: <b>the left number is the win rate in games that end by 16 minutes, the right is in games past 22 minutes</b> (the middle is dropped on purpose).</p>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Ornn.png" alt="Ornn"><div class="art-meta"><div class="nm">Ornn</div><div class="sb">41.0% → 57.1% · 22,905 games</div></div><span class="lf" style="color:#3aa0ff">+16.1<small>pp</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Shaco.png" alt="Shaco"><div class="art-meta"><div class="nm">Shaco</div><div class="sb">38.0% → 51.0% · 40,300 games</div></div><span class="lf" style="color:#3aa0ff">+13.0<small>pp</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Twitch.png" alt="Twitch"><div class="art-meta"><div class="nm">Twitch</div><div class="sb">41.6% → 54.4% · 38,266 games</div></div><span class="lf" style="color:#3aa0ff">+12.8<small>pp</small></span></div>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Seraphine.png" alt="Seraphine"><div class="art-meta"><div class="nm">Seraphine</div><div class="sb">63.4% → 50.1% · 40,015 games</div></div><span class="lf" style="color:#e2574b">−13.3<small>pp</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Hwei.png" alt="Hwei"><div class="art-meta"><div class="nm">Hwei</div><div class="sb">61.8% → 49.3% · 27,456 games</div></div><span class="lf" style="color:#e2574b">−12.4<small>pp</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Sion.png" alt="Sion"><div class="art-meta"><div class="nm">Sion</div><div class="sb">63.6% → 51.9% · 31,327 games</div></div><span class="lf" style="color:#e2574b">−11.7<small>pp</small></span></div>
<p><b>Ornn</b> is the biggest climber in the game: his Masterwork upgrades hand teammates a thousand gold of free item stats, so he loses short games hard (41%) and wins long ones (57.1%) — and he is also the 4th-slowest closer (+48s). A classic "wins by dragging it out" profile. The big Summoner's Rift dataset (8M games, Diamond+) also flags Ornn as one of the strongest even-state climbers — two completely different datasets landing on the same champion. <b>Seraphine</b> is the biggest early-game monster: she wins nearly two thirds (63.4%) of games that end by 16 minutes, then falls back to a coin flip past 22. And <b>Shaco and Twitch</b> reappear here — big sprees, slow wins, stronger the later it gets. Three signals, one conclusion: their sprees are farmed in long games, not rolled into fast ones.</p>
<h2>One honest caveat</h2>
<p>The win-tempo effect is <b>small</b>: every champion's median win sits within about ±1 minute of the global median — game length is decided by ten players, so any one champion's influence is diluted by construction. It is also <b>descriptive</b>: a long game is not necessarily one he dragged out; even matchups simply run longer. And the early/late tilt shifts with the patch window: Kassadin read as nearly flat in the previous window and −3.3pp mildly early in this one — the axis is very stable across patches overall (r≈0.86–0.93), but read individual mid-game-spike champions with care.</p>
<p>Data: local games.db · queue 2400 (Mayhem) · patches 16.11–16.13 · 951,931 games (collector writes while we scan; the two passes differ by 38 games) · ≥500 wins and ≥500 losses per champion · win tempo = median win duration − global median win 17.6 min · spree axis vs win tempo Spearman rho = −0.03 · early/late = WR(≥22 min) − WR(≤16 min).</p>`,
        },
        {
            id: 'skill-scaling',
            date: '2026-06-28',
            kicker_zh: '操作係數', kicker_en: 'Skill-scaling',
            cover_motif: 'diverge', cover_accent: '#3aa0ff',
            cover_zh: '吃操作|或屠低分', cover_en: 'SKILL|OR STOMP',
            title_zh: '操作係數：誰吃操作，誰專屠低分',
            title_en: 'Skill-scaling: who rewards skill, who farms low elo',
            summary_zh: '同一隻英雄，在高手局和菜雞局的勝率差多少？汎 +5.0pp、賽恩 −4.3pp —— 一張表看誰吃操作、誰只會屠低分。',
            summary_en: 'The same champion, split by lobby skill: Vayne climbs +5.0pp in strong lobbies, Sion drops −4.3pp. Who rewards good play, and who just farms weak games.',
            body_zh: `<p>大亂鬥隨機發英雄，又靠配對把勝率拉回五五波，所以「牌位」幾乎被磨平 —— 出裝大家抄一樣的、英雄又不能選。那 ARAM 還有沒有高低手之分？有，只是它不藏在「選什麼」，而藏在「同一隻英雄，你榨得出多少」。</p>
<p>我把每一場依玩家平均效率分成<b>高分局（前 25%）</b>與<b>低分局（後 25%）</b>，再看每隻英雄在這兩種局裡的勝率差。這個差就是<b>操作係數</b>：</p>
<p style="text-align:center;font-size:15px;margin:16px 0"><b>操作係數 ＝ 高分局勝率 − 低分局勝率</b></p>
<p>正值代表<b>吃操作</b> —— 高手手上更強；負值代表<b>低分強勢</b> —— 在弱局屠殺、遇到高手就現形。下面每一列，<b>箭頭左邊是低分局勝率、右邊是高分局勝率</b>。</p>
<h2>最吃操作（高手手上更強）</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Vayne.png" alt="汎"><div class="art-meta"><div class="nm">汎</div><div class="sb">55.0% → 60.0% · 23,659 場</div></div><span class="lf" style="color:#3aa0ff">+5.0<small>pp</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Lulu.png" alt="露璐"><div class="art-meta"><div class="nm">露璐</div><div class="sb">46.1% → 50.6% · 9,704 場</div></div><span class="lf" style="color:#3aa0ff">+4.4<small>pp</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Taliyah.png" alt="塔莉雅"><div class="art-meta"><div class="nm">塔莉雅</div><div class="sb">46.3% → 50.5% · 7,804 場</div></div><span class="lf" style="color:#3aa0ff">+4.2<small>pp</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Vladimir.png" alt="弗拉迪米爾"><div class="art-meta"><div class="nm">弗拉迪米爾</div><div class="sb">46.5% → 50.7% · 12,907 場</div></div><span class="lf" style="color:#3aa0ff">+4.2<small>pp</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Smolder.png" alt="史矛德"><div class="art-meta"><div class="nm">史矛德</div><div class="sb">45.6% → 49.8% · 23,583 場</div></div><span class="lf" style="color:#3aa0ff">+4.1<small>pp</small></span></div>
<div class="art-rank"><span class="rk">6</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Quinn.png" alt="葵恩"><div class="art-meta"><div class="nm">葵恩</div><div class="sb">43.8% → 48.1% · 6,628 場</div></div><span class="lf" style="color:#3aa0ff">+4.2<small>pp</small></span></div>
<h2>最低分強勢（專屠弱局）</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/MonkeyKing.png" alt="悟空"><div class="art-meta"><div class="nm">悟空</div><div class="sb">51.9% → 47.4% · 9,927 場</div></div><span class="lf" style="color:#e2574b">−4.5<small>pp</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Sion.png" alt="賽恩"><div class="art-meta"><div class="nm">賽恩</div><div class="sb">61.3% → 57.0% · 18,598 場</div></div><span class="lf" style="color:#e2574b">−4.3<small>pp</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Jax.png" alt="賈克斯"><div class="art-meta"><div class="nm">賈克斯</div><div class="sb">53.8% → 49.6% · 10,341 場</div></div><span class="lf" style="color:#e2574b">−4.2<small>pp</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Nasus.png" alt="納瑟斯"><div class="art-meta"><div class="nm">納瑟斯</div><div class="sb">53.0% → 49.2% · 13,004 場</div></div><span class="lf" style="color:#e2574b">−3.8<small>pp</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Swain.png" alt="斯溫"><div class="art-meta"><div class="nm">斯溫</div><div class="sb">49.5% → 45.9% · 20,924 場</div></div><span class="lf" style="color:#e2574b">−3.7<small>pp</small></span></div>
<div class="art-rank"><span class="rk">6</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Karthus.png" alt="卡爾瑟斯"><div class="art-meta"><div class="nm">卡爾瑟斯</div><div class="sb">52.3% → 48.8% · 24,173 場</div></div><span class="lf" style="color:#e2574b">−3.5<small>pp</small></span></div>
<h2>讀出來的故事</h2>
<p>排行幾乎照著老常識走：<b>吃操作</b>那端清一色是機制難、上限高的後排 carry（汎、史矛德、貝爾薇斯）跟要搭 carry 的<b>露璐</b> —— 高手才餵得動、站得穩。<b>低分強勢</b>那端則是好上手的前排戰坦（賽恩、納瑟斯、悟空）：點一點就有貢獻，在亂打的局裡屠殺，一旦對手會閃、會集火，就被針對。</p>
<p>賽恩是最戲劇化的例子：低分局他有 <b>61.3%</b> 勝率（全英雄數一數二），到高分局掉到 57.0% —— 還是強，但「躺贏」那一截被抽掉了。</p>
<h2>一個誠實的但書</h2>
<p>這個訊號是<b>真的</b>（跨版本穩定、統計顯著），但<b>不大</b> —— 最極端也就 ±5pp 上下。它能告訴你「這隻吃不吃操作」，卻<b>不能</b>反推某個玩家的牌位：大亂鬥勝負雜訊太大，個人分數會被拉回中段。把它當「選角／練哪隻」的參考剛剛好，別當天梯分。</p>
<p>資料：本機 games.db · queue 2400（Mayhem）· 版本 16.11–16.13 · 對局水平＝玩家「同英雄傷害／經濟效率」，高低分各取前後 25%，每英雄樣本 ≥800 場 · 操作係數跨版本相關 r≈0.5。</p>`,
            body_en: `<p>ARAM hands you random champions and matchmaking drags every win rate back toward 50% — so "rank" gets ground flat: everyone copies the same build, and you don't pick your champ. Is there still a skill gap? Yes — it doesn't live in <i>what you pick</i>, but in <i>how much you squeeze out of the same champion</i>.</p>
<p>I split every game by average player efficiency into a <b>high-skill half (top 25%)</b> and a <b>low-skill half (bottom 25%)</b>, then measured each champion's win-rate gap between them. That gap is the <b>skill-scaling</b> coefficient:</p>
<p style="text-align:center;font-size:15px;margin:16px 0"><b>Skill-scaling = WR(high-skill lobbies) − WR(low-skill lobbies)</b></p>
<p>Positive = <b>rewards skill</b> (stronger in good hands); negative = <b>stomps low elo</b> (farms weak games, exposed against good players). In every row below, <b>the left number is the low-skill-lobby win rate, the right is the high-skill-lobby win rate</b>.</p>
<h2>Rewards skill the most</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Vayne.png" alt="Vayne"><div class="art-meta"><div class="nm">Vayne</div><div class="sb">55.0% → 60.0% · 23,659 games</div></div><span class="lf" style="color:#3aa0ff">+5.0<small>pp</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Lulu.png" alt="Lulu"><div class="art-meta"><div class="nm">Lulu</div><div class="sb">46.1% → 50.6% · 9,704 games</div></div><span class="lf" style="color:#3aa0ff">+4.4<small>pp</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Taliyah.png" alt="Taliyah"><div class="art-meta"><div class="nm">Taliyah</div><div class="sb">46.3% → 50.5% · 7,804 games</div></div><span class="lf" style="color:#3aa0ff">+4.2<small>pp</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Vladimir.png" alt="Vladimir"><div class="art-meta"><div class="nm">Vladimir</div><div class="sb">46.5% → 50.7% · 12,907 games</div></div><span class="lf" style="color:#3aa0ff">+4.2<small>pp</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Smolder.png" alt="Smolder"><div class="art-meta"><div class="nm">Smolder</div><div class="sb">45.6% → 49.8% · 23,583 games</div></div><span class="lf" style="color:#3aa0ff">+4.1<small>pp</small></span></div>
<div class="art-rank"><span class="rk">6</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Quinn.png" alt="Quinn"><div class="art-meta"><div class="nm">Quinn</div><div class="sb">43.8% → 48.1% · 6,628 games</div></div><span class="lf" style="color:#3aa0ff">+4.2<small>pp</small></span></div>
<h2>Farms low elo the most</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/MonkeyKing.png" alt="Wukong"><div class="art-meta"><div class="nm">Wukong</div><div class="sb">51.9% → 47.4% · 9,927 games</div></div><span class="lf" style="color:#e2574b">−4.5<small>pp</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Sion.png" alt="Sion"><div class="art-meta"><div class="nm">Sion</div><div class="sb">61.3% → 57.0% · 18,598 games</div></div><span class="lf" style="color:#e2574b">−4.3<small>pp</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Jax.png" alt="Jax"><div class="art-meta"><div class="nm">Jax</div><div class="sb">53.8% → 49.6% · 10,341 games</div></div><span class="lf" style="color:#e2574b">−4.2<small>pp</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Nasus.png" alt="Nasus"><div class="art-meta"><div class="nm">Nasus</div><div class="sb">53.0% → 49.2% · 13,004 games</div></div><span class="lf" style="color:#e2574b">−3.8<small>pp</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Swain.png" alt="Swain"><div class="art-meta"><div class="nm">Swain</div><div class="sb">49.5% → 45.9% · 20,924 games</div></div><span class="lf" style="color:#e2574b">−3.7<small>pp</small></span></div>
<div class="art-rank"><span class="rk">6</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Karthus.png" alt="Karthus"><div class="art-meta"><div class="nm">Karthus</div><div class="sb">52.3% → 48.8% · 24,173 games</div></div><span class="lf" style="color:#e2574b">−3.5<small>pp</small></span></div>
<h2>What it says</h2>
<p>The ranking tracks common sense: the <b>rewards-skill</b> end is mechanically demanding, high-ceiling carries (Vayne, Smolder, Bel'Veth) plus the carry-dependent <b>Lulu</b> — they only pay off in good hands. The <b>low-elo</b> end is simple frontline bruisers (Sion, Nasus, Wukong): point-and-click value that stomps messy games, but gets exposed once enemies flash and focus-fire.</p>
<p>Sion is the most dramatic case: <b>61.3%</b> win rate in low-skill lobbies (one of the highest of any champion), falling to 57.0% in high-skill ones — still strong, just with the free-win cushion stripped away.</p>
<h2>One honest caveat</h2>
<p>The signal is <b>real</b> (patch-stable, statistically significant) but <b>small</b> — at most ±5pp. It tells you whether a champion rewards skill; it <b>cannot</b> be flipped around to read a player's rank, because ARAM outcomes are too noisy and individual scores regress to the middle. Use it to pick a champ or decide what to practice — not as a ladder rating.</p>
<p>Data: local games.db · queue 2400 (Mayhem) · patches 16.11–16.13 · lobby skill = players' champ-controlled damage/gold efficiency, top vs bottom 25%, ≥800 games per champion · skill-scaling cross-patch correlation r≈0.5.</p>`,
        },
        {
            id: 'scaling-snowball',
            date: '2026-06-28',
            kicker_zh: '英雄地圖', kicker_en: 'Champion map',
            cover_motif: 'scatter', cover_accent: '#48b868',
            cover_zh: '英雄定位圖', cover_en: 'CHAMPION|MAP',
            title_zh: '後期 × 滾雪球：英雄定位圖',
            title_en: 'Late game × snowball: a champion map',
            summary_zh: '把全英雄畫在「能不能滾雪球」與「能不能撐到後期」兩條軸上，一眼看出誰是速攻、誰是發育、誰是全能。',
            summary_en: 'Every champion plotted on two axes — snowball potential and late-game strength — so you can see who rushes, who scales, and who does both.',
            body_zh: `<p>每隻英雄在 Mayhem 都有兩種「怎麼贏」的傾向：能不能<b>滾雪球</b>（靠人頭擴大優勢），以及能不能撐到<b>後期</b>。這張圖把全英雄同時畫在這兩條軸上。</p>
<div class="scatter-wrap"><div class="scatter-chart" id="scatter-host"></div>
<div class="scatter-legend"><span><span class="dot" style="background:#d64545"></span>低勝率</span><span><span class="dot" style="background:#d0b23a"></span>中</span><span><span class="dot" style="background:#48b868"></span>高勝率</span><span style="color:var(--text-dim)">· 滑鼠移到頭像看詳細數據</span></div></div>
<h2>怎麼讀</h2>
<ul><li><b>X 軸 滾雪球</b>＝0.6×平均最大連殺 ＋ 0.4×平均最大多殺。越右邊代表越會打出大連殺、滾成肥 carry。</li>
<li><b>Y 軸 後期</b>＝比賽 ≥22 分鐘的勝率 − ≤16 分鐘的勝率。越上面代表局拖越久越強。</li>
<li><b>外框顏色</b>＝該英雄平均勝率（紅→黃→綠）。</li>
<li>虛線是全英雄<b>中位數</b>，把圖切成四象限。</li></ul>
<h2>四象限</h2>
<ul><li><b>右上（滾雪球強・後期強）</b>：全能型，前中後期都能打。</li>
<li><b>左上（後期強）</b>：發育型，前期低調、愈拖愈強。</li>
<li><b>右下（滾雪球強・後期弱）</b>：速攻型，要趁早靠人頭結束，拖久會虛。</li>
<li><b>左下（兩者皆弱）</b>：較吃節奏，仰賴隊友或特定增幅。</li></ul>
<p>一個常見誤解：滾雪球量的是「人頭爆發／收割能力」，<b>不直接等於把比賽提早結束</b>——它只反映擊殺面的擴張力，與局長是兩件事。本資料中位數時長約 17.6 分鐘。</p>
<p>資料：本機 games.db · queue 2400（Mayhem）· 後期／滾雪球取自 <code>champ-empirical-axes.json</code>，每英雄樣本 ≥400 場。</p>`,
            body_en: `<p>In Mayhem every champion leans two ways to win: how well they <b>snowball</b> (turn kills into a lead) and how well they <b>scale into the late game</b>. This chart plots all champions on both axes at once.</p>
<div class="scatter-wrap"><div class="scatter-chart" id="scatter-host"></div>
<div class="scatter-legend"><span><span class="dot" style="background:#d64545"></span>Low WR</span><span><span class="dot" style="background:#d0b23a"></span>Mid</span><span><span class="dot" style="background:#48b868"></span>High WR</span><span style="color:var(--text-dim)">· hover an icon for details</span></div></div>
<h2>How to read it</h2>
<ul><li><b>X axis — snowball</b> = 0.6×avg largest killing spree + 0.4×avg largest multi-kill. Further right = bigger kill streaks, snowballs into a fed carry.</li>
<li><b>Y axis — late game</b> = WR(games ≥22 min) − WR(games ≤16 min). Higher = stronger the longer the game runs.</li>
<li><b>Ring color</b> = the champion's average win rate (red → yellow → green).</li>
<li>The dashed lines are the all-champion <b>medians</b>, splitting the map into four quadrants.</li></ul>
<h2>The four quadrants</h2>
<ul><li><b>Top-right (snowball + late)</b>: all-rounders, strong at every stage.</li>
<li><b>Top-left (late)</b>: scalers, quiet early and stronger the longer it goes.</li>
<li><b>Bottom-right (snowball, weak late)</b>: rushers — close it out early on kills, fade if it drags.</li>
<li><b>Bottom-left (neither)</b>: tempo-dependent, lean on teammates or specific augments.</li></ul>
<p>One common misread: snowball measures kill burst / cleanup potential, it does <b>not</b> directly mean ending the game early — it only reflects kill-side expansion, which is separate from game length. The median game here is about 17.6 minutes.</p>
<p>Data: local games.db · queue 2400 (Mayhem) · late/snowball from <code>champ-empirical-axes.json</code>, ≥400 games per champion.</p>`,
        },
        {
            id: 'draw-your-sword',
            date: '2026-06-28',
            kicker_zh: '增幅深入', kicker_en: 'Augment deep-dive',
            cover_motif: 'blade', cover_accent: '#ff6a3d',
            cover_zh: '拔劍吧', cover_en: 'DRAW YOUR|SWORD',
            cover_image_zh: 'assets/covers/draw-your-sword-zh.webp',
            cover_image_en: 'assets/covers/draw-your-sword-en.webp',
            title_zh: '拔劍吧：誰用最強，怎麼出裝',
            title_en: 'Draw Your Sword: who abuses it, and how to build',
            summary_zh: '依 lift（已扣除英雄本身強度）排名，煞蜜拉以 +20.7pp 居首；附實測最佳出裝與「雙開不拖」的證據。',
            summary_en: 'Ranked by lift (champion strength removed): Samira tops at +20.7pp, plus the data-backed core build.',
            body_zh: `<p><b>拔劍吧</b>（Draw Your Sword）：附近沒有敵人時蓄力，下次攻擊向前突進斬擊、造成額外傷害。本質是「進場 ＋ 收割」的爆發型增幅。</p>
<p>下面依 <b>lift</b> 排名 —— 也就是「裝了這個增幅後，勝率比該英雄平常高多少」，已扣除英雄本身的強度，所以衡量的是「這個增幅成就了誰」，而不是誰本來就強。</p>
<h2>最強使用者（依 lift）</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Samira.png" alt="煞蜜拉"><div class="art-meta"><div class="nm">煞蜜拉</div><div class="sb">46.4% → 67.1% · 2,171 場</div></div><span class="lf">+20.7<small>pp</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Aphelios.png" alt="亞菲利歐"><div class="art-meta"><div class="nm">亞菲利歐</div><div class="sb">52.3% → 67.9% · 589 場</div></div><span class="lf">+15.6<small>pp</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Graves.png" alt="葛雷夫"><div class="art-meta"><div class="nm">葛雷夫</div><div class="sb">50.7% → 65.2% · 5,283 場</div></div><span class="lf">+14.6<small>pp</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Caitlyn.png" alt="凱特琳"><div class="art-meta"><div class="nm">凱特琳</div><div class="sb">52.5% → 67.1% · 1,330 場</div></div><span class="lf">+14.5<small>pp</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Varus.png" alt="法洛士"><div class="art-meta"><div class="nm">法洛士</div><div class="sb">48.6% → 62.0% · 1,044 場</div></div><span class="lf">+13.3<small>pp</small></span></div>
<p>煞蜜拉登頂的原因很直覺：她本體勝率只有 46%、偏弱，痛點正是「進不去」；拔劍吧的突進剛好補足進場，於是勝率直接噴到 67%。這個增幅不是錦上添花，而是補她的命門。</p>
<h2>推薦核心出裝</h2>
<p>全都是實測正向的單品，依建議順序：</p>
<div class="art-build"><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/6676.png" alt="蒐集者"><div class="it-nm">蒐集者</div><div class="it-tag">①核心</div></div><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/3031.png" alt="無盡之刃"><div class="it-nm">無盡之刃</div><div class="it-tag">②核心</div></div><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/3008.png" alt="貪婪護脛"><div class="it-nm">貪婪護脛</div><div class="it-tag">③鞋</div></div><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/6697.png" alt="傲慢"><div class="it-nm">傲慢</div><div class="it-tag">④滾雪球</div></div><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/3036.png" alt="多明尼克的問候"><div class="it-nm">多明尼克</div><div class="it-tag">⑤破甲</div></div><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/6333.png" alt="死亡之舞"><div class="it-nm">死亡之舞</div><div class="it-tag">⑥防爆</div></div></div>
<ul><li><b>① 蒐集者</b> — 最便宜、斬殺 ＋ 暴擊，最快成戰力</li>
<li><b>② 無盡之刃</b> — 暴擊乘區核心</li>
<li><b>③ 貪婪護脛</b> — 補移速 ＋ 擊殺疊吸血（被風箏兇可提前）</li>
<li><b>④ 傲慢</b> — 靠擊殺滾 AD，越早越強</li>
<li><b>⑤ 多明尼克的問候</b> — 對面有坦克／高護甲再提前</li>
<li><b>⑥ 死亡之舞</b> — 後期防爆收尾</li></ul>
<h2>三個關鍵心得</h2>
<ul><li><b>蒐集者 ＋ 傲慢雙開不拖節奏。</b>四象限實測雙開 67.4% 是最高，平均時長只比單開多約 30 秒 —— 兩件都是中價位即戰力，戰力曲線連續上升，沒有空窗。</li>
<li><b>不要疊兩件吸血。</b>嗜血者（61.9%）與無盡飢渴（60.9%）在骨架上都明顯低於基準；爆發流要的是傷害，不是續航。</li>
<li><b>鞋子要出，首選貪婪護脛（67.2%）。</b>「不出鞋勝率較高」是反向因果（贏太快來不及買第六件），不是策略；對面 AP／控制多時改水星之靴。</li></ul>
<p><b>lift</b> ＝ 裝此增幅後勝率 − 該英雄不帶此增幅的勝率（pp）。資料：本機 games.db · queue 2400（Mayhem）· patch 16.12 · 每英雄帶此增幅 ≥150 場。</p>`,
            body_en: `<p><b>Draw Your Sword</b>: when no enemy is nearby you charge up, and your next attack dashes forward with a slash for bonus damage. It is fundamentally an engage-and-cleanup burst augment.</p>
<p>The ranking below is by <b>lift</b> — how much higher a champion's win rate is with this augment than their usual baseline, with the champion's own strength removed. So it measures who the augment <b>elevates</b>, not who was already strong.</p>
<h2>Top users (by lift)</h2>
<div class="art-rank"><span class="rk">1</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Samira.png" alt="Samira"><div class="art-meta"><div class="nm">Samira</div><div class="sb">46.4% → 67.1% · 2,171 games</div></div><span class="lf">+20.7<small>pp</small></span></div>
<div class="art-rank"><span class="rk">2</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Aphelios.png" alt="Aphelios"><div class="art-meta"><div class="nm">Aphelios</div><div class="sb">52.3% → 67.9% · 589 games</div></div><span class="lf">+15.6<small>pp</small></span></div>
<div class="art-rank"><span class="rk">3</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Graves.png" alt="Graves"><div class="art-meta"><div class="nm">Graves</div><div class="sb">50.7% → 65.2% · 5,283 games</div></div><span class="lf">+14.6<small>pp</small></span></div>
<div class="art-rank"><span class="rk">4</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Caitlyn.png" alt="Caitlyn"><div class="art-meta"><div class="nm">Caitlyn</div><div class="sb">52.5% → 67.1% · 1,330 games</div></div><span class="lf">+14.5<small>pp</small></span></div>
<div class="art-rank"><span class="rk">5</span><img class="art-face" src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/champion/Varus.png" alt="Varus"><div class="art-meta"><div class="nm">Varus</div><div class="sb">48.6% → 62.0% · 1,044 games</div></div><span class="lf">+13.3<small>pp</small></span></div>
<p>Samira tops the list for an intuitive reason: her baseline is only 46% — on the weak side — and her core problem is reaching the fight. The dash fixes exactly that, and her win rate jumps to 67%. The augment patches her weakness rather than padding a strength.</p>
<h2>Recommended core build</h2>
<p>Every item below tests positive; in suggested order:</p>
<div class="art-build"><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/6676.png" alt="The Collector"><div class="it-nm">Collector</div><div class="it-tag">① core</div></div><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/3031.png" alt="Infinity Edge"><div class="it-nm">Infinity Edge</div><div class="it-tag">② core</div></div><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/3008.png" alt="Gluttonous Greaves"><div class="it-nm">Greaves</div><div class="it-tag">③ boots</div></div><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/6697.png" alt="Hubris"><div class="it-nm">Hubris</div><div class="it-tag">④ snowball</div></div><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/3036.png" alt="Lord Dominik's Regards"><div class="it-nm">Dominik's</div><div class="it-tag">⑤ armor pen</div></div><div class="art-item"><img src="https://ddragon.leagueoflegends.com/cdn/16.13.1/img/item/6333.png" alt="Death's Dance"><div class="it-nm">Death's Dance</div><div class="it-tag">⑥ anti-burst</div></div></div>
<ul><li><b>① The Collector</b> — cheapest, execute + crit, fastest power spike</li>
<li><b>② Infinity Edge</b> — the crit-multiplier core</li>
<li><b>③ Gluttonous Greaves</b> — movement + kill-stacked omnivamp (rush earlier if kited hard)</li>
<li><b>④ Hubris</b> — stacks AD off kills; the earlier the better</li>
<li><b>⑤ Lord Dominik's Regards</b> — pull earlier vs tanks / heavy armor</li>
<li><b>⑥ Death's Dance</b> — late-game anti-burst to close it out</li></ul>
<h2>Three key takeaways</h2>
<ul><li><b>Collector + Hubris together does not slow you down.</b> In the four-quadrant test, running both is the highest at 67.4%, and games are only ~30s longer than running one — both are mid-cost, immediately useful items, so the power curve climbs without a gap.</li>
<li><b>Do not stack two lifesteal items.</b> Bloodthirster (61.9%) and the omnivamp lifesteal option (60.9%) both sit clearly below baseline on the skeleton; a burst build wants damage, not sustain.</li>
<li><b>Buy boots — Gluttonous Greaves first (67.2%).</b> "No boots wins more" is reverse causation (you snowball too fast to afford a sixth item), not a strategy; swap to Mercury's Treads vs heavy AP / CC.</li></ul>
<p><b>lift</b> = win rate with this augment − the champion's win rate without it (pp). Data: local games.db · queue 2400 (Mayhem) · patch 16.12 · ≥150 games per champion with this augment.</p>`,
        },
        {
            id: 'how-to-read',
            date: '2026-06-27',
            kicker_zh: '入門', kicker_en: 'Basics',
            cover_motif: 'tiers', cover_accent: '#f5c518',
            cover_zh: '先看懂|這三欄', cover_en: 'READ|THIS FIRST',
            title_zh: '如何閱讀這份 Tier List',
            title_en: 'How to read this tier list',
            summary_zh: '勝率、貝氏修正、樣本數 — 三個你該先看懂的欄位，別被小樣本誤導。',
            summary_en: 'Win rate, Bayesian shrinkage, sample size — the three columns to read first so small samples do not fool you.',
            body_zh: `<p>這份 tier list 把英雄依「貝氏修正後的勝率」分級，而不是單純的原始勝率。原因很簡單：小樣本的原始勝率非常不穩。</p>
<h2>三個關鍵欄位</h2>
<ul><li><b>勝率</b>：經貝氏收縮後的估計值，樣本越少越會被拉回整體基準。</li>
<li><b>場次</b>：樣本數，決定你該多相信這個數字。</li>
<li><b>原始勝率</b>：未修正的真實勝率，和上面對照看落差。</li></ul>
<p>當原始勝率很高、但場次很低時，修正後的勝率會明顯被往下壓 — 這是刻意的保守，避免你追到只打了幾場的假強勢。</p>`,
            body_en: `<p>This tier list ranks champions by a <b>Bayesian-shrunk win rate</b>, not the raw win rate. The reason is simple: raw win rate is very noisy on small samples.</p>
<h2>Three columns that matter</h2>
<ul><li><b>Win rate</b>: the shrunk estimate; the fewer the games, the more it is pulled back toward the overall baseline.</li>
<li><b>Games</b>: the sample size — how much to trust the number.</li>
<li><b>Raw win rate</b>: the unadjusted figure; compare it against the shrunk one.</li></ul>
<p>When the raw win rate is high but games are few, the shrunk win rate is pushed down noticeably — a deliberate caution that keeps you from chasing a few-game mirage.</p>`,
        },
    ];
    let columnArticle = null;
    const SET_RESIDUAL_THRESHOLD = 0.02;
    function trackEvent(name, params = {}) {
        if (typeof gtag === 'function') {
            gtag('event', name, params);
        }
    }
    const COPY = {
        zh: {
            htmlLang: 'zh-Hant',
            subtitle: () => (SHORT_PATCH_ZH === 'all patches' ? '全版本' : `版本 ${SHORT_PATCH_ZH}`),
            searchPlaceholderDesktop: '搜尋英雄（中 / 英）   Ctrl+F',
            searchPlaceholderMobile: '搜尋英雄（中 / 英）',
            searchAria: '搜尋英雄',
            shownUnit: '隻',
            tierUnit: '隻',
            updatesButton: '近期更新',
            updatesKicker: '本版重點',
            updatesTitle: '近期重要更新',
            updatesClose: '關閉近期更新',
            updatesItems: [
                '2026-06-29：增幅榜的選用率改用「每場平均出現次數」，同一場多名玩家選同一個增幅會累加，更貼近真實熱門度（最熱門可超過 100%）。同時把熱力配色改成灰→藍→黃→橙→紅，5 段更好分辨。',
                '2026-06-17：出裝頁的選取率數字（單件／前兩件／核心／搭配裝備／鞋子）也套用與增幅裝置相同的熱門程度配色。',
                '2026-06-17：增幅裝置卡片下方灰字精簡為只顯示選用率，並依熱門程度上色（冷色＝少人選，金色＝熱門）。',
                '2026-05-25：英雄詳情新增「單件裝備強度」，六格中出過就計入，幫你選第三到第六件。',
                '2026-05-25：前兩件出裝與單件裝備改成右滑 carousel，手機一次看更多裝備。',
                '2026-05-25：增幅裝置改成同彩度一路由強排到弱，不再拆成兩個區塊。',
            ],
            recModeOn: '選擇你的隊友：開',
            recModeOff: '選擇你的隊友：關',
            emptyTitle: '沒有符合條件的英雄',
            emptyCopy: '換個角色篩選，或試試英雄中／英文名。',
            freshness: () => `${DATE_STR_ZH}（${TOTAL_GAMES} 場） · ${PATCH_LABEL}`,
            sideTitle: '推薦組合排行',
            sideSub: '選 1～4 隻看誰適合補進來；選滿 5 隻改看整隊估計勝率與能力維度。',
            sideTitleFull: '五人陣容評估',
            sideSubFull: '依英雄基準勝率、兩兩搭配 lift 與陣容組成估計；維度為隊內平均百分位。',
            closeRecs: '關閉推薦組合',
            openRecs: n => n >= 5 ? `看陣容評估 (${n})` : `看推薦組合 (${n})`,
            teamEstWr: '估計勝率',
            teamBaseWr: '英雄均值',
            teamPairLift: '搭配 lift',
            teamCompAdj: '陣容搭配',
            teamPairCover: (have, total) => `已知搭配 ${have}/${total} 組`,
            teamDimsTitle: '隊伍能力維度',
            teamCompTitle: '陣容組成',
            teamDimBad: '偏弱',
            teamDimMid: '普通',
            teamDimGood: '充足',
            teamConfHigh: '可信度高',
            teamConfMid: '可信度中',
            teamConfLow: '樣本偏早',
            teamPickFullNote: '已選滿 5 隻 · 以下為整隊評估（非再推薦第 6 人）',
            teamTempoTitle: '發力節奏',
            teamTempoEarly: '前期',
            teamTempoLate: '後期',
            teamTempoEarlyLean: '偏前期發力',
            teamTempoLateLean: '偏後期發力',
            teamTempoBalanced: '前後期均衡',
            teamTempoTip: '前期＝短局（≤16 分）勝率均值；後期＝長局（≥22 分）勝率均值。數字是勝率%，前後期本身無好壞。',
            langToggleLabel: 'EN',
            langToggleTitle: 'Switch to English',
            langToggleAria: '切換語言',
            themeToLightTitle: '切換淺色',
            themeToLightAria: '切換成淺色主題',
            themeToDarkTitle: '切換深色',
            themeToDarkAria: '切換成深色主題',
            removePick: name => `移除 ${name}`,
            pickEmpty: '英雄',
            maxOnly: n => `最多只能選 ${n} 隻英雄。`,
            pickNoteEmpty: n => `最多選 ${n} 隻；未滿 5 隻時看推薦補位，選滿後看整隊勝率與維度。`,
            pickNotePartial: want => `目前這組選角的完整資料較少，先用已知搭配排序。`,
            pickNoteReady: (want, minGames) => `已選 ${want}/${MAX_TEAM_PICKS} 隻；pair 門檻 >= ${minGames} 場。`,
            panelEmpty: '到 Draft 分頁選 1～5 隻我方英雄；可再選對手。未滿 5 隻時排出補位推薦；有對手時顯示對陣估計勝率與雙方隊伍特性。',
            panelNoData: '這組英雄目前沒有足夠的 pair 資料。',
            draftEmpty: '連續點選英雄：先填我方 5 隻，滿了會自動接續選對手。',
            draftAllyEval: '我方陣容',
            draftEnemyEval: '對手陣容',
            draftVs: 'VS',
            draftCompareTitle: '對陣比較',
            draftRadarTitle: '能力雷達（雙隊疊加）',
            draftCompareExtras: '項目比較',
            draftLegendAlly: '我方',
            draftLegendEnemy: '對手',
            draftChampStrength: '英雄強度',
            draftMetricFinal: '最終勝率',
            draftMetricFinalNote: 'AI推估勝率：綜合參考 1. 英雄強度 2. 搭配 3. 對戰組合。備註：勝率主要受英雄強度影響',
            draftMetricPartial: '完成我方與對方各 5 隻英雄後計算',
            draftMetricUnavailable: '預測模型尚未載入或含未知英雄',
            draftOnOtherSide: '這隻已在另一邊陣容裡。',
            gamePickCount: (n, max) => `已選 ${n}/${max}`,
            gameLock: '鎖定',
            gamePlayAgain: '再來一局',
            gameMaxOnly: '最多選 5 隻。',
            gameNeedFive: '請先選滿 5 隻再鎖定。',
            gameMissTitle: '未答對',
            gameMissNeed: k => `還差 ${k} 隻正解英雄`,
            gameMissScore: (hit, total) => `答對：${hit}/${total}`,
            gameHint: '提示',
            gameReveal: '看答案',
            gameHintUsed: name => `提示：${name}`,
            gameHintAuto: name => `提示：${name}`,
            gamePerfect: '完全正確！',
            gameYourWr: '你的估計勝率',
            gameOptimalWr: '最佳陣容勝率',
            gameYourTeam: '你的選擇',
            gameBestTeam: '最佳 5 人',
            gameDelta: '差距',
            gameGrade: '評等',
            gameRank: '排名',
            gamePr: 'PR',
            gameRankOf: (rank, total) => `#${rank} / ${total}`,
            gamePrValue: pr => `PR ${pr}`,
            gameHintBadge: '提示',
            gameOptimalBadge: '最佳',
            gameTagBoth: '選中·正解',
            gameTagYours: '選中',
            gameTagBest: '正解',
            gameTagMiss: '未選',
            gameLegendPicked: '選取（白色圓圈）',
            gameLegendCorrect: '最佳（黃字）',
            gameLegendNeither: '未選（灰階）',
            gameCompareTitle: '陣容對照',
            gamePoolReview: '英雄池',
            gameSoloWrList: '英雄池',
            gameTabPool: '英雄池',
            gameTabAnalysis: '隊伍分析',
            gameAnalysisBest: '最佳 5 人',
            gameAnalysisYours: '你的選擇',
            gameAnalysisTitle: '最強隊伍評價',
            gameAxisDamage: '輸出',
            gameAxisFront: '前排',
            gameAxisEngage: '開戰',
            gameAxisWave: '清兵',
            gameAxisChem: '默契',
            gameAxisStrength: '英雄強度',
            gameEvalStrength: '英雄強度',
            gameEvalStrengthTip: '5 人單獨勝率平均（非 PR）',
            gameEvalWave: '清兵',
            gameEvalMix: '傷害構成',
            gameEvalDamage: '輸出',
            gameEvalFront: '前排',
            gameEvalEngage: '開戰',
            gameMixAd: 'AD',
            gameMixAp: 'AP',
            gameMixTrue: 'True',
            gameMixNote: (ad, ap, tr) => `AD ${ad}% · AP ${ap}% · True ${tr}%`,
            gameBestEstWr: '最佳陣容勝率',
            gameWaitingData: '載入英雄資料中…',
            gameNoPool: '目前沒有足夠樣本的英雄可開局。',
            gameRoundOf: (a, b) => `第 ${a}/${b} 回合`,
            gameRoundN: n => `第 ${n} 回合`,
            gameNextRound: '下一回合',
            gameShowSettle: '結算',
            gameSettleTitle: '五回合結算',
            gameSettleAvg: '平均 OVR',
            gameSettleRankSub: (avg, total) => `平均名次 #${avg} / ${total}`,
            gameOvrValueShort: ovr => `OVR ${ovr}`,
            gameNickLabel: '暱稱',
            gameNickPlaceholder: '2–16 字',
            gameNickInvalid: '暱稱需 2–16 個字（去頭尾空白）',
            gameSubmit: '上傳成績',
            gameSubmitting: '上傳中…',
            gameSubmitOk: ovr => `已上傳 · 平均 OVR ${ovr}`,
            gameSubmitKept: ovr => `已上榜 · OVR ${ovr}`,
            gameSubmitFail: '上傳失敗，請稍後再試',
            gameSubmitDup: '這組成績已上榜，不能重傳同一局',
            gameSubmitLocked: '本局已上傳',
            gameRestart: '再來一輪（5 回合）',
            gameNeedFiveRounds: '請先完成 5 回合',
            gamePatchMissing: '缺少版本快照，無法上傳',
            gameBoardUnavailable: '排行榜未連線（未設定 API）',
            gameBoardLoading: '載入排行榜…',
            gameBoardFail: '排行榜載入失敗',
            gameBoardEmpty: '還沒有成績，來當第一名',
            gameBoardPatch: '版本',
            gameBoardNick: '暱稱',
            gameBoardAvg: '平均 OVR',
            gameBoardRanks: '各回合',
            detailEmpty: '這個英雄目前沒有可顯示的資料。',
            detailClose: '關閉詳細資訊',
            pairSectionTitle: '推薦搭檔',
            pairSectionMeta: '適配度為主，勝率為輔',
            setSectionTitle: '增幅裝置系列相性',
            setSectionMeta: '保守分數；負值代表相對較好，但未達正訊號',
            itemSectionTitle: '最強前兩件出裝',
            itemSectionMeta: '不含鞋子；選取 ≥1% · 最多 8 組 · 強度為主並保留最高出場',
            itemClusterSectionTitle: '',
            // Empty on purpose: core / 搭配裝備 / 常見後續 labels + per-item WR·pick
            // already carry the structure; the long methodology caption was noise.
            itemClusterSectionMeta: '',
            augTypeSectionTitle: '推薦增幅裝置傾向',
            augTypeSectionMeta: '細分類優先；分數扣掉同角色／傷害型英雄的平均偏好',
            relativeBest: '相對最佳',
            best: '最佳',
            worst: '最差',
            bestAugments: '最佳增幅裝置',
            worstAugments: '最差增幅裝置',
            augmentStrengthMeta: '強度綜合參考勝率與選取率',
            augmentStrengthTip: '排序以勝率提升的保守估計為主，並搭配選取率判斷樣本穩定度；低選取率的高勝率會更保守看待。卡片上的選用率用太陽色階表示熱門度：青→亮黃→枯葉黃→白→灰（越亮越熱門）；綠／紅只表示勝率。',
            weak: '偏弱',
            insufficient: '資料不足',
            rarityLabels: { kPrismatic: '彩色', kGold: '金色', kSilver: '銀色' },
            augSetLabel: '系列',
            augTitle: (name, setName, wr, games, desc) => `${name}${setName ? ' · 系列：' + setName : ''} · WR ${wr} · ${games}場${desc ? ' — ' + desc : ''}`,
            augAria: (name, wr, lift, games, desc) => `${name}，勝率 ${wr}，相對基準 ${lift}，樣本 ${games} 場${desc ? '，' + desc : ''}`,
            augTipStat: (wr, lift, games) => `WR ${wr} · ${lift} · ${games}場`,
            mateTitle: (name, wr, expectedText, lift, zText, games) => `${name} · WR ${wr}${expectedText} · residual ${lift} · z ${zText} · ${games}場`,
            mateMetaHtml: (lift, zText, games) => `${lift}<span class="mmeta-label"> residual</span><span class="mmeta-z"> · z ${zText}</span><span class="mmeta-games"> · ${games}場</span>`,
            setTitle: (name, res, lift, avg, wr, games) => `${name} · residual ${res} · 英雄 lift ${lift} · 類型平均 ${avg} · WR ${wr} · ${games}場`,
            setMeta: (lift, avg, wr, games) => `英雄 ${lift} · 類型 ${avg} · WR ${wr} · ${games}場`,
            itemBuildTitle: (name, pick, lift) => `${name} 選取率 ${pick} 勝率${lift}`,
            itemBuildCardTitle: (itemName, wr, pick, lift, games) => `${itemName} · WR ${wr} · 選取率 ${pick} · 勝率 ${lift} · ${games} 場`,
            itemClusterCardTitle: (name, wr, pick, lift, games, confirm, lane) => `${name}〔${lane}〕· 勝率 ${wr}（${lift}）· 選取率 ${pick} · 該選擇 ${games} 場 · 完整六件確認 ${confirm} 場`,
            // Not map lanes — tags for why this option is highlighted in the build route.
            itemClusterLanes: { popular: { label: '最常出' }, winrate: { label: '最高勝率' }, combined: { label: '綜合' } },
            itemClusterPick: (pick) => `${pick} 選取`,
            itemClusterGames: (games) => `${games} 場`,
            coreBuildShare: (pick) => `${pick} 選取率`,
            coreBuildWr: (wr) => `勝率 ${wr}`,
            coreBuildThird: '搭配裝備',
            coreBuildTail: '常見後續',
            coreBuildTailTip: '這套常一起出、但選取率與勝率都未達上方「搭配裝備」門檻的裝備',
            coreBuildHeadTitle: (name, wr, lift, pick, games) => `${name} · 勝率 ${wr}（${lift}）· 選取率 ${pick} · ${games} 場`,
            itemTipWr: '勝率',
            itemTipPick: '選取率',
            itemTipLift: '相對基準',
            itemTipGames: '場次',
            itemTipGold: '金',
            itemTipConfirm: '完整六件確認',
            itemTipLane: '標記',
            expected: value => ` · 預期 ${value}`,
            recRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · 推薦度 ${fit} · 搭配 ${pairFit} · 陣容 ${comp} · ${confidence}`,
            leastFitLabel: '最不適配',
            leastFitRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · 最不適配 ${fit} · 搭配 ${pairFit} · 陣容 ${comp} · ${confidence}`,
            champCardTitle: (name, wr, games, raw) => `${name} · WR ${wr} · games ${games} · raw ${raw}`,
            champCardAria: (name, alias, tier, wr) => `${name} ${alias}，tier ${tier}，勝率 ${wr}`,
            secondaryRoleBadgeTitle: (style, pick, lift) => `${style} 選取率 ${pick}，勝率${lift}`,
            secondaryRoleBadgePick: pick => `選取率 ${pick}`,
            secondaryRoleBadgeLift: lift => `勝率${lift}`,
            // Card surface: bare pick % only (label lives in the left rarity rail).
            augPickLabel: pick => pick,
            augSortWr: '勝率',
            augSortPick: '選用率',
            augSortWrAria: '依勝率排序此稀有度',
            augSortPickAria: '依選用率排序此稀有度',
            augHotBadge: '熱門',
            augFilterAll: '全部',
            augFilterAllTip: '清除分類，顯示全部增幅',
            augFilterNewTip: patch => `${patch} 新推出的增幅`,
            augFilterEmpty: '此分類沒有符合的增幅',
            augChampsHint: '點擊查看適配英雄',
            augChampsLiftHead: 'Lift 最高',
            augChampsLiftSub: '排序＝勝率提升值（小樣本會往下壓）',
            augChampsPickHead: '選取率最高',
            augChampsPickSub: '同稀有度出現時最常被選走',
            augChampsGames: games => `${games} 場`,
            augChampsEmpty: '樣本不足',
            augChampsFoot: n => `lift 與選取率皆為該英雄自身的數據；每組英雄×增幅樣本 ≥${n} 場`,
            overviewWrLabel: '綜合勝率',
            overviewGames: games => `${games} 場`,
            compFitTitle: '英雄能力',
            compFitMeta: '左：六軸能力百分位；右：操作係數與對局節奏',
            compFitAvoid: '避免',
            compFitPrefer: '適配',
            compFitAvoidDesc: '隊友角色重複，發揮變差',
            compFitFootnote: '陣型備註（僅顯示顯著差值）',
            compAbilityCap: '英雄能力',
            compSkillTitle: '操作係數',
            compSkillTip: '高分局勝率 − 低分局勝率（依對局水平前 25% vs 後 25%）',
            compSkillNeutral: '中性',
            compSkillHigh: '吃操作',
            compSkillLow: '低分強勢',
            compSkillMissing: '樣本不足',
            compSkillGames: g => `${g.toLocaleString('zh-TW')} 場`,
            compStageTitle: '對局節奏',
            compStageEarly: '前期型',
            compStageLate: '後期型',
            compStageBalanced: '均衡',
            compStageEarlyDesc: '短局勝率高、長局相對弱 — 盡早結束',
            compStageLateDesc: '長局勝率高、短局偏弱 — 拖進中後期',
            compStageBalancedDesc: '全英雄 PR40–60：短局長局差距不明顯',
            compStageEarlyAxis: '前期',
            compStageLateAxis: '後期',
            compStageTip: '前期＝≤16 分結束勝率；後期＝≥22 分結束勝率。依全英雄 late−early 百分位：PR40–60 為均衡；兩端為前期／後期型並顯示該方向排名（#1＝最偏）',
            compStageTipGap: '差距',
            compStageTipPr: pr => `PR ${pr}`,
            compStageTipRank: (rank, n) => `#${rank}/${n}`,
            compStageTipFootLines: [
                '前期 ≤16 分結束 · 後期 ≥22 分結束',
                'PR <40 前期 · 40–60 均衡 · >60 後期',
            ],
        },
        en: {
            htmlLang: 'en',
            subtitle: () => (SHORT_PATCH_ZH === 'all patches' ? 'All patches' : `Patch ${SHORT_PATCH_ZH}`),
            searchPlaceholderDesktop: 'Search champions (ZH / EN)   Ctrl+F',
            searchPlaceholderMobile: 'Search champions (ZH / EN)',
            searchAria: 'Search champions',
            shownUnit: 'shown',
            tierUnit: 'shown',
            updatesButton: 'Updates',
            updatesKicker: 'This build',
            updatesTitle: 'Recent important changes',
            updatesClose: 'Close recent updates',
            updatesItems: [
                '2026-06-29: Augment Tier pick rate is now average appearances per game — multiple players taking the same augment in one game stack — for a truer popularity signal (the hottest can exceed 100%). The heat ramp is now grey -> blue -> yellow -> orange -> red so the five steps are easy to tell apart.',
                '2026-06-17: Item pick-rate figures (single items, first two items, core rush, paired items, boots) now use the same popularity colour-coding as augments.',
                '2026-06-17: Augment cards now show only the pick rate beneath each tile, colour-coded by popularity (cool = rarely taken, gold = hot).',
                '2026-05-25: Added Single Item Strength, counting any final-slot item to help choose items three through six.',
                '2026-05-25: First-two-item and single-item recommendations now use swipeable carousels with denser mobile cards.',
                '2026-05-25: Augment rows now run strongest to weakest within each rarity instead of splitting into two blocks.',
            ],
            recModeOn: 'Teammate mode: On',
            recModeOff: 'Teammate mode: Off',
            emptyTitle: 'No champions match the current filters',
            emptyCopy: 'Try a different role, or search by Chinese / English champion name.',
            freshness: () => `Updated ${BUILD_DATE} (${TOTAL_GAMES} games) · ${PATCH_LABEL}`,
            sideTitle: 'Recommended teammates',
            sideSub: 'Pick 1–4 to rank fill-ins; pick all 5 for full-team win-rate and ability dims.',
            sideTitleFull: '5-man team evaluation',
            sideSubFull: 'Estimated from champion baselines, pair lifts, and composition tables; dims are mean percentiles.',
            closeRecs: 'Close recommendations',
            openRecs: n => n >= 5 ? `Open team eval (${n})` : `Open recommendations (${n})`,
            teamEstWr: 'Est. win rate',
            teamBaseWr: 'Champ avg',
            teamPairLift: 'Pair lift',
            teamCompAdj: 'Comp fit',
            teamPairCover: (have, total) => `Pairs known ${have}/${total}`,
            teamDimsTitle: 'Team ability dims',
            teamCompTitle: 'Composition',
            teamDimBad: 'Poor',
            teamDimMid: 'Fair',
            teamDimGood: 'Strong',
            teamConfHigh: 'High confidence',
            teamConfMid: 'Medium confidence',
            teamConfLow: 'Early signal',
            teamPickFullNote: 'Full roster of 5 · team evaluation (not a 6th pick list)',
            teamTempoTitle: 'Power timing',
            teamTempoEarly: 'Early',
            teamTempoLate: 'Late',
            teamTempoEarlyLean: 'Early-leaning',
            teamTempoLateLean: 'Late-leaning',
            teamTempoBalanced: 'Balanced timing',
            teamTempoTip: 'Early = mean WR in short games (≤16 min); Late = mean WR in long games (≥22 min). Numbers are win rates; early/late is not better or worse.',
            langToggleLabel: '中',
            langToggleTitle: '切換成中文',
            langToggleAria: 'Switch language',
            themeToLightTitle: 'Switch to light',
            themeToLightAria: 'Switch to light theme',
            themeToDarkTitle: 'Switch to dark',
            themeToDarkAria: 'Switch to dark theme',
            removePick: name => `Remove ${name}`,
            pickEmpty: 'Champion',
            maxOnly: n => `You can only pick up to ${n} champions.`,
            pickNoteEmpty: n => `Pick up to ${n}. Under 5 shows fill-in ranks; at 5 shows full-team WR and dims.`,
            pickNotePartial: want => `This selected group has less complete data, so the list uses known teammate fits first.`,
            pickNoteReady: (want, minGames) => `${want}/${MAX_TEAM_PICKS} picked; pair threshold >= ${minGames} games.`,
            panelEmpty: 'Open the Draft tab and pick 1–5 allies; you can also pick opponents. Under 5 ranks fills; with an enemy roster we show matchup WR and both team traits.',
            panelNoData: 'This combination does not have enough pair data yet.',
            draftEmpty: 'Click champions continuously: fill 5 allies first, then auto-continue into the enemy roster.',
            draftAllyEval: 'Ally composition',
            draftEnemyEval: 'Enemy composition',
            draftVs: 'VS',
            draftCompareTitle: 'Matchup compare',
            draftRadarTitle: 'Ability radar (both teams)',
            draftCompareExtras: 'Stat compare',
            draftLegendAlly: 'Ally',
            draftLegendEnemy: 'Enemy',
            draftChampStrength: 'Champ strength',
            draftMetricFinal: 'Final win rate',
            draftMetricFinalNote: 'AI win-rate estimate from 1) champion strength 2) synergy 3) matchup. Note: win rate is driven mainly by champion strength',
            draftMetricPartial: 'Complete both 5-champion rosters to calculate',
            draftMetricUnavailable: 'Model not loaded or contains unknown champions',
            draftOnOtherSide: 'Already on the other team.',
            gamePickCount: (n, max) => `Picked ${n}/${max}`,
            gameLock: 'Lock in',
            gamePlayAgain: 'Play again',
            gameMaxOnly: 'You can only pick 5.',
            gameNeedFive: 'Pick 5 champions before locking in.',
            gameMissTitle: 'Not quite',
            gameMissNeed: k => `${k} correct champion(s) still missing`,
            gameMissScore: (hit, total) => `Correct: ${hit}/${total}`,
            gameHint: 'Hint',
            gameReveal: 'Answer',
            gameHintUsed: name => `Hint: ${name}`,
            gameHintAuto: name => `Hint: ${name}`,
            gamePerfect: 'Perfect!',
            gameYourWr: 'Your est. WR',
            gameOptimalWr: 'Best team WR',
            gameYourTeam: 'Your pick',
            gameBestTeam: 'Best 5',
            gameDelta: 'Delta',
            gameGrade: 'Grade',
            gameRank: 'Rank',
            gamePr: 'PR',
            gameRankOf: (rank, total) => `#${rank} / ${total}`,
            gamePrValue: pr => `PR ${pr}`,
            gameHintBadge: 'Hint',
            gameOptimalBadge: 'Best',
            gameTagBoth: 'Picked · correct',
            gameTagYours: 'Picked',
            gameTagBest: 'Correct',
            gameTagMiss: '—',
            gameLegendPicked: 'Picked (white ring)',
            gameLegendCorrect: 'Best (yellow text)',
            gameLegendNeither: 'Not picked (gray)',
            gameCompareTitle: 'Roster compare',
            gamePoolReview: 'Champion pool',
            gameSoloWrList: 'Champion pool',
            gameTabPool: 'Pool',
            gameTabAnalysis: 'Team analysis',
            gameAnalysisBest: 'Best 5',
            gameAnalysisYours: 'Your pick',
            gameAnalysisTitle: 'Best team rating',
            gameAxisDamage: 'Damage',
            gameAxisFront: 'Front',
            gameAxisEngage: 'Engage',
            gameAxisWave: 'Wave',
            gameAxisChem: 'Synergy',
            gameAxisStrength: 'Strength',
            gameEvalStrength: 'Strength',
            gameEvalStrengthTip: 'Mean solo WR of the 5 (not a PR)',
            gameEvalWave: 'Wave',
            gameEvalMix: 'Damage mix',
            gameEvalDamage: 'Damage',
            gameEvalFront: 'Frontline',
            gameEvalEngage: 'Engage',
            gameMixAd: 'AD',
            gameMixAp: 'AP',
            gameMixTrue: 'True',
            gameMixNote: (ad, ap, tr) => `AD ${ad}% · AP ${ap}% · True ${tr}%`,
            gameBestEstWr: 'Best team WR',
            gameWaitingData: 'Loading champion data…',
            gameNoPool: 'Not enough champions with sample size to start a round.',
            gameRoundOf: (a, b) => `Round ${a}/${b}`,
            gameRoundN: n => `R${n}`,
            gameNextRound: 'Next round',
            gameShowSettle: 'Settlement',
            gameSettleTitle: '5-round settlement',
            gameSettleAvg: 'Average OVR',
            gameSettleRankSub: (avg, total) => `Avg rank #${avg} / ${total}`,
            gameOvrValueShort: ovr => `OVR ${ovr}`,
            gameNickLabel: 'Nickname',
            gameNickPlaceholder: '2–16 characters',
            gameNickInvalid: 'Nickname must be 2–16 characters (after trim)',
            gameSubmit: 'Submit score',
            gameSubmitting: 'Submitting…',
            gameSubmitOk: ovr => `Submitted · avg OVR ${ovr}`,
            gameSubmitKept: ovr => `On board · OVR ${ovr}`,
            gameSubmitFail: 'Submit failed — try again later',
            gameSubmitDup: 'This run is already on the board — cannot re-upload the same score',
            gameSubmitLocked: 'Already submitted this run',
            gameRestart: 'Play again (5 rounds)',
            gameNeedFiveRounds: 'Finish all 5 rounds first',
            gamePatchMissing: 'Missing patch snapshot — cannot submit',
            gameBoardUnavailable: 'Leaderboard offline (API not configured)',
            gameBoardLoading: 'Loading leaderboard…',
            gameBoardFail: 'Could not load leaderboard',
            gameBoardEmpty: 'No scores yet — be the first',
            gameBoardPatch: 'Patch',
            gameBoardNick: 'Name',
            gameBoardAvg: 'Avg OVR',
            gameBoardRanks: 'Rounds',
            detailEmpty: 'No detail data is available for this champion yet.',
            detailClose: 'Close details',
            pairSectionTitle: 'Recommended Pairings',
            pairSectionMeta: 'Fit first, win rate second',
            setSectionTitle: 'Augment Sets',
            setSectionMeta: 'Conservative score; negative can still be relative-best',
            itemSectionTitle: 'Best First Two Items',
            itemSectionMeta: 'boots excluded; pick ≥1% · up to 8 · strength first, keep top pick',
            itemClusterSectionTitle: '',
            // Empty on purpose — see zh itemClusterSectionMeta note.
            itemClusterSectionMeta: '',
            augTypeSectionTitle: 'Recommended Augment Tendencies',
            augTypeSectionMeta: 'Fine-grained first; scores are adjusted against similar role/damage-profile champions.',
            relativeBest: 'Relative Best',
            best: 'Best',
            worst: 'Worst',
            bestAugments: 'Best Augments',
            worstAugments: 'Worst Augments',
            augmentStrengthMeta: 'Strength considers both win rate and pick rate',
            augmentStrengthTip: 'Ranking is led by conservative win-rate lift, with pick rate used as a stability signal; low-pick high-win results are treated more carefully. Pick-rate uses a solar ladder: cyan → bright yellow → ochre → white → gray (brighter = hotter); green/red are reserved for win rate.',
            weak: 'Weak',
            insufficient: 'Not enough data',
            rarityLabels: { kPrismatic: 'Prismatic', kGold: 'Gold', kSilver: 'Silver' },
            augSetLabel: 'Set',
            augTitle: (name, setName, wr, games, desc) => `${name}${setName ? ' · Set: ' + setName : ''} · WR ${wr} · ${games} games${desc ? ' — ' + desc : ''}`,
            augAria: (name, wr, lift, games, desc) => `${name}, win rate ${wr}, versus baseline ${lift}, sample ${games} games${desc ? ', ' + desc : ''}`,
            augTipStat: (wr, lift, games) => `WR ${wr} · ${lift} · ${games} games`,
            mateTitle: (name, wr, expectedText, lift, zText, games) => `${name} · WR ${wr}${expectedText} · residual ${lift} · z ${zText} · ${games} games`,
            mateMetaHtml: (lift, zText, games) => `${lift}<span class="mmeta-label"> residual</span><span class="mmeta-z"> · z ${zText}</span><span class="mmeta-games"> · ${games} games</span>`,
            setTitle: (name, res, lift, avg, wr, games) => `${name} · residual ${res} · champion lift ${lift} · type average ${avg} · WR ${wr} · ${games} games`,
            setMeta: (lift, avg, wr, games) => `champ ${lift} · type ${avg} · WR ${wr} · ${games} games`,
            itemBuildTitle: (name, pick, lift) => `${name} pick ${pick}, WR ${lift}`,
            itemBuildCardTitle: (itemName, wr, pick, lift, games) => `${itemName} · WR ${wr} · pick ${pick} · lift ${lift} · ${games} games`,
            itemClusterCardTitle: (name, wr, pick, lift, games, confirm, lane) => `${name} [${lane}] · WR ${wr} (${lift}) · pick ${pick} · ${games} core games · ${confirm} full-build confirmations`,
            // Not map lanes — tags for why this option is highlighted in the build route.
            itemClusterLanes: { popular: { label: 'Most picked' }, winrate: { label: 'Best WR' }, combined: { label: 'Balanced' } },
            itemClusterPick: (pick) => `${pick} pick`,
            itemClusterGames: (games) => `${games}g`,
            coreBuildShare: (pick) => `${pick} pick`,
            coreBuildWr: (wr) => `${wr} WR`,
            coreBuildThird: 'Other items',
            coreBuildTail: 'Also common',
            coreBuildTailTip: 'Built with this core but below the pick-rate / win-rate bar of the items above',
            coreBuildHeadTitle: (name, wr, lift, pick, games) => `${name} · WR ${wr} (${lift}) · pick ${pick} · ${games} games`,
            itemTipWr: 'Win rate',
            itemTipPick: 'Pick rate',
            itemTipLift: 'vs baseline',
            itemTipGames: 'Games',
            itemTipGold: 'gold',
            itemTipConfirm: 'Full-build confirms',
            itemTipLane: 'Highlight',
            expected: value => ` · expected ${value}`,
            recRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · fit ${fit} · pair ${pairFit} · comp ${comp} · ${confidence}`,
            leastFitLabel: 'Least fit',
            leastFitRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · least fit ${fit} · pair ${pairFit} · comp ${comp} · ${confidence}`,
            champCardTitle: (name, wr, games, raw) => `${name} · WR ${wr} · games ${games} · raw ${raw}`,
            champCardAria: (name, alias, tier, wr) => `${name} ${alias}, tier ${tier}, win rate ${wr}`,
            secondaryRoleBadgeTitle: (style, pick, lift) => `${style} pick ${pick}, WR ${lift}`,
            secondaryRoleBadgePick: pick => `pick ${pick}`,
            secondaryRoleBadgeLift: lift => `WR ${lift}`,
            // Card surface: bare pick % only (label lives in the left rarity rail).
            augPickLabel: pick => pick,
            augSortWr: 'WR',
            augSortPick: 'Pick',
            augSortWrAria: 'Sort this rarity by win rate',
            augSortPickAria: 'Sort this rarity by pick rate',
            augHotBadge: 'Hot',
            augFilterAll: 'All',
            augFilterAllTip: 'Clear categories — show every augment',
            augFilterNewTip: patch => `Augments introduced in ${patch}`,
            augFilterEmpty: 'No augments match this category',
            augChampsHint: 'Click to see who fits it best',
            augChampsLiftHead: 'Highest lift',
            augChampsLiftSub: 'sorted by WR lift (small samples demoted)',
            augChampsPickHead: 'Most picked',
            augChampsPickSub: 'taken most often when offered at this rarity',
            augChampsGames: games => `${games} games`,
            augChampsEmpty: 'Not enough data',
            augChampsFoot: n => `lift and pick rate are champion-relative; each champion×augment pair needs ≥${n} games`,
            overviewWrLabel: 'Overall WR',
            overviewGames: games => `${games} games`,
            compFitTitle: 'Abilities',
            compFitMeta: 'left: 6-axis ability percentiles; right: skill-scaling & tempo',
            compFitAvoid: 'Avoid',
            compFitPrefer: 'Fits',
            compFitAvoidDesc: 'redundant teammates — underperforms',
            compFitFootnote: 'Comp notes (only clear deltas)',
            compAbilityCap: 'Abilities',
            compSkillTitle: 'Skill-scaling',
            compSkillTip: 'WR in high-skill minus low-skill lobbies (top vs bottom 25% by lobby skill)',
            compSkillNeutral: 'skill-neutral',
            compSkillHigh: 'rewards skill',
            compSkillLow: 'stomps low elo',
            compSkillMissing: 'not enough data',
            compSkillGames: g => `${g.toLocaleString('en-US')} games`,
            compStageTitle: 'Game tempo',
            compStageEarly: 'Early',
            compStageLate: 'Late',
            compStageBalanced: 'Balanced',
            compStageEarlyDesc: 'Higher WR in short games — close it out early',
            compStageLateDesc: 'Higher WR in long games — drag into mid/late',
            compStageBalancedDesc: 'PR40–60 among all champs — no clear early/late lean',
            compStageEarlyAxis: 'Early',
            compStageLateAxis: 'Late',
            compStageTip: 'Early = WR when game ends ≤16 min; Late = ≥22 min. Classified by percentile of late−early across all champs: PR40–60 balanced; outside that band labeled Early/Late with direction rank (#1 = most extreme)',
            compStageTipGap: 'Gap',
            compStageTipPr: pr => `PR ${pr}`,
            compStageTipRank: (rank, n) => `#${rank}/${n}`,
            compStageTipFootLines: [
                'Early ≤16 min · Late ≥22 min',
                'PR <40 early · 40–60 balanced · >60 late',
            ],
        }
    };
    // Prefer URL / stub-stashed locale over the zh SSR default.
    const LANG_OPTIONS = [
        { id: 'zh', label: '繁體中文', short: '繁中', htmlLang: 'zh-Hant', prefix: '' },
        { id: 'zh-CN', label: '简体中文', short: '简中', htmlLang: 'zh-Hans', prefix: '/zh-CN' },
        { id: 'en', label: 'English', short: 'EN', htmlLang: 'en', prefix: '/en' },
    ];
    function normalizeLang(lang) {
        if (lang === 'en') return 'en';
        if (lang === 'zh-CN' || lang === 'zh_CN' || lang === 'zh-Hans' || lang === 'cn') return 'zh-CN';
        return 'zh';
    }
    let currentLang = normalizeLang(pendingBootLang || 'zh');
    let updatesOpen = false;
    let activeUpdateTab = 'heroes';
    let filterState = { role: '', q: '' };
    let _trZhCN = null;

    // Product term (aramkit / CN client): 增幅(裝置) → 海克斯.
    // Protect rune names like 冰川增幅 (Glacial Augment) from the bare replace.
    function cnUiTerms(s) {
        if (s == null || s === '') return s;
        let t = String(s);
        const locks = [];
        t = t.replace(/冰川增幅/g, () => {
            const i = locks.length;
            locks.push('冰川增幅');
            return `\uE000${i}\uE001`;
        });
        t = t.replace(/增幅裝置/g, '海克斯').replace(/增幅装置/g, '海克斯');
        t = t.replace(/增幅/g, '海克斯');
        t = t.replace(/\uE000(\d+)\uE001/g, (_, i) => locks[+i] || '');
        return t;
    }
    function t2s(s) {
        if (s == null || s === '') return s;
        const map = (NAMES_ZH_CN && NAMES_ZH_CN.t2s) || null;
        let out = String(s);
        if (map) {
            let built = '';
            for (const ch of out) built += map[ch] || ch;
            out = built;
        }
        // Always apply product-term remap for zh-CN callers (safe no-op on EN).
        return cnUiTerms(out);
    }
    function localizeZhCN(value) {
        if (value == null) return value;
        if (typeof value === 'string') return t2s(value);
        if (typeof value === 'number' || typeof value === 'boolean') return value;
        if (typeof value === 'function') {
            return function localizedFn(...args) {
                return localizeZhCN(value.apply(this, args));
            };
        }
        if (Array.isArray(value)) return value.map(localizeZhCN);
        if (typeof value === 'object') {
            const out = {};
            for (const k of Object.keys(value)) out[k] = localizeZhCN(value[k]);
            return out;
        }
        return value;
    }
    // Chinese UI string: keep traditional, or convert when in simplified mode.
    function zhUi(s) {
        return currentLang === 'zh-CN' ? t2s(s) : s;
    }
    // Pick EN vs ZH (auto-converts ZH → simplified when needed).
    function pickLang(zh, en) {
        return currentLang === 'en' ? en : zhUi(zh);
    }
    function langMeta(lang) {
        const id = normalizeLang(lang || currentLang);
        return LANG_OPTIONS.find(o => o.id === id) || LANG_OPTIONS[0];
    }
    function tr() {
        if (currentLang === 'en') return COPY.en;
        if (currentLang === 'zh-CN') {
            if (!_trZhCN) _trZhCN = localizeZhCN(COPY.zh);
            return _trZhCN;
        }
        return COPY.zh;
    }

    function roleLabel(role) {
        const labels = ROLE_LABELS.zh || {};
        const enLabels = ROLE_LABELS.en || {};
        if (currentLang === 'en') return enLabels[role] || role;
        return zhUi(labels[role] || role);
    }

    function styleLabel(info) {
        if (!info) return '';
        return currentLang === 'en'
            ? (info.styleNameEn || info.styleName || '')
            : (info.styleNameZh || info.styleName || info.styleNameEn || '');
    }

    function secondaryRoleBadgeSummary(info, role) {
        const copy = tr();
        const style = styleLabel(info) || roleLabel(role);
        const pick = pct(info.pick || 0);
        const liftValue = Number(info.lift || 0);
        const lift = signed(liftValue);
        return {
            style,
            pick,
            lift,
            title: copy.secondaryRoleBadgeTitle(style, pick, lift),
            pickLabel: copy.secondaryRoleBadgePick(pick),
            liftLabel: copy.secondaryRoleBadgeLift(lift),
            toneClass: liftValue > 0.0005 ? 'is-good' : (liftValue < -0.0005 ? 'is-bad' : 'is-even'),
        };
    }

    function secondaryRoleBadgeTooltipHtml(summary) {
        return `
            <span class="alt-role-tooltip" role="tooltip">
                <span class="alt-role-tooltip-style">${escHtml(summary.style)}</span>
                <span class="alt-role-tooltip-pick">${escHtml(summary.pickLabel)}</span>
                <span class="alt-role-tooltip-lift ${summary.toneClass}">${escHtml(summary.liftLabel)}</span>
            </span>
        `;
    }

    function secondaryRoleBadgeIconHtml(role) {
        return ROLE_BADGE_ICONS[role] || '';
    }

    // Localized role pills shown next to the champion name in the detail header.
    // info.tags is the fixed site role list (primary first, optional secondary).
    function buildDetailRoleTags(info) {
        const tags = (info && info.tags) || [];
        if (!tags.length) return '';
        const inner = tags.map(role =>
            `<span class="detail-role-tag" data-role="${escHtml(role)}">${escHtml(roleLabel(role))}</span>`
        ).join('');
        return `<span class="detail-roles">${inner}</span>`;
    }

    function refreshSecondaryRoleBadges() {
        const role = filterState.role;
        document.querySelectorAll('.champ').forEach(champ => {
            const badge = champ.querySelector('.alt-role-badge');
            if (!badge) return;
            const cid = champ.getAttribute('data-cid');
            const info = DATA.champs[cid] || {};
            const tags = info.tags || [];
            const secondaryRole = tags[1] || '';
            const primaryRole = tags[0] || secondaryRole || '';
            const show = Boolean(role) && secondaryRole === role;
            if (!show) {
                champ.classList.remove('secondary-role-match');
                badge.setAttribute('hidden', '');
                badge.setAttribute('aria-label', '');
                badge.innerHTML = '';
                return;
            }
            const roleInfo = ((info.roleMeta || {})[secondaryRole]) || null;
            const hasDataBadge = Boolean(
                roleInfo &&
                roleInfo.source === 'data' &&
                typeof roleInfo.pick === 'number'
            );
            champ.classList.toggle('secondary-role-match', show && hasDataBadge);
            if (!hasDataBadge) {
                badge.setAttribute('hidden', '');
                badge.setAttribute('aria-label', '');
                badge.innerHTML = '';
                return;
            }
            const summary = secondaryRoleBadgeSummary(roleInfo, secondaryRole);
            badge.removeAttribute('hidden');
            badge.setAttribute('data-alt-role', primaryRole);
            badge.setAttribute('aria-label', summary.title);
            badge.innerHTML = secondaryRoleBadgeIconHtml(primaryRole) + secondaryRoleBadgeTooltipHtml(summary);
        });
    }

    function positionSecondaryRoleTooltip(badge) {
        if (!badge || badge.hasAttribute('hidden')) return;
        const tooltip = badge.querySelector('.alt-role-tooltip');
        if (!tooltip) return;
        badge.classList.remove('tip-right', 'tip-below');
        const viewportPad = 12;
        let rect = tooltip.getBoundingClientRect();
        if (rect.left < viewportPad) {
            badge.classList.add('tip-right');
            rect = tooltip.getBoundingClientRect();
        }
        if (rect.right > window.innerWidth - viewportPad) {
            badge.classList.remove('tip-right');
        }
        const champ = badge.closest('.champ');
        const champRect = champ ? champ.getBoundingClientRect() : null;
        if (champRect && champRect.top < 96) {
            badge.classList.add('tip-below');
        }
    }

    function positionFitChipTooltip(wrap) {
        if (!wrap) return;
        const tooltip = wrap.querySelector('.fit-chip-tooltip');
        if (!tooltip) return;
        wrap.classList.remove('tip-left', 'tip-below');
        const viewportPad = 12;
        const rect = tooltip.getBoundingClientRect();
        if (rect.right > window.innerWidth - viewportPad) {
            wrap.classList.add('tip-left');
        }
        const wrapRect = wrap.getBoundingClientRect();
        if (wrapRect.top < 96) {
            wrap.classList.add('tip-below');
        }
    }

    function isMobileViewport() {
        return window.matchMedia('(max-width: 700px)').matches;
    }

    function searchPlaceholderFor(copy) {
        return isMobileViewport()
            ? copy.searchPlaceholderMobile
            : copy.searchPlaceholderDesktop;
    }

    function updateSearchPlaceholder() {
        const copy = tr();
        const searchEl = document.getElementById('champ-search');
        if (searchEl) {
            searchEl.placeholder = searchPlaceholderFor(copy);
            searchEl.setAttribute('aria-label', copy.searchAria);
        }
        // Draft pool search is a separate input (hardcoded zh in HTML shell).
        const draftSearch = document.getElementById('draft-search');
        if (draftSearch) {
            draftSearch.placeholder = copy.searchPlaceholderMobile;
            draftSearch.setAttribute('aria-label', copy.searchAria);
        }
    }

    function champName(info, cid) {
        if (!info) return '';
        if (currentLang === 'en') return info.name_en || info.alias || info.name || '';
        if (currentLang === 'zh-CN') {
            const id = cid != null ? String(cid)
                : (info.id != null ? String(info.id)
                : (info.champion_id != null ? String(info.champion_id) : ''));
            const cn = id && NAMES_ZH_CN && NAMES_ZH_CN.champs && NAMES_ZH_CN.champs[id];
            if (cn) return cn;
            return t2s(info.name_zh || info.name || info.alias || '');
        }
        return info.name_zh || info.name || info.alias || '';
    }

    function augName(aug, aid) {
        if (!aug) return '';
        if (currentLang === 'en') return aug.name_en || aug.name || '';
        if (currentLang === 'zh-CN') {
            if (aug.name_cn) return aug.name_cn;
            const id = aid != null ? String(aid)
                : (aug.id != null ? String(aug.id) : '');
            const row = id && NAMES_ZH_CN && NAMES_ZH_CN.augs && NAMES_ZH_CN.augs[id];
            if (row && row.n) return row.n;
            return t2s(aug.name_zh || aug.name || '');
        }
        return aug.name_zh || aug.name || '';
    }

    function augDesc(aug, aid) {
        if (!aug) return '';
        if (currentLang === 'en') return aug.desc_en || aug.desc || '';
        if (currentLang === 'zh-CN') {
            if (aug.desc_cn) return aug.desc_cn;
            const id = aid != null ? String(aid)
                : (aug.id != null ? String(aug.id) : '');
            const row = id && NAMES_ZH_CN && NAMES_ZH_CN.augs && NAMES_ZH_CN.augs[id];
            if (row && row.d) return row.d;
            return t2s(aug.desc_zh || aug.desc || '');
        }
        return aug.desc_zh || aug.desc || '';
    }

    function augSetName(aug, aid) {
        if (!aug) return '';
        if (currentLang === 'en') return aug.set_en || aug.set || '';
        if (currentLang === 'zh-CN') {
            if (aug.set_cn) return aug.set_cn;
            const id = aid != null ? String(aid)
                : (aug.id != null ? String(aug.id) : '');
            const row = id && NAMES_ZH_CN && NAMES_ZH_CN.augs && NAMES_ZH_CN.augs[id];
            if (row && row.s) return row.s;
            return t2s(aug.set_zh || aug.set || aug.set_en || '');
        }
        return aug.set_zh || aug.set || aug.set_en || '';
    }

    // Augment-set / named-row label (NOT for item build cards — use itemRowDisplayName).
    function setEntryName(entry) {
        if (!entry) return '';
        if (currentLang === 'en') return entry.name_en || entry.name || '';
        if (currentLang === 'zh-CN') return t2s(entry.name_zh || entry.name || entry.name_en || '');
        return entry.name_zh || entry.name || entry.name_en || '';
    }

    // ----- Rich item hover tooltips (LoL-style floating card) -----
    // Source markup is rendered inside each host as .item-tip-src (display:none).
    // A single fixed #item-float-tip clones that HTML on hover/focus so parent
    // overflow:auto / overflow:hidden carousels never clip the popup.
    function itemCatalogEntry(id) {
        if (id === null || id === undefined || id === '') return null;
        const m = (DATA.itemLut || {})[String(id)];
        if (!m) return null;
        const ver = DATA.ddv || '';
        return {
            id: String(id),
            name_zh: m.z || '',
            name_en: m.e || '',
            name_cn: m.c || '',
            desc_zh: m.dz || '',
            desc_en: m.de || '',
            desc_cn: m.dc || '',
            price: Number(m.p || 0) || 0,
            icon: ver
                ? ('https://ddragon.leagueoflegends.com/cdn/' + ver + '/img/item/' + id + '.png')
                : '',
        };
    }

    function itemCnName(id) {
        if (id == null || id === '') return '';
        const key = String(id);
        const lut = DATA.itemLut && DATA.itemLut[key];
        if (lut && lut.c) return lut.c;
        if (NAMES_ZH_CN && NAMES_ZH_CN.items && NAMES_ZH_CN.items[key]) {
            return NAMES_ZH_CN.items[key];
        }
        return '';
    }
    function itemCnDesc(id) {
        if (id == null || id === '') return '';
        const key = String(id);
        const lut = DATA.itemLut && DATA.itemLut[key];
        if (lut && lut.dc) return lut.dc;
        if (NAMES_ZH_CN && NAMES_ZH_CN.itemDescs && NAMES_ZH_CN.itemDescs[key]) {
            return NAMES_ZH_CN.itemDescs[key];
        }
        return '';
    }
    function itemDisplayName(item) {
        if (!item) return '';
        if (currentLang === 'zh-CN') {
            if (item.name_cn) return item.name_cn;
            const cn = itemCnName(item.id);
            if (cn) return cn;
            // slug-only rows (rare)
            if (item.slug && /^\d+$/.test(String(item.slug))) {
                const bySlug = itemCnName(item.slug);
                if (bySlug) return bySlug;
            }
            if (item.name_zh || item.name || item.name_en) {
                return t2s(item.name_zh || item.name || item.name_en || '');
            }
            const cat = itemCatalogEntry(item.id);
            if (!cat) return item.id ? ('#' + item.id) : '';
            return cat.name_cn || t2s(cat.name_zh || cat.name_en || '');
        }
        if (item.name_zh || item.name_en || item.name) {
            return currentLang === 'en'
                ? (item.name_en || item.name || item.name_zh || '')
                : (item.name_zh || item.name || item.name_en || '');
        }
        const cat = itemCatalogEntry(item.id);
        if (!cat) return item.id ? ('#' + item.id) : '';
        return currentLang === 'en'
            ? (cat.name_en || cat.name_zh || '')
            : (cat.name_zh || cat.name_en || '');
    }

    // Item recommendation / build-card row: may be a single item or "A + B" pair.
    // Always prefer composing official CN names from child item ids — TW labels
    // like 芮蘭颶風箭 are not the same as CN 卢安娜的飓风 (t2s alone is wrong).
    function itemRowDisplayName(entry) {
        if (!entry) return '';
        if (currentLang === 'en') {
            return entry.name_en || entry.name || entry.name_zh || '';
        }
        const pairItems = Array.isArray(entry.items) ? entry.items : [];
        if (currentLang === 'zh-CN') {
            if (pairItems.length) {
                const parts = pairItems.map(it => itemDisplayName(it)).filter(Boolean);
                if (parts.length) return parts.join(' + ');
            }
            if (entry.slug) {
                const slug = String(entry.slug);
                if (/^\d+$/.test(slug)) {
                    const cn = itemCnName(slug);
                    if (cn) return cn;
                } else if (slug.includes('+')) {
                    const parts = slug.split('+').map(s => itemCnName(s.trim())).filter(Boolean);
                    if (parts.length) return parts.join(' + ');
                }
            }
            if (entry.id != null && entry.id !== '') {
                const cn = itemCnName(entry.id);
                if (cn) return cn;
            }
            return t2s(entry.name_zh || entry.name || entry.name_en || '');
        }
        return entry.name_zh || entry.name || entry.name_en || '';
    }

    function itemDescription(item) {
        if (!item) return '';
        if (currentLang === 'zh-CN') {
            if (item.desc_cn) return item.desc_cn;
            const cn = itemCnDesc(item.id);
            if (cn) return cn;
            const direct = item.desc_zh || item.desc || item.desc_en || '';
            if (direct) return t2s(String(direct));
            const cat = itemCatalogEntry(item.id);
            if (!cat) return '';
            return cat.desc_cn || t2s(cat.desc_zh || cat.desc_en || '');
        }
        const direct = currentLang === 'en'
            ? (item.desc_en || item.desc_zh || item.desc || '')
            : (item.desc_zh || item.desc_en || item.desc || '');
        if (direct) return String(direct);
        const cat = itemCatalogEntry(item.id);
        if (!cat) return '';
        return currentLang === 'en'
            ? (cat.desc_en || cat.desc_zh || '')
            : (cat.desc_zh || cat.desc_en || '');
    }

    function itemGold(item) {
        if (!item) return 0;
        const direct = Number(item.price || item.price_total || 0);
        if (direct > 0) return direct;
        const cat = itemCatalogEntry(item.id);
        return cat ? (cat.price || 0) : 0;
    }

    function itemIconUrl(item) {
        if (!item) return '';
        if (item.icon) return String(item.icon);
        const cat = itemCatalogEntry(item.id);
        return cat ? (cat.icon || '') : '';
    }

    function liftToneClass(lift) {
        const v = Number(lift || 0);
        if (v > 0.005) return 'is-good';
        if (v < -0.005) return 'is-bad';
        return 'is-even';
    }

    function formatItemDescHtml(desc) {
        const text = String(desc || '').trim();
        if (!text) return '';
        // CommunityDragon plain text uses newlines between stat lines and passives.
        return text
            .split(/\n+/)
            .map(line => line.trim())
            .filter(Boolean)
            .map(line => `<div class="item-tip-line">${escHtml(line)}</div>`)
            .join('');
    }

    function buildItemTipHtml(opts = {}) {
        const copy = tr();
        const name = opts.name || '';
        const items = Array.isArray(opts.items) ? opts.items.filter(Boolean) : [];
        const iconUrls = (opts.icons && opts.icons.length)
            ? opts.icons
            : items.map(itemIconUrl).filter(Boolean);
        const iconsHtml = iconUrls.length
            ? `<div class="item-tip-icons">${iconUrls.map(src => (
                `<img class="item-tip-icon" src="${escHtml(src)}" alt="" loading="lazy">`
            )).join('')}</div>`
            : '';

        // Single-item gold; multi-item builds skip a combined price (ambiguous).
        let goldHtml = '';
        if (items.length === 1) {
            const gold = itemGold(items[0]);
            if (gold > 0) {
                goldHtml = `<span class="item-tip-gold">${gold.toLocaleString()} ${escHtml(copy.itemTipGold || 'gold')}</span>`;
            }
        } else if (!items.length && opts.price > 0) {
            goldHtml = `<span class="item-tip-gold">${Number(opts.price).toLocaleString()} ${escHtml(copy.itemTipGold || 'gold')}</span>`;
        }
        const subtitleHtml = opts.subtitle
            ? `<div class="item-tip-sub">${escHtml(opts.subtitle)}</div>`
            : '';

        // Descriptions: full text for 1 item, compact name+desc for 2, names only beyond.
        let bodyHtml = '';
        if (items.length === 1) {
            const descHtml = formatItemDescHtml(itemDescription(items[0]));
            if (descHtml) bodyHtml = `<div class="item-tip-desc">${descHtml}</div>`;
        } else if (items.length === 2) {
            bodyHtml = `<div class="item-tip-parts">${items.map(it => {
                const nm = itemDisplayName(it);
                const descHtml = formatItemDescHtml(itemDescription(it));
                return `<div class="item-tip-part">
                    <div class="item-tip-part-name">${escHtml(nm)}</div>
                    ${descHtml ? `<div class="item-tip-desc">${descHtml}</div>` : ''}
                </div>`;
            }).join('')}</div>`;
        } else if (items.length > 2) {
            const names = items.map(itemDisplayName).filter(Boolean);
            if (names.length) {
                bodyHtml = `<div class="item-tip-parts-list">${names.map(nm => (
                    `<span class="item-tip-chip">${escHtml(nm)}</span>`
                )).join('')}</div>`;
            }
        } else if (opts.desc) {
            const descHtml = formatItemDescHtml(opts.desc);
            if (descHtml) bodyHtml = `<div class="item-tip-desc">${descHtml}</div>`;
        }

        const rows = [];
        if (opts.wr != null && opts.wr !== '') {
            rows.push(`<div class="item-tip-row"><span>${escHtml(copy.itemTipWr || 'WR')}</span><b class="${liftToneClass(opts.lift)}">${escHtml(String(opts.wr))}</b></div>`);
        }
        if (opts.pick != null && opts.pick !== '') {
            const rate = Number(opts.pickRate != null ? opts.pickRate : 0);
            const colorFn = typeof opts.colorTierFn === 'function' ? opts.colorTierFn : pickColorTier;
            const pickHeat = pickHeatClass(rate, colorFn);
            rows.push(`<div class="item-tip-row"><span>${escHtml(copy.itemTipPick || 'Pick')}</span><b class="${pickHeat}">${escHtml(String(opts.pick))}</b></div>`);
        }
        if (opts.lift != null && opts.lift !== '' && opts.liftLabel) {
            rows.push(`<div class="item-tip-row"><span>${escHtml(copy.itemTipLift || 'Lift')}</span><b class="${liftToneClass(opts.lift)}">${escHtml(String(opts.liftLabel))}</b></div>`);
        }
        if (opts.games != null && opts.games !== '') {
            rows.push(`<div class="item-tip-row"><span>${escHtml(copy.itemTipGames || 'Games')}</span><b>${escHtml(String(opts.games))}</b></div>`);
        }
        if (opts.confirm != null && opts.confirm !== '' && Number(opts.confirm) > 0) {
            rows.push(`<div class="item-tip-row"><span>${escHtml(copy.itemTipConfirm || 'Confirm')}</span><b>${escHtml(String(opts.confirm))}</b></div>`);
        }
        if (opts.lane) {
            rows.push(`<div class="item-tip-row"><span>${escHtml(copy.itemTipLane || 'Lane')}</span><b>${escHtml(String(opts.lane))}</b></div>`);
        }
        (opts.extraRows || []).forEach(row => {
            if (!row || !row.label) return;
            const cls = row.cls ? ` class="${escHtml(row.cls)}"` : '';
            rows.push(`<div class="item-tip-row"><span>${escHtml(row.label)}</span><b${cls}>${escHtml(String(row.value ?? ''))}</b></div>`);
        });
        const statsHtml = rows.length
            ? `<div class="item-tip-stats">${rows.join('')}</div>`
            : '';
        const noteHtml = opts.note
            ? `<div class="item-tip-note">${escHtml(opts.note)}</div>`
            : '';

        return `
            <div class="item-tip-card">
                <div class="item-tip-head">
                    ${iconsHtml}
                    <div class="item-tip-head-text">
                        <div class="item-tip-name">${escHtml(name)}</div>
                        ${subtitleHtml}
                        ${goldHtml}
                    </div>
                </div>
                ${bodyHtml}
                ${statsHtml}
                ${noteHtml}
            </div>
        `;
    }

    function itemTipSource(html) {
        // Hidden source node; floating layer clones this on hover/focus.
        return `<div class="item-tip-src" hidden>${html}</div>`;
    }

    let _itemFloatTipEl = null;
    let _itemFloatTipHideTimer = 0;
    function ensureItemFloatTip() {
        if (_itemFloatTipEl && document.body.contains(_itemFloatTipEl)) return _itemFloatTipEl;
        const el = document.createElement('div');
        el.id = 'item-float-tip';
        el.className = 'item-float-tip';
        el.setAttribute('role', 'tooltip');
        el.hidden = true;
        document.body.appendChild(el);
        _itemFloatTipEl = el;
        return el;
    }

    function hideItemFloatTip() {
        if (_itemFloatTipHideTimer) {
            clearTimeout(_itemFloatTipHideTimer);
            _itemFloatTipHideTimer = 0;
        }
        const el = _itemFloatTipEl || document.getElementById('item-float-tip');
        if (!el) return;
        el.hidden = true;
        el.classList.remove('is-visible', 'flip-below', 'align-left', 'align-right', 'is-scrollable');
        el.style.maxHeight = '';
        el.innerHTML = '';
    }

    function positionItemFloatTip(anchor) {
        const el = ensureItemFloatTip();
        if (!anchor || el.hidden) return;
        const gap = 10;
        const margin = 8;
        const rect = anchor.getBoundingClientRect();
        const vw = window.innerWidth || document.documentElement.clientWidth || 0;
        const vh = window.innerHeight || document.documentElement.clientHeight || 0;

        // Natural content height first — no forced scrollbar for short tips.
        el.classList.remove('flip-below', 'align-left', 'align-right', 'is-scrollable');
        el.style.maxHeight = '';
        el.style.left = '0px';
        el.style.top = '0px';

        let tipRect = el.getBoundingClientRect();
        const maxH = Math.max(120, vh - margin * 2);
        // Only clamp + scroll when content genuinely exceeds the viewport.
        if (tipRect.height > maxH) {
            el.style.maxHeight = maxH + 'px';
            el.classList.add('is-scrollable');
            tipRect = el.getBoundingClientRect();
        }

        let left = rect.left + (rect.width / 2) - (tipRect.width / 2);
        left = Math.max(margin, Math.min(left, vw - tipRect.width - margin));

        let top = rect.top - tipRect.height - gap;
        let flipBelow = false;
        if (top < margin) {
            top = rect.bottom + gap;
            flipBelow = true;
        }
        if (top + tipRect.height > vh - margin) {
            // Prefer whichever side has more room.
            const spaceAbove = rect.top - margin;
            const spaceBelow = vh - rect.bottom - margin;
            if (spaceBelow > spaceAbove) {
                top = Math.min(rect.bottom + gap, vh - tipRect.height - margin);
                flipBelow = true;
            } else {
                top = Math.max(margin, rect.top - tipRect.height - gap);
                flipBelow = false;
            }
        }

        el.style.left = Math.round(left) + 'px';
        el.style.top = Math.round(top) + 'px';
        el.classList.toggle('flip-below', flipBelow);
        // Arrow horizontal alignment toward the anchor center.
        const arrowX = rect.left + rect.width / 2 - left;
        el.style.setProperty('--item-tip-arrow-x', Math.round(arrowX) + 'px');
    }

    function showItemFloatTip(anchor) {
        if (!anchor) return;
        const src = anchor.querySelector('.item-tip-src');
        if (!src) return;
        if (_itemFloatTipHideTimer) {
            clearTimeout(_itemFloatTipHideTimer);
            _itemFloatTipHideTimer = 0;
        }
        const el = ensureItemFloatTip();
        el.innerHTML = src.innerHTML;
        el.hidden = false;
        el.classList.add('is-visible');
        positionItemFloatTip(anchor);
    }

    function scheduleHideItemFloatTip() {
        if (_itemFloatTipHideTimer) clearTimeout(_itemFloatTipHideTimer);
        _itemFloatTipHideTimer = setTimeout(() => {
            _itemFloatTipHideTimer = 0;
            hideItemFloatTip();
        }, 80);
    }

    function compactSearchText(value) {
        return String(value || '').toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, '');
    }

    function searchMatchesText(haystack, query) {
        const q = String(query || '').trim().toLowerCase();
        if (!q) return false;
        const text = String(haystack || '').toLowerCase();
        if (text.includes(q)) return true;
        const compactQ = compactSearchText(q);
        return Boolean(compactQ) && compactSearchText(text).includes(compactQ);
    }

    function entrySearchText(entry) {
        const parts = [
            entry?.name,
            entry?.name_zh,
            entry?.name_en,
            entry?.name_cn,
            entry?.set,
            entry?.set_zh,
            entry?.set_en,
            entry?.set_cn,
            entry?.slug,
        ];
        // Always index official CN labels so simplified-mode search works.
        if (entry && Array.isArray(entry.items)) {
            entry.items.forEach(item => {
                parts.push(item.name, item.name_zh, item.name_en, item.name_cn, item.id);
                if (item && item.id != null) parts.push(itemCnName(item.id));
            });
        } else if (entry && entry.slug) {
            String(entry.slug).split('+').forEach(s => {
                const id = s.trim();
                if (/^\d+$/.test(id)) parts.push(itemCnName(id));
            });
        }
        return parts.filter(Boolean).join(' ');
    }

    function currentSearchQuery() {
        const searchEl = document.getElementById('champ-search');
        return searchEl ? searchEl.value : filterState.q;
    }

    function applySearchHighlights(root = document) {
        const q = currentSearchQuery();
        // Empty query is the common case (grid open, no search): skip the
        // per-card regex/compaction work and just clear any leftover hits cheaply.
        if (!q || !String(q).trim()) {
            root.querySelectorAll('[data-match-text].search-hit').forEach(card => card.classList.remove('search-hit'));
            return;
        }
        root.querySelectorAll('[data-match-text]').forEach(card => {
            card.classList.toggle('search-hit', searchMatchesText(card.getAttribute('data-match-text') || '', q));
        });
    }

    function buildAugCard(entry, kind, opts) {
        const aug = DATA.augs[entry.id];
        const name = aug ? augName(aug, entry.id) : '#' + entry.id;
        const icon = aug && aug.icon ? aug.icon : '';
        const rarity = aug ? (aug.rarity || '') : '';
        const desc = augDesc(aug, entry.id);
        const setName = augSetName(aug, entry.id);
        const copy = tr();
        const matchText = [
            name,
            aug?.name,
            aug?.name_zh,
            aug?.name_en,
            setName,
            aug?.set,
            aug?.set_zh,
            aug?.set_en,
            aug?.setSlug,
            entry.id,
        ].filter(Boolean).join(' ');
        // The 增幅榜 board's pick is "average appearances per game" (counts
        // multiplicity), a different scale than the champion-relative pick the
        // per-champion cards carry, so the board uses its own 熱門 cut + heat ramp.
        const onBoard = !!(opts && opts.board);
        // Augment card carries its own ARIA semantics so screen readers and
        // keyboard users get the same info hover tooltip shows.
        const pickRate = Number(entry.pick || 0);
        const pickPct = pct(pickRate);
        const isHot = pickRate >= (onBoard ? AUG_BOARD_HOT_PICK : AUG_HOT_PICK);
        const hotBadge = isHot ? `<span class="aug-hot-badge">${copy.augHotBadge}</span>` : '';
        // Board: own per-game colour scale. Champ: absolute solar ladder.
        const pickHeat = onBoard
            ? pickHeatClass(pickRate, augBoardColorTier)
            : pickHeatClass(pickRate);
        const cats = (aug && Array.isArray(aug.cats)) ? aug.cats.join(' ') : '';
        // rawWr stays in the payload for sorting/debug, but the card no longer
        // shows a "raw … · n=" line — hover tip already carries WR / pick / games.
        const ariaLabel = copy.augAria(name, pct(entry.wr), signed(entry.lift), entry.g, desc);
        // Shared rich float tip (same card language as items).
        const tipHtml = buildItemTipHtml({
            name,
            icons: icon ? [icon] : [],
            subtitle: setName
                ? `${copy.augSetLabel}: ${setName}`
                : '',
            desc,
            wr: pct(entry.wr),
            pick: pickPct,
            pickRate,
            colorTierFn: onBoard ? augBoardColorTier : pickColorTier,
            lift: entry.lift,
            liftLabel: signed(entry.lift),
            games: entry.g,
            note: onBoard ? copy.augChampsHint : '',
        });
        // Card: icon → name → WR% → pick% (two bare numbers, stacked). Labels
        // for 勝率 / 選用率 live in the left rarity rail (with sort controls).
        return `
            <div class="aug ${kind} rarity-${rarity} has-item-tip"
                 tabindex="0"
                 data-aug-id="${escHtml(String(entry.id))}"
                 data-wr="${Number(entry.wr || 0)}"
                 data-pick="${pickRate}"
                 data-match-text="${escHtml(matchText)}"
                 data-cats="${escHtml(cats)}"
                 aria-label="${escHtml(ariaLabel)}">
                ${hotBadge}
                ${icon ? `<img loading="lazy" src="${icon}" alt="">` : '<div class="aicon-ph"></div>'}
                <div class="aname"><span>${escHtml(name)}</span></div>
                <div class="awr wr-${wrToneTier(entry)}">${pct(entry.wr)}</div>
                <div class="alift ${pickHeat}" title="${escHtml(pickLang(`選用率 ${pickPct}`, `pick ${pickPct}`))}">${copy.augPickLabel(pickPct)}</div>
                ${itemTipSource(tipHtml)}
            </div>
        `;
    }

    /** Reorder .aug cards inside one rarity row by wr or pick (desc). */
    function sortRarityAugList(row, sortKey) {
        if (!row) return;
        const list = row.querySelector('.aug-list');
        if (!list) return;
        const attr = sortKey === 'pick' ? 'data-pick' : 'data-wr';
        const cards = Array.from(list.querySelectorAll('.aug[data-aug-id]'));
        cards.sort((a, b) => {
            const av = Number(a.getAttribute(attr) || 0);
            const bv = Number(b.getAttribute(attr) || 0);
            if (bv !== av) return bv - av;
            // Stable-ish tie-break: keep existing DOM order via data-aug-id.
            return String(a.getAttribute('data-aug-id') || '')
                .localeCompare(String(b.getAttribute('data-aug-id') || ''), undefined, { numeric: true });
        });
        cards.forEach(card => list.appendChild(card));
        row.querySelectorAll('.rlabel-sort').forEach(btn => {
            const active = btn.getAttribute('data-sort') === sortKey;
            btn.classList.toggle('is-active', active);
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
    }

    // Champion-relative pick rate (0-1) at or above this flags an augment as 熱門.
    const AUG_HOT_PICK = 0.20;
    // 增幅榜 board pick = average appearances per game (counts multiplicity), so it
    // runs on its own scale: the most-taken augment sits near ~0.75/game, the
    // median near ~0.14, and a value can exceed 1.0.  Give the board its own 熱門
    // cut + colour ramp so the colour still means "popular" relative to the field.
    const AUG_BOARD_HOT_PICK = 0.40;
    // Absolute solar colour ladder — 5 tiers, colour only (no border).
    //   ≥50% pick-5  青 #67E8F9     20–49.9% pick-4  亮黃 #FDE047
    //   5–19.9% pick-3 枯葉黃 #C4A35A  1–4.9% pick-2  白 #E6E8EB
    //   <1% pick-1  灰 #7C8AA1
    function pickColorTier(pick) {
        if (pick >= 0.50) return 5;
        if (pick >= 0.20) return 4;
        if (pick >= 0.05) return 3;
        if (pick >= 0.01) return 2;
        return 1;
    }
    // Board appearances/game scale (not champion-relative %).
    function augBoardColorTier(pick) {
        if (pick >= 0.40) return 5;
        if (pick >= 0.28) return 4;
        if (pick >= 0.18) return 3;
        if (pick >= 0.10) return 2;
        return 1;
    }
    function pickHeatClass(pick, colorTierFn) {
        const tier = (typeof colorTierFn === 'function' ? colorTierFn : pickColorTier)(pick);
        return `pick-${tier}`;
    }
    /**
     * 5-step win-rate tone for .awr:
     *   5 正綠 → 4 淺綠 → 3 無色 → 2 淺紅 → 1 深紅
     * Prefer champion-relative lift when present; else WR vs 50%.
     */
    function wrToneTier(entry) {
        const liftRaw = entry && entry.lift;
        if (liftRaw != null && Number.isFinite(Number(liftRaw))) {
            const L = Number(liftRaw);
            if (L >= 0.03) return 5;   // +3pp+
            if (L >= 0.01) return 4;   // +1–3pp
            if (L > -0.01) return 3;   // ≈ flat
            if (L > -0.03) return 2;   // −1–3pp
            return 1;                 // −3pp+
        }
        const d = Number(entry && entry.wr || 0) - 0.5;
        if (d >= 0.05) return 5;
        if (d >= 0.02) return 4;
        if (d > -0.02) return 3;
        if (d > -0.05) return 2;
        return 1;
    }
    const RARITIES = [
        { key: 'kPrismatic', css: 'prismatic' },
        { key: 'kGold',      css: 'gold' },
        { key: 'kSilver',    css: 'silver' },
    ];
    const MATE_LIST_LIMIT_DESKTOP = 8;
    const MATE_LIST_LIMIT_MOBILE = 6;

    // ----- Single-item role filter (出裝 → 單件裝備強度) -------------------------
    // Single-select chips: All + 6 roles + 常見.  Role comes from shell-injected
    // ITEM_FILTER_ROLES (CDragon style → role list); 常見 = pick ≥ 10% on this champ.
    // An item may match multiple roles (e.g. 殞落之祭 → Mage + Marksman).
    function itemFilterRolesForId(id) {
        if (id == null || id === '') return [];
        const key = String(id);
        const lut = DATA && DATA.itemLut && DATA.itemLut[key];
        if (lut && lut.r) {
            return String(lut.r).split(/\s+/).filter(Boolean);
        }
        const v = ITEM_FILTER_ROLES[key];
        if (Array.isArray(v)) return v.map(String).filter(Boolean);
        if (typeof v === 'string' && v) return v.split(/\s+/).filter(Boolean);
        return [];
    }
    function itemFilterRoleLabels(role) {
        if (role === 'common') return { zh: '常見', en: 'Common' };
        if (!role) return { zh: '全部', en: 'All' };
        const pack = ROLE_LABELS || {};
        return {
            zh: (pack.zh && pack.zh[role]) || role,
            en: (pack.en && pack.en[role]) || role,
        };
    }
    function buildSingleItemFilterChips() {
        if (!ITEM_FILTER_ROLES || !Object.keys(ITEM_FILTER_ROLES).length) return '';
        const allOn = !singleItemFilter;
        const mk = (key, extraClass = '') => {
            const on = key ? singleItemFilter === key : !singleItemFilter;
            const labels = itemFilterRoleLabels(key);
            const shown = currentLang === 'en' ? labels.en : zhUi(labels.zh);
            const tip = key === 'common'
                ? ` title="${escHtml(pickLang('選取率 ≥ 10%', 'pick rate ≥ 10%'))}"`
                : '';
            return `<button type="button" class="item-role-chip${extraClass}${on ? ' is-active' : ''}" data-item-filter="${key}" data-label-zh="${escHtml(labels.zh)}" data-label-en="${escHtml(labels.en)}" aria-pressed="${on}"${tip}>${escHtml(shown)}</button>`;
        };
        const chips = [mk('', '')];
        ITEM_FILTER_ROLE_ORDER.forEach(role => chips.push(mk(role, ` role-${role}`)));
        chips.push(mk('common', ' role-common'));
        return `<div class="item-role-bar" role="group" aria-label="${escHtml(pickLang('裝備篩選', 'Item filters'))}">${chips.join('')}</div>`;
    }
    function applySingleItemFilter(root) {
        if (!root) return;
        root.querySelectorAll('.item-role-chip').forEach(chip => {
            const key = chip.getAttribute('data-item-filter') || '';
            const on = key ? singleItemFilter === key : !singleItemFilter;
            chip.classList.toggle('is-active', on);
            chip.setAttribute('aria-pressed', String(on));
        });
        root.querySelectorAll('.single-item-filter-host').forEach(host => {
            const filter = singleItemFilter;
            let shown = 0;
            let total = 0;
            host.querySelectorAll('.single-item-card').forEach(card => {
                total++;
                const roles = (card.getAttribute('data-item-role') || '').split(/\s+/).filter(Boolean);
                const pick = Number(card.getAttribute('data-item-pick') || 0);
                let match = true;
                if (filter === 'common') match = pick >= SINGLE_ITEM_COMMON_MIN_PICK;
                else if (filter) match = roles.includes(filter);
                card.classList.toggle('item-filter-hidden', !match);
                if (match) shown++;
            });
            let empty = host.querySelector('.item-filter-empty');
            if (filter && total > 0 && shown === 0) {
                if (!empty) {
                    empty = document.createElement('div');
                    empty.className = 'item-filter-empty mate-list empty-list';
                    host.appendChild(empty);
                }
                empty.hidden = false;
                empty.textContent = pickLang('沒有符合此篩選的裝備', 'No items match this filter');
            } else if (empty) {
                empty.hidden = true;
            }
        });
    }
    function setSingleItemFilter(key) {
        singleItemFilter = key || '';
        document.querySelectorAll('.detail').forEach(applySingleItemFilter);
    }
    document.addEventListener('click', (ev) => {
        const chip = ev.target.closest('.item-role-chip');
        if (!chip) return;
        ev.preventDefault();
        const key = chip.getAttribute('data-item-filter') || '';
        // Toggle off → All when re-clicking the active non-all chip.
        if (key && singleItemFilter === key) setSingleItemFilter('');
        else setSingleItemFilter(key);
    });

    // ----- Augment category filter (chips above the per-champion ranking) -----
    // Multi-select OR filter, persisted across champion switches within a
    // session.  Each .aug card carries data-cats; chips toggle membership and we
    // show/hide the matching cards (+ collapse rarity rows that empty out)
    // without re-rendering the whole detail.
    const augCatFilter = new Set();
    function augCatMeta() {
        return (DATA && DATA.augCategories) || { order: [], labels: {}, newPatch: '' };
    }
    function augCatLabel(cat) {
        const lbl = (augCatMeta().labels || {})[cat];
        if (!lbl) return cat;
        return currentLang === 'en' ? (lbl.en || lbl.zh || cat) : zhUi(lbl.zh || lbl.en || cat);
    }
    function buildAugCatChips() {
        const meta = augCatMeta();
        const order = Array.isArray(meta.order) ? meta.order : [];
        if (!order.length) return '';
        const copy = tr();
        const allActive = augCatFilter.size === 0;
        const chips = [
            `<button type="button" class="aug-cat-chip aug-cat-all${allActive ? ' is-active' : ''}" data-cat="" aria-pressed="${allActive}" title="${escHtml(copy.augFilterAllTip)}">${escHtml(copy.augFilterAll)}</button>`,
        ];
        order.forEach(cat => {
            const active = augCatFilter.has(cat);
            const tip = (cat === 'new' && meta.newPatch) ? ` title="${escHtml(copy.augFilterNewTip(meta.newPatch))}"` : '';
            chips.push(`<button type="button" class="aug-cat-chip cat-${cat}${active ? ' is-active' : ''}" data-cat="${cat}" aria-pressed="${active}"${tip}>${escHtml(augCatLabel(cat))}</button>`);
        });
        return `<div class="aug-cat-bar" role="group" aria-label="${escHtml(pickLang('增幅分類', 'Augment categories'))}">${chips.join('')}</div>`;
    }
    // Re-apply the active filter to every .aug card under `root`, syncing chip
    // pressed state and collapsing rarity rows that hold cards but match none.
    function applyAugCatFilter(root) {
        if (!root) return;
        const active = augCatFilter;
        root.querySelectorAll('.aug-cat-chip').forEach(chip => {
            const cat = chip.getAttribute('data-cat') || '';
            const on = cat ? active.has(cat) : active.size === 0;
            chip.classList.toggle('is-active', on);
            chip.setAttribute('aria-pressed', String(on));
        });
        root.querySelectorAll('.rarity-row').forEach(row => {
            let shown = 0;
            const cards = row.querySelectorAll('.aug');
            cards.forEach(card => {
                const cats = (card.getAttribute('data-cats') || '').split(' ').filter(Boolean);
                const match = active.size === 0 || cats.some(c => active.has(c));
                card.classList.toggle('cat-hidden', !match);
                if (match) shown++;
            });
            row.classList.toggle('cat-empty', cards.length > 0 && shown === 0 && active.size > 0);
        });
    }
    function toggleAugCat(cat) {
        if (!cat) augCatFilter.clear();
        else if (augCatFilter.has(cat)) augCatFilter.delete(cat);
        else augCatFilter.add(cat);
        document.querySelectorAll('.detail').forEach(applyAugCatFilter);
    }
    document.addEventListener('click', (ev) => {
        const chip = ev.target.closest('.aug-cat-chip');
        // The 增幅榜 tab reuses .aug-cat-chip styling but has its own filter state
        // and handler (below); don't let the per-champion detail filter grab them.
        if (!chip || chip.closest('#aug-tier-filters')) return;
        ev.preventDefault();
        toggleAugCat(chip.getAttribute('data-cat') || '');
    });

    /* ===== 增幅榜 (global augment tier) =========================================
       Built client-side from DATA.augs (each augment carries wr/g/lift/pick from
       the Python rollup).  Tier = within-rarity percentile of wr, so a strong
       Silver is not buried under mediocre Prismatics.  Reuses buildAugCard + the
       .tier-block visuals; rarity + category chips filter the cards in place. */
    const AUG_TIER_MIN_GAMES = 500;
    const AUG_TIER_PCT = [['OP',0.08],['T1',0.17],['T2',0.25],['T3',0.25],['T4',0.17],['T5',0.08]];
    const AUG_RARITY_LABELS = {
        kPrismatic: { zh: '棱彩', en: 'Prismatic' },
        kGold:      { zh: '金',   en: 'Gold' },
        kSilver:    { zh: '銀',   en: 'Silver' },
    };
    const AUG_RARITY_ORDER = ['', 'kPrismatic', 'kGold', 'kSilver'];
    let augTierRarity = '';
    const augTierCats = new Set();

    function augTierEntries() {
        const out = [];
        const augs = (DATA && DATA.augs) || {};
        for (const id in augs) {
            const a = augs[id];
            if (a && typeof a.wr === 'number' && (a.g || 0) >= AUG_TIER_MIN_GAMES) {
                out.push({ id: id, wr: a.wr, g: a.g, rawWr: a.rawWr, lift: a.lift, pick: a.pick, rarity: a.rarity || '' });
            }
        }
        return out;
    }
    function computeAugTiers(entries) {
        const byR = {};
        entries.forEach(e => { (byR[e.rarity] = byR[e.rarity] || []).push(e); });
        Object.keys(byR).forEach(rar => {
            const list = byR[rar];
            list.sort((a, b) => b.wr - a.wr);
            const n = list.length;
            let idx = 0, cum = 0;
            AUG_TIER_PCT.forEach(pair => {
                cum += pair[1];
                const cutoff = Math.round(cum * n);
                while (idx < cutoff && idx < n) { list[idx].tier = pair[0]; idx++; }
            });
            while (idx < n) { list[idx].tier = 'T5'; idx++; }
        });
        return entries;
    }
    function augRarityLabel(rar) {
        if (!rar) return pickLang('全部', 'All');
        const l = AUG_RARITY_LABELS[rar];
        return l ? pickLang(l.zh, l.en) : rar;
    }
    function buildAugTierFilters() {
        const host = document.getElementById('aug-tier-filters');
        if (!host) return;
        const rarChips = AUG_RARITY_ORDER.map(rar => {
            const on = augTierRarity === rar;
            const cls = 'aug-cat-chip aug-rarity-chip' + (rar ? ' rarity-' + rar : '') + (on ? ' is-active' : '');
            return `<button type="button" class="${cls}" data-rarity="${rar}" aria-pressed="${on}">${escHtml(augRarityLabel(rar))}</button>`;
        }).join('');
        const meta = augCatMeta();
        const order = Array.isArray(meta.order) ? meta.order : [];
        const allCat = augTierCats.size === 0;
        const catChips = [`<button type="button" class="aug-cat-chip aug-cat-all${allCat ? ' is-active' : ''}" data-tcat="" aria-pressed="${allCat}">${escHtml(pickLang('全部', 'All'))}</button>`]
            .concat(order.map(cat => {
                const on = augTierCats.has(cat);
                return `<button type="button" class="aug-cat-chip cat-${cat}${on ? ' is-active' : ''}" data-tcat="${cat}" aria-pressed="${on}">${escHtml(augCatLabel(cat))}</button>`;
            })).join('');
        const rarLbl = pickLang('稀有度', 'Rarity');
        const catLbl = pickLang('分類', 'Category');
        host.innerHTML =
            `<div class="aug-cat-bar aug-rarity-bar" role="group" aria-label="${escHtml(rarLbl)}">${rarChips}</div>`
            + `<div class="aug-cat-bar" role="group" aria-label="${escHtml(catLbl)}">${catChips}</div>`;
    }
    function renderAugmentTier() {
        const host = document.getElementById('aug-tier-host');
        if (!host) return;
        // The board's HTML depends only on the language (labels / names) and on
        // whether the CN name dictionary has landed — augment data is static per
        // page load.  Re-entering the tab used to rebuild hundreds of cards
        // inside the tab-switch task; when nothing changed, just re-apply the
        // cheap class-toggle filters instead.
        const renderKey = currentLang + ':' + (NAMES_ZH_CN ? 1 : 0);
        if (host.getAttribute('data-render-key') === renderKey && host.firstChild) {
            buildAugTierFilters();
            applyAugTierFilters();
            renderAugChamps();
            return;
        }
        host.setAttribute('data-render-key', renderKey);
        buildAugTierFilters();
        const tierMeta = (DATA && DATA.tiers) || {};
        const order = (tierMeta.order && tierMeta.order.length) ? tierMeta.order : ['OP','T1','T2','T3','T4','T5'];
        const colors = tierMeta.colors || {};
        const entries = computeAugTiers(augTierEntries());
        if (!entries.length) {
            host.innerHTML = `<div class="empty-state visible">${escHtml(pickLang('尚無增幅勝率資料。', 'Augment win-rate data is not available yet.'))}</div>`;
            return;
        }
        const byTier = {};
        entries.forEach(e => { (byTier[e.tier] = byTier[e.tier] || []).push(e); });
        const unit = pickLang('個', '');
        const blocks = [];
        order.forEach(tier => {
            const list = byTier[tier];
            if (!list || !list.length) return;
            list.sort((a, b) => b.wr - a.wr);
            const col = colors[tier] || {};
            const cards = list.map(e => buildAugCard(e, e.wr >= 0.5 ? 'good' : 'bad', { board: true })).join('');
            blocks.push(
                `<div class="tier-block aug-tier-block" data-tier="${tier}" style="--tier-color:${col.color || '#555'}; --tier-bg:${col.bg || '#555'};">`
                + `<h2 class="tier-heading"><span class="tier-pill"><span>${tier}</span></span>`
                + `<span class="tier-count"><span class="aug-tier-count-num">${list.length}</span>${unit ? ' ' + escHtml(unit) : ''}</span></h2>`
                + `<div class="tier-grid aug-tier-grid">${cards}</div>`
                + `</div>`
            );
        });
        host.innerHTML = blocks.join('');
        applyAugTierFilters();
        // Language toggle re-renders the board; keep an open champion popup in
        // sync (no-op when closed).
        renderAugChamps();
    }
    function applyAugTierFilters() {
        const host = document.getElementById('aug-tier-host');
        const filters = document.getElementById('aug-tier-filters');
        if (!host) return;
        host.querySelectorAll('.aug-tier-block').forEach(block => {
            let shown = 0;
            block.querySelectorAll('.aug').forEach(card => {
                const cats = (card.getAttribute('data-cats') || '').split(' ').filter(Boolean);
                const rarCls = [...card.classList].find(c => c.indexOf('rarity-') === 0) || '';
                const rarKey = rarCls.replace('rarity-', '');
                const okRar = !augTierRarity || rarKey === augTierRarity;
                const okCat = augTierCats.size === 0 || cats.some(c => augTierCats.has(c));
                const hide = !(okRar && okCat);
                card.classList.toggle('hidden', hide);
                if (!hide) shown++;
            });
            const num = block.querySelector('.aug-tier-count-num');
            if (num) num.textContent = shown;
            block.classList.toggle('hidden', shown === 0);
        });
        if (filters) {
            filters.querySelectorAll('.aug-rarity-chip').forEach(chip => {
                const on = (chip.getAttribute('data-rarity') || '') === augTierRarity;
                chip.classList.toggle('is-active', on); chip.setAttribute('aria-pressed', String(on));
            });
            filters.querySelectorAll('[data-tcat]').forEach(chip => {
                const cat = chip.getAttribute('data-tcat') || '';
                const on = cat ? augTierCats.has(cat) : augTierCats.size === 0;
                chip.classList.toggle('is-active', on); chip.setAttribute('aria-pressed', String(on));
            });
        }
    }
    document.addEventListener('click', (ev) => {
        if (!ev.target.closest('#aug-tier-filters')) return;
        const rChip = ev.target.closest('[data-rarity]');
        if (rChip) {
            ev.preventDefault();
            augTierRarity = rChip.getAttribute('data-rarity') || '';
            applyAugTierFilters();
            return;
        }
        const tChip = ev.target.closest('[data-tcat]');
        if (tChip) {
            ev.preventDefault();
            const cat = tChip.getAttribute('data-tcat') || '';
            if (!cat) augTierCats.clear();
            else if (augTierCats.has(cat)) augTierCats.delete(cat);
            else augTierCats.add(cat);
            applyAugTierFilters();
            return;
        }
    });

    // ----- 增幅榜: click an augment card -> which champions it fits best -----
    // The payload's champ.top buckets ship the FULL ranked augment list per
    // champion (build_champ_augment_picks keeps the whole bucket for the
    // carousel), so inverting them client-side yields every champion×augment
    // pair above the min-games floor — no extra payload needed.
    let augChampsId = null;
    let _augChampIdx = null;
    function augChampIndex() {
        if (_augChampIdx) return _augChampIdx;
        const idx = new Map();
        for (const cid in DATA.champs) {
            const buckets = DATA.champs[cid].top || {};
            for (const rar in buckets) {
                (buckets[rar] || []).forEach(e => {
                    const key = Number(e.id);
                    let arr = idx.get(key);
                    if (!arr) idx.set(key, arr = []);
                    arr.push({
                        cid,
                        g: Number(e.g) || 0,
                        wr: Number(e.wr) || 0,
                        lift: Number(e.lift) || 0,
                        // score = conservative LCB lift + small pick-stability
                        // term (the same ranking the champion carousel uses).
                        score: Number(e.score != null ? e.score : e.lift) || 0,
                        pick: Number(e.pick) || 0,
                    });
                });
            }
        }
        _augChampIdx = idx;
        return idx;
    }
    const AUG_CHAMP_LIST_LIMIT = 8;
    function buildAugChampRow(r, mode) {
        const info = DATA.champs[r.cid] || {};
        const val = mode === 'lift'
            ? `<span class="augch-val ${r.lift >= 0 ? 'up' : 'down'}">${signed(r.lift)}</span>`
            : `<span class="augch-val">${pct(r.pick)}</span>`;
        const sub = mode === 'lift'
            ? `WR ${pct(r.wr)} · n=${fmtInt(r.g)}`
            : `n=${fmtInt(r.g)}`;
        return `
            <button type="button" class="augch-row" data-cid="${escHtml(String(r.cid))}">
                ${info.image ? `<img loading="lazy" src="${info.image}" alt="">` : ''}
                <span class="augch-nm">${escHtml(champName(info, r.cid))}</span>
                <span class="augch-meta">${escHtml(sub)}</span>
                ${val}
            </button>
        `;
    }
    function renderAugChamps() {
        const view = document.getElementById('view-augments');
        if (!view) return;
        let host = document.getElementById('augch-host');
        const aug = augChampsId != null ? DATA.augs[augChampsId] : null;
        document.body.classList.toggle('augch-open', Boolean(aug));
        if (!aug) { if (host) host.remove(); return; }
        if (!host) {
            // Lives inside the augments view so its z-index resolves in the
            // same stacking context as the view (see the veil comment in CSS).
            host = document.createElement('div');
            host.id = 'augch-host';
            view.appendChild(host);
        }
        const copy = tr();
        const rows = augChampIndex().get(Number(augChampsId)) || [];
        const byLift = rows.slice().sort((a, b) => (b.score - a.score) || (b.g - a.g)).slice(0, AUG_CHAMP_LIST_LIMIT);
        const byPick = rows.slice().sort((a, b) => (b.pick - a.pick) || (b.g - a.g)).slice(0, AUG_CHAMP_LIST_LIMIT);
        const section = (head, subText, list, mode) => `
            <div class="augch-col">
                <div class="augch-colhead">${escHtml(head)}</div>
                <div class="augch-colsub">${escHtml(subText)}</div>
                ${list.length
                    ? list.map(r => buildAugChampRow(r, mode)).join('')
                    : `<div class="augch-empty">${escHtml(copy.augChampsEmpty)}</div>`}
            </div>
        `;
        const setName = augSetName(aug, augChampsId);
        const rarityLabel = copy.rarityLabels[aug.rarity] || '';
        const statBits = [];
        if (aug.wr != null) statBits.push(`WR ${pct(aug.wr)}`);
        if (aug.lift != null) statBits.push(signed(aug.lift));
        if (aug.g != null) statBits.push(copy.augChampsGames(fmtInt(aug.g)));
        host.innerHTML = `
            <div class="augch-card rarity-${escHtml(aug.rarity || '')}" role="dialog" aria-modal="true" aria-labelledby="augch-title">
                <button type="button" class="augch-close" id="augch-close" aria-label="${escHtml(copy.updatesClose || 'close')}">×</button>
                <div class="augch-head">
                    ${aug.icon ? `<img class="augch-icon" src="${aug.icon}" alt="">` : ''}
                    <div>
                        <div class="augch-name" id="augch-title">${escHtml(augName(aug, augChampsId))}</div>
                        <div class="augch-sub">${escHtml([rarityLabel, setName].filter(Boolean).join(' · '))}</div>
                        <div class="augch-stat">${escHtml(statBits.join(' · '))}</div>
                    </div>
                </div>
                <div class="augch-cols">
                    ${section(copy.augChampsLiftHead, copy.augChampsLiftSub, byLift, 'lift')}
                    ${section(copy.augChampsPickHead, copy.augChampsPickSub, byPick, 'pick')}
                </div>
                <div class="augch-foot">${escHtml(copy.augChampsFoot(DATA.min_games_per_pair || 15))}</div>
            </div>
        `;
        const closeBtn = host.querySelector('#augch-close');
        if (closeBtn) closeBtn.focus({ preventScroll: true });
    }
    function closeAugChamps() {
        augChampsId = null;
        renderAugChamps();
    }
    document.addEventListener('click', (ev) => {
        if (augChampsId != null) {
            if (ev.target.closest('#augch-close')) { closeAugChamps(); return; }
            const row = ev.target.closest('.augch-row');
            if (row) {
                const cid = row.getAttribute('data-cid');
                closeAugChamps();
                openDetailByCid(cid);
                trackEvent('aug_champs_champ_click', { champion_id: cid });
                return;
            }
            const hostEl = document.getElementById('augch-host');
            // Tap on the backdrop (the host itself) closes, same as the mobile
            // champion-detail sheet.
            if (hostEl && ev.target === hostEl) { closeAugChamps(); return; }
            if (hostEl && hostEl.contains(ev.target)) return;
        }
        const card = ev.target.closest('#view-augments .aug[data-aug-id]');
        if (card) {
            augChampsId = card.getAttribute('data-aug-id');
            renderAugChamps();
            trackEvent('aug_champs_open', { augment_id: augChampsId });
        }
    });

    function buildRarityRow(items, kind, r) {
        const copy = tr();
        const cards = (items || []).map(e => {
            const cardKind = kind === 'ranked'
                ? (Number(e.lift || 0) >= 0 ? 'good' : 'bad')
                : kind;
            return buildAugCard(e, cardKind);
        }).join('');
        // The left control is a peer of .aug cards inside the same flex row —
        // same height, same bottom two rows — so 勝率/選用率 line up with the
        // WR/pick numbers. Structure mirrors a card: head (centered rarity) +
        // two fixed-height foot rows (sort keys where other cards put stats).
        const sortWr = escHtml(copy.augSortWr || '勝率');
        const sortPick = escHtml(copy.augSortPick || '選用率');
        const rail = `
            <div class="rlabel-rail rlabel ${r.css}" role="group"
                 aria-label="${escHtml(copy.rarityLabels[r.key])}">
                <div class="rlabel-head">
                    <div class="rlabel-name">${escHtml(copy.rarityLabels[r.key])}</div>
                </div>
                <button type="button" class="rlabel-sort is-active" data-sort="wr"
                        aria-pressed="true"
                        aria-label="${escHtml(copy.augSortWrAria || sortWr)}">${sortWr}</button>
                <button type="button" class="rlabel-sort" data-sort="pick"
                        aria-pressed="false"
                        aria-label="${escHtml(copy.augSortPickAria || sortPick)}">${sortPick}</button>
            </div>`;
        const body = cards
            ? `${rail}${cards}`
            : `${rail}<div class="aug-list-empty">${copy.insufficient}</div>`;
        return `
            <div class="rarity-row" data-rarity="${escHtml(r.key)}">
                <div class="aug-list">${body}</div>
            </div>
        `;
    }

    function renderDetail(cid) {
        // renderDetail reads item name/icon off DATA.champs[cid]'s stripped item
        // rows; ensure they are rehydrated (cheap no-op if already done or if the
        // background warm pass reached this champ first).
        rehydrateChamp(cid);
        const info = DATA.champs[cid];
        if (!info) {
            return `<div class="empty">${tr().detailEmpty}</div>`;
        }
        const copy = tr();
        const top = info.top || {};
        const setInfo = info.sets || {};
        const setTop = setInfo.top || [];
        const itemInfo = info.items || {};
        const singleItemInfo = info.singleItems || {};
        const bootInfo = info.boots || {};
        const spellInfo = info.spells || {};
        const itemClusterInfo = info.itemClusters || {};
        const augTypeInfo = info.augTypes || {};
        const augmentRankTitle = pickLang('增幅裝置排行', 'Augment Ranking');
        const singleItemTitle = pickLang('單件裝備強度', 'Single Item Strength');
        const singleItemMeta = pickLang('六格中出過就計入；由強到弱，右滑看更多', 'counts any final-slot item; strongest first, swipe for more');
        const singleItemBadTitle = pickLang('常見但不推薦', 'Common Traps');
        const singleItemBadMeta = pickLang('負 lift 但仍常見；選取率 ≥ 10% 一律列出', 'negative-lift items people still build; pick ≥ 10% always listed');
        const topRows = RARITIES.map(r => buildRarityRow(top[r.key], 'ranked', r)).join('');
        const pairs = info.pairs || [];
        const mateLimit = isMobileViewport() ? MATE_LIST_LIMIT_MOBILE : MATE_LIST_LIMIT_DESKTOP;
        const mateTop = pairs.slice(0, mateLimit);
        const mateBot = [...pairs].slice(-mateLimit).reverse();
        const buildMateCard = (entry, kind) => {
            const mate = DATA.champs[String(entry.id)];
            const name = mate ? champName(mate, entry.id) : ('#' + entry.id);
            const image = mate && mate.image ? mate.image : '';
            const zText = `${entry.z >= 0 ? '+' : ''}${entry.z.toFixed(2)}`;
            const expectedText = entry.expected !== undefined ? copy.expected(pct(entry.expected)) : '';
            const titleAttr = copy.mateTitle(name, pct(entry.wr), expectedText, signed(entry.lift), zText, entry.g);
            return `
                <div class="mate-card ${kind}" title="${escHtml(titleAttr)}">
                    ${image ? `<img loading="lazy" src="${image}" alt="">` : '<div style="width:42px;height:42px;border-radius:8px;background:#2a3142"></div>'}
                    <div>
                        <div class="mname">${escHtml(name)}</div>
                        <div class="mwr">${pct(entry.wr)}</div>
                        <div class="mmeta">${copy.mateMetaHtml(signed(entry.lift), zText, entry.g)}</div>
                    </div>
                </div>
            `;
        };
        const buildMateList = (items, kind) => {
            if (!items.length) return `<div class="mate-list empty-list">${copy.insufficient}</div>`;
            return `<div class="mate-list">${items.map(entry => buildMateCard(entry, kind)).join('')}</div>`;
        };
        const buildSetSummary = (rows, bad = false) => {
            const visibleSets = rows
                .filter(entry => {
                    const metric = bad ? (entry.badScore ?? entry.res) : (entry.score ?? entry.res);
                    return bad ? metric <= -SET_RESIDUAL_THRESHOLD : metric >= SET_RESIDUAL_THRESHOLD;
                })
                .slice(0, 3);
            if (!visibleSets.length) return '';
            const titleAttr = visibleSets.map(entry => {
                const name = setEntryName(entry);
                const metric = bad ? (entry.badScore ?? entry.res) : (entry.score ?? entry.res);
                return `${name} score ${signed(metric)}, residual ${signed(entry.res)}, lift ${signed(entry.lift)}, set avg ${signed(entry.avg)}, WR ${pct(entry.wr)}, ${entry.g} games`;
            }).join('\\n');
            return `
                <div class="aug-set-summary ${bad ? 'bad' : ''}" title="${escHtml(titleAttr)}">
                    ${visibleSets.map(entry => `<span class="sum-item">${escHtml(setEntryName(entry))}</span>`).join('')}
                </div>
            `;
        };
        const buildFitChip = (entry, kind) => {
            const pairItems = Array.isArray(entry.items) ? entry.items : [];
            // Item chips carry .items; set chips don't — pick the right name helper.
            const name = pairItems.length ? itemRowDisplayName(entry) : setEntryName(entry);
            const score = kind === 'bad' ? (entry.badScore ?? entry.res) : (entry.score ?? entry.res);
            const titleAttr = copy.setTitle(name, signed(entry.res), signed(entry.lift), signed(entry.avg), pct(entry.wr), entry.g);
            const liftValue = Number(entry.lift ?? entry.res ?? 0);
            const liftClass = liftValue > 0.0005 ? 'is-good' : (liftValue < -0.0005 ? 'is-bad' : 'is-even');
            const itemTitle = copy.itemBuildTitle(name, pct(entry.pick || 0), signed(liftValue));
            const itemIcons = pairItems.length
                ? `<span class="item-pair-icons">${pairItems.map(item => `
                    <span class="item-pair-icon-wrap">
                        ${item.icon ? `<img src="${escHtml(item.icon)}" alt="" loading="lazy">` : ''}
                    </span>
                `).join('')}</span>`
                : '';
            if (pairItems.length) {
                const tipHtml = buildItemTipHtml({
                    name,
                    items: pairItems.slice(0, 2),
                    wr: entry.wr != null ? pct(entry.wr || 0) : '',
                    pick: pct(entry.pick || 0),
                    pickRate: Number(entry.pick || 0),
                    lift: liftValue,
                    liftLabel: signed(liftValue),
                    games: entry.g || 0,
                });
                return `
                    <span class="fit-chip-wrap has-item-tip" tabindex="0" aria-label="${escHtml(itemTitle)}">
                        <span class="fit-chip ${kind} item-build-chip">
                            ${itemIcons}<span class="fit-chip-label">${escHtml(name)}</span>
                        </span>
                        ${itemTipSource(tipHtml)}
                    </span>
                `;
            }
            return `
                <span class="fit-chip ${kind}" title="${escHtml(`${name} ${signed(score)} · ${titleAttr}`)}">
                    ${itemIcons}<span class="fit-chip-label">${escHtml(name)}</span>
                </span>
            `;
        };
        const buildFitList = (rows, kind) => {
            if (!rows || !rows.length) return `<div class="mate-list empty-list">${copy.insufficient}</div>`;
            return `<div class="fit-chip-list">${rows.slice(0, 3).map(entry => buildFitChip(entry, kind)).join('')}</div>`;
        };
        const buildItemCard = (entry, options = {}) => {
            const name = itemRowDisplayName(entry);
            const pairItems = Array.isArray(entry.items) ? entry.items : [];
            const liftValue = Number(entry.lift ?? entry.res ?? 0);
            const titleForItemCard = copy.itemBuildCardTitle || ((itemName, wr, pick, lift, games) => (
                currentLang === 'en'
                    ? `${itemName} · WR ${wr} · pick ${pick} · lift ${lift} · ${games} games`
                    : `${itemName} · WR ${wr} · 選取率 ${pick} · 勝率 ${lift} · ${games} 場`
            ));
            const matchText = entrySearchText(entry);

            if (options.itemCluster) {
                // Stats live on the 3 core items; the flex tail is shown desaturated.
                const lane = String(entry.lane || '');
                const laneInfo = (copy.itemClusterLanes && copy.itemClusterLanes[lane]) || null;
                const games = Number(entry.g || 0);
                const confirm = Number(entry.exactGames || 0);
                const pickVal = Number(entry.pick || 0);
                const wrSign = liftValue > 0.005 ? 'is-good' : (liftValue < -0.005 ? 'is-bad' : 'is-even');
                const pickHeat = pickHeatClass(pickVal);
                const clusterTitle = copy.itemClusterCardTitle
                    ? copy.itemClusterCardTitle(name, pct(entry.wr || 0), pct(pickVal), signed(liftValue), games, confirm, laneInfo ? laneInfo.label : lane)
                    : titleForItemCard(name, pct(entry.wr || 0), pct(pickVal), signed(liftValue), games);
                // Tip focuses on the coloured core items; flex slots stay as names only.
                const coreItems = pairItems.filter(it => it && it.core !== false).slice(0, 3);
                const tipItems = coreItems.length ? coreItems : pairItems.slice(0, 3);
                const tipHtml = buildItemTipHtml({
                    name,
                    items: tipItems,
                    wr: pct(entry.wr || 0),
                    pick: pct(pickVal),
                    pickRate: pickVal,
                    lift: liftValue,
                    liftLabel: signed(liftValue),
                    games,
                    confirm,
                    lane: laneInfo ? laneInfo.label : lane,
                });
                const icons = Array.from({ length: 6 }, (_, i) => {
                    const item = pairItems[i];
                    const flexClass = item && item.core === false ? ' is-flex' : '';
                    if (item && item.icon) {
                        return `<img class="item-build-icon${flexClass}" src="${escHtml(item.icon)}" alt="" loading="lazy">`;
                    }
                    return `<span class="item-build-icon${flexClass}"></span>`;
                }).join('');
                const laneBadge = laneInfo ? `<span class="lane-badge lane-${lane}">${escHtml(laneInfo.label)}</span>` : '';
                const gamesText = copy.itemClusterGames ? copy.itemClusterGames(games) : `${games}`;
                const pickText = copy.itemClusterPick ? copy.itemClusterPick(pct(pickVal)) : pct(pickVal);
                return `
                    <div class="item-build-card item-cluster-card lane-${lane} has-item-tip" tabindex="0" data-match-text="${escHtml(matchText)}" aria-label="${escHtml(clusterTitle)}">
                        <div class="item-cluster-top">${laneBadge}<span class="cluster-games">${escHtml(gamesText)}</span></div>
                        <div class="item-build-icons">${icons}</div>
                        <div class="item-cluster-stats">
                            <span class="item-build-wr ${wrSign}">${pct(entry.wr || 0)}</span>
                            <span class="cluster-pick ${pickHeat}">${escHtml(pickText)}</span>
                        </div>
                        <div class="item-build-name"><span>${escHtml(name)}</span></div>
                        ${itemTipSource(tipHtml)}
                    </div>
                `;
            }

            const titleAttr = titleForItemCard(
                name,
                pct(entry.wr || 0),
                pct(entry.pick || 0),
                signed(liftValue),
                entry.g || 0,
            );
            const iconLimit = options.singleItem ? 1 : 2;
            const tipItems = pairItems.slice(0, iconLimit);
            const pickVal = Number(entry.pick || 0);
            const pickHeat = pickHeatClass(pickVal);
            const tipHtml = buildItemTipHtml({
                name,
                items: tipItems,
                wr: pct(entry.wr || 0),
                pick: pct(entry.pick || 0),
                pickRate: pickVal,
                lift: liftValue,
                liftLabel: signed(liftValue),
                games: entry.g || 0,
            });
            const icons = tipItems.map(item => (
                item.icon
                    ? `<img class="item-build-icon" src="${escHtml(item.icon)}" alt="" loading="lazy">`
                    : '<span class="item-build-icon"></span>'
            )).join('');
            const placeholderCount = options.singleItem ? 1 : 2;
            const paddedIcons = icons || Array.from(
                { length: placeholderCount },
                () => '<span class="item-build-icon"></span>'
            ).join('');
            const cardClass = options.singleItem ? 'item-build-card single-item-card' : 'item-build-card';
            // Color WR vs champion baseline (lift), not absolute WR — a "top" pair
            // can still sit under the champ's overall WR (e.g. popular traps).
            const wrSign = liftValue > 0.005 ? 'is-good'
                : (liftValue < -0.005 ? 'is-bad' : 'is-even');
            const wrClass = `item-build-wr ${wrSign}`;
            // Filter attrs only on the strength carousel (not 常見但不推薦 traps).
            let filterAttrs = '';
            if (options.singleItem && options.singleItemFilterable) {
                const iid = pairItems[0] && pairItems[0].id != null ? pairItems[0].id : (entry.slug || '');
                const roles = itemFilterRolesForId(iid).join(' ');
                filterAttrs = ` data-item-role="${escHtml(roles)}" data-item-pick="${pickVal}"`;
            }
            return `
                <div class="${cardClass} has-item-tip" tabindex="0" data-match-text="${escHtml(matchText)}" aria-label="${escHtml(titleAttr)}"${filterAttrs}>
                    <div class="item-build-icons">${paddedIcons}</div>
                    <div class="${wrClass}">${pct(entry.wr || 0)}</div>
                    <div class="item-build-pick ${pickHeat}">${pct(pickVal)}</div>
                    <div class="item-build-name"><span>${escHtml(name)}</span></div>
                    ${itemTipSource(tipHtml)}
                </div>
            `;
        };
        const buildItemCarousel = (rows, options = {}) => {
            if (!rows || !rows.length) return `<div class="mate-list empty-list">${copy.insufficient}</div>`;
            const carouselClasses = ['item-build-carousel'];
            if (options.itemCluster) {
                carouselClasses.push('item-build-grid', 'item-cluster-grid');
            } else if (options.itemPairGrid) {
                carouselClasses.push('item-build-grid', 'item-pair-grid');
            } else if (options.singleItem && !options.bootItem) {
                carouselClasses.push('item-build-grid', 'single-item-grid');
            }
            const carouselClass = carouselClasses.join(' ');
            const cards = rows.map(entry => buildItemCard(entry, options)).join('');
            if (options.singleItemFilterable) {
                return `<div class="single-item-filter-host">${buildSingleItemFilterChips()}<div class="${carouselClass}">${cards}</div></div>`;
            }
            return `<div class="${carouselClass}">${cards}</div>`;
        };
        const buildItemSectionFromRows = (title, meta, rows, options = {}) => {
            if (!rows || !rows.length) return '';
            const metaHtml = meta ? `<span class="section-meta">${meta}</span>` : '';
            return `
                <div class="detail-section">
                    <div class="detail-section-head">
                        <h3>${title}</h3>
                        ${metaHtml}
                    </div>
                    ${buildItemCarousel(rows, options)}
                </div>
            `;
        };
        // Common traps: default top-N by pick, but force-keep every negative-lift
        // item with pick ≥ 10% (e.g. Malzahar Void Staff at ~11.6% would otherwise
        // fall off a maxRows=4 list behind hotter traps like Shadowflame / Deathcap).
        const COMMON_TRAP_FORCE_MIN_PICK = 0.10;
        const selectCommonTrapRows = (payload, maxRows = 4) => {
            const sourceRows = (payload && (payload.popularBad || payload.bot)) || [];
            const badRows = sourceRows
                .filter(entry => Number(entry.lift ?? 0) <= -0.01);
            if (!badRows.length) return [];
            const pickOf = (entry) => Number(entry.pick ?? entry.pick_rate ?? 0);
            const gamesOf = (entry) => Number(entry.g ?? entry.games ?? 0);
            const sortByPick = (a, b) => (
                pickOf(b) - pickOf(a)
                || gamesOf(b) - gamesOf(a)
                || Number(a.lift ?? 0) - Number(b.lift ?? 0)
                || String(a.name_en || '').localeCompare(String(b.name_en || ''))
            );
            const mustKeep = badRows
                .filter(entry => pickOf(entry) >= COMMON_TRAP_FORCE_MIN_PICK)
                .sort(sortByPick);
            const optional = badRows
                .filter(entry => pickOf(entry) < COMMON_TRAP_FORCE_MIN_PICK)
                .sort(sortByPick);
            // Expand past maxRows when force-keeps alone exceed it.
            const limit = Math.max(maxRows, mustKeep.length);
            const fill = Math.max(0, limit - mustKeep.length);
            return mustKeep.concat(optional.slice(0, fill));
        };
        const closeFitRows = (rows, minRows = 1, maxRows = 3, options = {}) => {
            if (!rows || !rows.length) return [];
            const topScore = rows[0].score ?? rows[0].res ?? 0;
            const closeGap = 0.004;
            const selected = [];
            const rowKey = (entry) => String(
                entry.slug ?? entry.name_zh ?? entry.name_en ?? entry.name ?? selected.length
            );
            const selectedSlugs = new Set();
            const addSelected = (entry) => {
                const key = rowKey(entry);
                if (selectedSlugs.has(key)) return false;
                selected.push(entry);
                selectedSlugs.add(key);
                return true;
            };
            rows.forEach((entry, idx) => {
                if (selected.length >= maxRows) return;
                const score = entry.score ?? entry.res ?? topScore;
                if (idx === 0 || (topScore - score) <= closeGap) {
                    addSelected(entry);
                }
            });
            if (options.includeHighestPick) {
                const highestPickRow = rows.reduce((best, entry) => {
                    const entryPick = Number(entry.pick ?? entry.pick_rate ?? 0);
                    const bestPick = Number(best.pick ?? best.pick_rate ?? 0);
                    if (entryPick !== bestPick) return entryPick > bestPick ? entry : best;
                    const entryGames = Number(entry.g ?? entry.games ?? 0);
                    const bestGames = Number(best.g ?? best.games ?? 0);
                    if (entryGames !== bestGames) return entryGames > bestGames ? entry : best;
                    const entryScore = Number(entry.score ?? entry.res ?? 0);
                    const bestScore = Number(best.score ?? best.res ?? 0);
                    return entryScore > bestScore ? entry : best;
                }, rows[0]);
                const highestPickKey = rowKey(highestPickRow);
                if (!selectedSlugs.has(highestPickKey)) {
                    if (selected.length < maxRows) {
                        addSelected(highestPickRow);
                    } else if (selected.length) {
                        selected[selected.length - 1] = highestPickRow;
                        selectedSlugs.clear();
                        selected.forEach(entry => selectedSlugs.add(rowKey(entry)));
                    }
                }
            }
            if (selected.length < minRows) {
                for (const entry of rows) {
                    if (selected.length >= minRows || selected.length >= maxRows) break;
                    addSelected(entry);
                }
            }
            return selected;
        };
        // First-two item pairs: payload often ships 50–100+ rows (incl. 0.1% noise).
        // Strength-first, but force-keep the most-picked routes so meta isn't buried.
        const ITEM_PAIR_MAX_ROWS = 8;
        const ITEM_PAIR_MIN_PICK = 0.01;       // prefer ≥1% pick
        const ITEM_PAIR_MIN_PICK_FALLBACK = 0.005; // then ≥0.5%
        const ITEM_PAIR_GUARANTEE_PICK = 2;    // always include top-N by pick among the pool
        const selectItemPairRows = (payload) => {
            const rows = (payload && payload.top) || [];
            if (!rows.length) return [];
            const pickOf = (e) => Number(e.pick || 0);
            const keyOf = (e) => {
                if (e.slug != null && e.slug !== '') return `s:${e.slug}`;
                const ids = Array.isArray(e.items)
                    ? e.items.map(it => String(it && (it.id != null ? it.id : it.name || ''))).join('+')
                    : '';
                if (ids) return `i:${ids}`;
                return `n:${e.name_zh || e.name_en || e.name || ''}|${pickOf(e)}|${e.g || 0}`;
            };
            let pool = rows.filter(e => pickOf(e) >= ITEM_PAIR_MIN_PICK);
            if (pool.length < 4) {
                pool = rows.filter(e => pickOf(e) >= ITEM_PAIR_MIN_PICK_FALLBACK);
            }
            if (pool.length < 3) pool = rows.slice();
            // pool keeps payload strength order.
            const selected = [];
            const seen = new Set();
            const add = (e) => {
                const k = keyOf(e);
                if (seen.has(k)) return false;
                seen.add(k);
                selected.push(e);
                return true;
            };
            // Guarantee highest-pick routes among the pool (usually meta openers).
            const byPick = pool.slice().sort((a, b) => (
                pickOf(b) - pickOf(a)
                || (Number(b.g || 0) - Number(a.g || 0))
            ));
            for (const e of byPick.slice(0, ITEM_PAIR_GUARANTEE_PICK)) add(e);
            // Fill remaining slots by strength order.
            for (const e of pool) {
                if (selected.length >= ITEM_PAIR_MAX_ROWS) break;
                add(e);
            }
            // Display still strongest-first (popular-but-weaker land later in the 8).
            const strengthRank = new Map(pool.map((e, i) => [keyOf(e), i]));
            selected.sort((a, b) => (
                (strengthRank.get(keyOf(a)) ?? 999) - (strengthRank.get(keyOf(b)) ?? 999)
            ));
            return selected;
        };
        const buildAffinitySection = (title, meta, payload, options = {}) => {
            if (options.itemCarousel) {
                let rows = (payload && payload.top) || [];
                if (options.itemPairGrid) rows = selectItemPairRows(payload);
                if (!rows.length) return '';
                const pairMeta = pickLang(
                    '不含鞋子；選取 ≥1% · 最多 8 組 · 強度為主並保留最高出場',
                    'boots excluded; pick ≥1% · up to 8 · strength first, keep top pick',
                );
                const itemMeta = pickLang('不含鞋子；勝率分數由高到低，右滑看更多', 'boots excluded; strongest first, swipe for more');
                const displayMeta = options.itemPairGrid
                    ? pairMeta
                    : ((options.singleItem || options.itemCluster) && meta ? meta : itemMeta);
                const metaHtml = `<span class="section-meta">${displayMeta}</span>`;
                const filterable = Boolean(options.singleItem && options.singleItemFilterable);
                return `
                    <div class="detail-section">
                        <div class="detail-section-head">
                            <h3>${title}</h3>
                            ${metaHtml}
                        </div>
                        ${buildItemCarousel(rows, {
                            singleItem: Boolean(options.singleItem),
                            singleItemFilterable: filterable,
                            itemCluster: Boolean(options.itemCluster),
                            itemPairGrid: Boolean(options.itemPairGrid),
                        })}
                    </div>
                `;
            }
            const bestRows = closeFitRows(
                (payload && payload.top) || [],
                options.minRows || 1,
                options.maxRows || 3,
                { includeHighestPick: Boolean(options.includeHighestPick) },
            );
            if (!bestRows.length) return '';
            const metaHtml = meta ? `<span class="section-meta">${meta}</span>` : '';
            return `
                <div class="detail-section">
                    <div class="detail-section-head">
                        <h3>${title}</h3>
                        ${metaHtml}
                    </div>
                    ${buildFitList(bestRows, 'good')}
                </div>
            `;
        };
        const emptyDetailSection = (title, meta = '') => `
            <div class="detail-section">
                <div class="detail-section-head">
                    <h3>${title}</h3>
                    ${meta ? `<span class="section-meta">${meta}</span>` : ''}
                </div>
                <div class="mate-list empty-list">${copy.insufficient}</div>
            </div>
        `;
        const buildCoreGroupSection = (title, meta, info) => {
            const groups = (info && info.groups) || [];
            if (!groups.length) return emptyDetailSection(title, meta);
            const laneLabels = copy.itemClusterLanes || {};
            const iconImg = (item, cls) => {
                const nm = escHtml(itemDisplayName(item));
                return (item && itemIconUrl(item))
                    ? `<img class="${cls}" src="${escHtml(itemIconUrl(item))}" alt="${nm}" loading="lazy">`
                    : `<span class="${cls}"></span>`;
            };
            const blocks = groups.map(grp => {
                const core = Array.isArray(grp.core) ? grp.core : [];
                const coreIcons = core.map(it => {
                    const nm = itemDisplayName(it);
                    const tipHtml = buildItemTipHtml({ name: nm, items: [it] });
                    return `<span class="cg-icon-host has-item-tip" tabindex="0" aria-label="${escHtml(nm)}">${iconImg(it, 'cg-core-icon')}${itemTipSource(tipHtml)}</span>`;
                }).join('<span class="cg-arrow">▸</span>');
                const options = (Array.isArray(grp.options) ? grp.options : []).map(o => {
                    const lift = Number(o.lift || 0);
                    const wrSign = lift > 0.005 ? 'is-good' : (lift < -0.005 ? 'is-bad' : 'is-even');
                    const pickVal = Number(o.pick || 0);
                    const pickHeat = pickHeatClass(pickVal);
                    const laneInfo = laneLabels[o.lane];
                    const badge = laneInfo ? `<span class="lane-badge lane-${o.lane}">${escHtml(laneInfo.label)}</span>` : '';
                    const optName = itemDisplayName(o) || o.name_zh || o.name || '';
                    const tip = copy.itemClusterCardTitle
                        ? copy.itemClusterCardTitle(optName, pct(o.wr || 0), pct(pickVal), signed(lift), o.g || 0, o.exactGames || 0, laneInfo ? laneInfo.label : (o.lane || ''))
                        : optName;
                    const tipHtml = buildItemTipHtml({
                        name: optName,
                        items: [o],
                        wr: pct(o.wr || 0),
                        pick: pct(pickVal),
                        pickRate: pickVal,
                        lift,
                        liftLabel: signed(lift),
                        games: o.g || 0,
                        confirm: o.exactGames || 0,
                        lane: laneInfo ? laneInfo.label : (o.lane || ''),
                    });
                    return `
                        <div class="cg-option has-item-tip" tabindex="0" aria-label="${escHtml(tip)}">
                            ${iconImg(o, 'cg-option-icon')}
                            <span class="cg-option-wr ${wrSign}">${pct(o.wr || 0)}</span>
                            <span class="cg-option-pick ${pickHeat}">${pct(pickVal)}</span>
                            ${badge}
                            ${itemTipSource(tipHtml)}
                        </div>`;
                }).join('');
                const tail = Array.isArray(grp.tail) ? grp.tail : [];
                const tailBlock = tail.length
                    ? `<div class="cg-tail"><span class="cg-tail-label" title="${escHtml(copy.coreBuildTailTip || '')}">${copy.coreBuildTail || '收尾'}</span>${tail.map(it => {
                        const nm = itemDisplayName(it);
                        const tipHtml = buildItemTipHtml({
                            name: nm,
                            items: [it],
                            note: copy.coreBuildTailTip || '',
                        });
                        return `<span class="cg-icon-host has-item-tip" tabindex="0" aria-label="${escHtml(nm)}">${iconImg(it, 'cg-tail-icon')}${itemTipSource(tipHtml)}</span>`;
                    }).join('')}</div>`
                    : '';
                const share = copy.coreBuildShare ? copy.coreBuildShare(pct(grp.pick || 0)) : pct(grp.pick || 0);
                const coreLift = Number(grp.lift || 0);
                const coreWrSign = coreLift > 0.005 ? 'is-good' : (coreLift < -0.005 ? 'is-bad' : 'is-even');
                const wrText = copy.coreBuildWr ? copy.coreBuildWr(pct(grp.wr || 0)) : pct(grp.wr || 0);
                const wrHtml = grp.wr ? `<span class="cg-core-wr ${coreWrSign}">${escHtml(wrText)}</span>` : '';
                const coreGroupName = (() => {
                    if (currentLang === 'en') return grp.name_en || grp.name || grp.name_zh || '';
                    if (currentLang === 'zh-CN') {
                        const parts = core.map(it => itemDisplayName(it)).filter(Boolean);
                        if (parts.length) return parts.join(' + ');
                        return t2s(grp.name_zh || grp.name || '');
                    }
                    return grp.name_zh || grp.name || '';
                })();
                const headTitle = copy.coreBuildHeadTitle
                    ? copy.coreBuildHeadTitle(coreGroupName, pct(grp.wr || 0), signed(coreLift), pct(grp.pick || 0), grp.g || 0)
                    : '';
                const corePickVal = Number(grp.pick || 0);
                const headTipHtml = buildItemTipHtml({
                    name: coreGroupName,
                    items: core,
                    wr: grp.wr != null ? pct(grp.wr || 0) : '',
                    pick: pct(grp.pick || 0),
                    pickRate: corePickVal,
                    lift: coreLift,
                    liftLabel: signed(coreLift),
                    games: grp.g || 0,
                });
                return `
                    <div class="core-group">
                        <div class="cg-head has-item-tip" tabindex="0"${headTitle ? ` aria-label="${escHtml(headTitle)}"` : ''}>
                            <div class="cg-core">${coreIcons}</div>
                            <div class="cg-core-meta">
                                ${wrHtml}
                                <span class="cg-core-share ${pickHeatClass(corePickVal)}">${escHtml(share)}</span>
                            </div>
                            ${itemTipSource(headTipHtml)}
                        </div>
                        <div class="cg-options-label">${copy.coreBuildThird || '搭配裝備'}</div>
                        <div class="cg-options">${options}</div>
                        ${tailBlock}
                    </div>`;
            }).join('');
            const metaHtml = meta ? `<span class="section-meta">${meta}</span>` : '';
            const titleHtml = title ? `<h3>${title}</h3>` : '';
            const headHtml = (titleHtml || metaHtml)
                ? `<div class="detail-section-head">${titleHtml}${metaHtml}</div>`
                : '';
            return `
                <div class="detail-section">
                    ${headHtml}
                    <div class="core-group-list">${blocks}</div>
                </div>`;
        };
        const buildItemPanel = (title, meta, payload, options = {}) => (
            buildAffinitySection(title, meta, payload, options) || emptyDetailSection(title, meta)
        );
        // Compact vertical boots list for the Overview right rail (fills the
        // space beside the build routes; champion-level, not per-route).
        const buildBootRail = (title, meta, payload, opts = {}) => {
            const rows = (payload && payload.top) || [];
            if (!rows.length) return emptyDetailSection(title, meta);
            const railLimit = opts.limit || 4;
            const railExtraClass = opts.extraClass ? ` ${opts.extraClass}` : '';
            const tipFn = copy.itemBuildCardTitle || ((itemName, wr, pick, lift, games) => (
                currentLang === 'en'
                    ? `${itemName} · WR ${wr} · pick ${pick} · lift ${lift} · ${games} games`
                    : `${itemName} · WR ${wr} · 選取率 ${pick} · 勝率 ${lift} · ${games} 場`
            ));
            const items = rows.slice(0, railLimit).map((entry, idx) => {
                const name = itemRowDisplayName(entry);
                const pairItems = Array.isArray(entry.items) ? entry.items : [];
                const icon = pairItems[0] && pairItems[0].icon;
                const wr = Number(entry.wr || 0);
                const liftValue = Number(entry.lift ?? entry.res ?? 0);
                const wrSign = liftValue > 0.005 ? 'is-good' : (liftValue < -0.005 ? 'is-bad' : 'is-even');
                const pickVal = Number(entry.pick || 0);
                const pickHeat = pickHeatClass(pickVal);
                const tip = tipFn(name, pct(wr), pct(pickVal), signed(liftValue), entry.g || 0);
                const tipHtml = buildItemTipHtml({
                    name,
                    items: pairItems.slice(0, 1),
                    wr: pct(wr),
                    pick: pct(pickVal),
                    pickRate: pickVal,
                    lift: liftValue,
                    liftLabel: signed(liftValue),
                    games: entry.g || 0,
                });
                return `
                    <div class="boot-rail-row has-item-tip${idx === 0 ? ' is-top' : ''}" tabindex="0" data-match-text="${escHtml(entrySearchText(entry))}" aria-label="${escHtml(tip)}">
                        ${icon ? `<img class="boot-rail-icon" src="${escHtml(icon)}" alt="" loading="lazy">` : '<span class="boot-rail-icon"></span>'}
                        <span class="boot-rail-name">${escHtml(name)}</span>
                        <span class="boot-rail-wr ${wrSign}">${pct(wr)}</span>
                        <span class="boot-rail-pick ${pickHeat}">${pct(pickVal)}</span>
                        ${itemTipSource(tipHtml)}
                    </div>`;
            }).join('');
            const metaHtml = meta ? `<span class="section-meta">${meta}</span>` : '';
            return `
                <div class="detail-section boot-rail-section${railExtraClass}">
                    <div class="detail-section-head"><h3>${title}</h3>${metaHtml}</div>
                    <div class="boot-rail">${items}</div>
                </div>`;
        };
        const buildSingleItemPanel = (title, meta, payload) => {
            const goodSection = buildAffinitySection(title, meta, payload, {
                itemCarousel: true,
                singleItem: true,
                singleItemFilterable: true,
            });
            const badSection = buildItemSectionFromRows(
                singleItemBadTitle,
                singleItemBadMeta,
                selectCommonTrapRows(payload, 4),
                { singleItem: true },
            );
            if (!goodSection && !badSection) {
                return emptyDetailSection(title, meta);
            }
            return `${goodSection || ''}${badSection || ''}`;
        };
        const buildDetailTabSet = (scope, tabs, extraClass = '', stickyLeadHtml = '') => {
            const name = `detail-${scope}-${cid}`;
            const inputs = tabs.map((tab, idx) => {
                const inputId = `${name}-${tab.key}`;
                return `<input class="detail-tab-input" type="radio" id="${inputId}" name="${name}" ${idx === 0 ? 'checked' : ''} aria-label="${escHtml(tab.label)}">`;
            }).join('');
            const labels = tabs.map(tab => {
                const inputId = `${name}-${tab.key}`;
                return `<label class="detail-tab-label" id="${inputId}-label" role="tab" for="${inputId}">${escHtml(tab.label)}</label>`;
            }).join('');
            const panels = tabs.map(tab => {
                const inputId = `${name}-${tab.key}`;
                return `<section class="detail-tab-panel" role="tabpanel" aria-labelledby="${inputId}-label">${tab.content}</section>`;
            }).join('');
            // Outer .detail-tab-rail is the sticky surface (champ head + tabs for
            // main set).  List keeps overflow-x:auto — overflow on the sticky
            // element itself breaks pin in some browsers.
            return `
                <div class="detail-tabset ${extraClass}">
                    ${inputs}
                    <div class="detail-tab-rail">
                        ${stickyLeadHtml}
                        <div class="detail-tab-list" role="tablist">${labels}</div>
                    </div>
                    <div class="detail-tab-panels">${panels}</div>
                </div>
            `;
        };
        const mainTabLabels = currentLang === 'en'
            ? { overview: 'Overview', items: 'Items', augments: 'Augments', compfit: 'Abilities' }
            : { overview: zhUi('概覽'), items: zhUi('出裝'), augments: zhUi('增幅裝置'), compfit: zhUi('英雄能力') };
        const bootItemTitle = pickLang('推薦鞋子', 'Recommended Boots');
        const bootItemMeta = pickLang('勝率 · 選取率', 'WR · pick');
        const spellRailTitle = pickLang('召喚師技能', 'Summoner Spells');
        // Mayhem players carry two spells, so pick rates sum to ~200% — that
        // domain fact stays in code comments / tips, not the rail chrome.
        const spellRailMeta = pickLang('勝率 · 選取率', 'WR · pick');
        // \u6982\u89bd: headline win-rate, then a two-column split \u2014 build routes
        // on the left, a compact boots rail filling the space on the right.
        const overviewTabContent = `
            <div class="detail-section detail-overview-head">
                <span class="ovr-wr">${pct(info.wr)}</span>
                <span class="ovr-meta">${copy.overviewWrLabel} \u00b7 ${copy.overviewGames(info.g)}</span>
            </div>
            <div class="overview-split">
                <div class="overview-split-main">
                    ${buildCoreGroupSection(
                        copy.itemClusterSectionTitle || '',
                        copy.itemClusterSectionMeta || '',
                        itemClusterInfo,
                    )}
                </div>
                <div class="overview-rail-col">
                    ${buildBootRail(bootItemTitle, bootItemMeta, bootInfo)}
                    ${buildBootRail(spellRailTitle, spellRailMeta, spellInfo, {
                        limit: 5,
                        extraClass: 'spell-rail-section',
                    })}
                </div>
            </div>
        `;
        // \u51fa\u88dd: single items + first-two items stacked (no sub-tabs).
        const itemTabContent = `
            ${buildSingleItemPanel(singleItemTitle, singleItemMeta, singleItemInfo)}
            ${buildItemPanel(
                copy.itemSectionTitle,
                copy.itemSectionMeta,
                itemInfo,
                { itemCarousel: true, itemPairGrid: true },
            )}
        `;
        const compFitTabContent = buildCompFit(info, cid);
        const augmentTabContent = `
            <div class="detail-section">
                <span class="section-meta augment-strength-meta">
                    ${copy.augmentStrengthMeta}
                    <span class="meta-help-wrap">
                        <button class="meta-help" type="button" aria-label="${escHtml(copy.augmentStrengthTip)}">?</button>
                        <span class="meta-help-tip" role="tooltip">${escHtml(copy.augmentStrengthTip)}</span>
                    </span>
                </span>
                <div class="detail-col best">
                    <div class="detail-col-heading">
                        <h3>${augmentRankTitle}</h3>
                        ${buildSetSummary(setTop)}
                    </div>
                    ${buildAugCatChips()}
                    ${topRows}
                </div>
            </div>
            ${buildAffinitySection(copy.augTypeSectionTitle, copy.augTypeSectionMeta, augTypeInfo)}
        `;
        // Champ icon + name live inside the sticky rail with the main tabs so
        // they pin together under the site header (and floating search chip).
        const stickyLeadHtml = `
            <button class="detail-close" type="button" title="${escHtml(copy.detailClose)}" aria-label="${escHtml(copy.detailClose)}">&times;</button>
            <div class="detail-head">
                ${info.image ? `<img class="detail-avatar" loading="lazy" src="${info.image}" alt="">` : ''}
                <span class="cname" id="detail-title-${cid}">${escHtml(champName(info, cid))}</span>
                ${buildDetailRoleTags(info)}
            </div>
        `;
        const detailTabs = buildDetailTabSet('main', [
            { key: 'overview', label: mainTabLabels.overview, content: overviewTabContent },
            { key: 'items', label: mainTabLabels.items, content: itemTabContent },
            { key: 'augments', label: mainTabLabels.augments, content: augmentTabContent },
            { key: 'compfit', label: mainTabLabels.compfit, content: compFitTabContent },
        ], 'detail-main-tabs', stickyLeadHtml);
        return detailTabs;
    }

    const REC_LIST_LIMIT = 12;
    // Full ARAM roster is 5. Under 5 → rank fill-ins; at 5 → evaluate the team.
    const MAX_TEAM_PICKS = 5;
    const TEAM_PAIR_TOTAL = (MAX_TEAM_PICKS * (MAX_TEAM_PICKS - 1)) / 2; // C(5,2)=10
    // Team-eval 6-axis radar + composition rows (roster-level, not per-champ tempo).
    // Order: 前排、輸出、開戰、清兵、續航、控場.
    // (User list had 輸出 twice; 6th axis is 控場 — say if 消耗/poke is preferred.)
    const TEAM_RADAR_AXES = [
        { key: 'front', zh: '前排', en: 'Front' },
        { key: 'damage', zh: '輸出', en: 'Damage' },
        { key: 'engage', zh: '開戰', en: 'Engage' },
        { key: 'wave', zh: '清兵', en: 'Wave' },
        { key: 'sustain', zh: '續航', en: 'Sustain' },
        { key: 'cc', zh: '控場', en: 'CC' },
    ];
    const TEAM_COMP_DIMS = TEAM_RADAR_AXES;
    let detailSelected = null;
    let recommendMode = false; // legacy home teammate mode — always off; Draft tab owns picks
    let recModalOpen = false;
    let teamPicks = []; // ally picks (also used by recommendation helpers)
    let enemyPicks = [];
    let draftSide = 'ally'; // which side the champ list adds to
    let draftView = 'draft'; // central Draft pane or Draft Analysis pane
    let draftRole = '';
    let draftQuery = '';
    let pickNotice = '';

    function zFmt(x) {
        return `${x >= 0 ? '+' : ''}${x.toFixed(2)}`;
    }

    // Find the last .champ in the same visual row as `clicked` (same offsetTop).
    // Tier-grid is a CSS grid so offsetTop tells us the row reliably across
    // viewport widths.
    function lastChampInRow(clicked) {
        const grid = clicked.parentElement;
        const topPx = clicked.offsetTop;
        const champs = grid.querySelectorAll(':scope > .champ');
        let last = clicked;
        for (const c of champs) {
            if (Math.abs(c.offsetTop - topPx) < 2) last = c;
        }
        return last;
    }

    function syncPickDecorations() {
        document.querySelectorAll('.champ').forEach(champ => {
            const cid = champ.getAttribute('data-cid');
            const idx = teamPicks.indexOf(cid);
            champ.classList.toggle('pick-selected', idx !== -1);
            if (idx !== -1) {
                champ.setAttribute('data-pick-rank', String(idx + 1));
            } else {
                champ.removeAttribute('data-pick-rank');
            }
        });
    }

    function adBin(adShare) {
        if (adShare < 0.35) return '<35% AD';
        if (adShare < 0.45) return '35-45% AD';
        if (adShare < 0.55) return '45-55% AD';
        if (adShare < 0.65) return '55-65% AD';
        return '>=65% AD';
    }

    function countGroup(projectedCount) {
        if (projectedCount < 0.5) return '0';
        if (projectedCount < 1.5) return '1';
        return '2+';
    }

    function frontGroup(projectedCount) {
        return countGroup(projectedCount) + ' front';
    }

    function tableValue(name, key) {
        const config = DATA.recommendation_composition || {};
        const tables = config.tables || {};
        const table = tables[name] || {};
        const raw = table[key];
        return Number.isFinite(Number(raw)) ? Number(raw) : 0;
    }

    function teamComposition(ids) {
        const config = DATA.recommendation_composition || {};
        const thresholds = config.lack_thresholds || {};
        const sums = { phys: 0, magic: 0, true: 0, wave: 0, cc: 0, engage: 0, damage: 0, poke: 0, sustain: 0, front: 0 };
        const roles = { Mage: 0, Marksman: 0 };
        let frontCount = 0;
        ids.forEach(rawId => {
            const info = DATA.champs[String(rawId)];
            if (!info) return;
            const comp = info.comp || {};
            Object.keys(sums).forEach(key => {
                sums[key] += Number(comp[key] || 0);
            });
            if (Number(comp.front || 0) >= 2.0) frontCount += 1;
            (info.tags || []).forEach(tag => {
                if (Object.prototype.hasOwnProperty.call(roles, tag)) roles[tag] += 1;
            });
        });

        const size = Math.max(1, ids.length);
        const projection = 5 / size;
        const thresholdScale = size / 5;
        const adDen = sums.phys + sums.magic;
        const adShare = adDen > 0 ? sums.phys / adDen : 0.5;
        const lacks = {};
        ['wave', 'cc', 'engage', 'damage', 'poke', 'sustain', 'front'].forEach(key => {
            const threshold = Number(thresholds[key] || 0);
            lacks[key] = threshold > 0 && sums[key] < threshold * thresholdScale;
        });
        const allLacks = Object.values(lacks).filter(Boolean).length;
        return {
            adShare,
            lacks,
            sums,
            adBin: adBin(adShare),
            frontGroup: frontGroup(frontCount * projection),
            mageGroup: countGroup(roles.Mage * projection),
            marksmanGroup: countGroup(roles.Marksman * projection),
            waveGroup: lacks.wave ? 'wave lack' : 'wave ok',
            engageGroup: lacks.engage ? 'engage lack' : 'engage ok',
            pokeGroup: lacks.poke ? 'poke lack' : 'poke ok',
            allLacksGroup: countGroup(allLacks * projection),
        };
    }

    function teamCompositionScore(ids) {
        if (!ids.length) return 0;
        const config = DATA.recommendation_composition || {};
        const weights = config.table_weights || {};
        const clamp = Number(config.clamp || 0.05);
        const comp = teamComposition(ids);
        let score = 0;
        score += Number(weights.ad_front || 0) * tableValue('ad_front', `${comp.frontGroup}|${comp.adBin}`);
        score += Number(weights.poke_front || 0) * tableValue('poke_front', `${comp.frontGroup}|${comp.pokeGroup}`);
        score += Number(weights.wave_engage || 0) * tableValue('wave_engage', `${comp.waveGroup}|${comp.engageGroup}`);
        score += Number(weights.all_lacks || 0) * tableValue('all_lacks', comp.allLacksGroup);
        score += Number(weights.mage_ad || 0) * tableValue('mage_ad', `${comp.mageGroup}|${comp.adBin}`);
        score += Number(weights.marksman_ad || 0) * tableValue('marksman_ad', `${comp.marksmanGroup}|${comp.adBin}`);
        const sizeWeight = Math.min(1, Math.max(0, (ids.length - 1) / 4));
        return Math.max(-clamp, Math.min(clamp, score)) * sizeWeight;
    }

    function clampAbs(value, maxAbs) {
        return Math.max(-maxAbs, Math.min(maxAbs, value));
    }

    /** Progressive green/red for signed pp-like values (pair lift, 陣容搭配). */
    function signedToneClass(x) {
        const v = Number(x) || 0;
        if (v >= 0.02) return 'tone-pos-2';   // strong green
        if (v >= 0.005) return 'tone-pos-1';  // light green
        if (v > -0.005) return 'tone-zero';   // neutral
        if (v > -0.02) return 'tone-neg-1';   // light red
        return 'tone-neg-2';                  // strong red
    }

    function damageMixScore(comp) {
        const mix = (DATA.recommendation_composition || {}).damage_mix || {};
        const target = Number(mix.target_ad_share || 0.4);
        return -Math.abs(Number(comp.adShare || 0.5) - target);
    }

    function aggregateRecommendations() {
        if (!teamPicks.length) return [];
        const pickedSet = new Set(teamPicks);
        const want = teamPicks.length;
        const compositionConfig = DATA.recommendation_composition || {};
        const compositionWeight = Number(compositionConfig.weight || 0);
        const damageMixConfig = compositionConfig.damage_mix || {};
        const damageMixWeight = Number(damageMixConfig.weight || 0);
        const damageMixClamp = Number(damageMixConfig.clamp || 0.025);
        const beforeComposition = teamCompositionScore(teamPicks);
        const beforeTeamComp = teamComposition(teamPicks);
        const beforeDamageMix = damageMixScore(beforeTeamComp);
        const byCandidate = new Map();
        teamPicks.forEach(anchorId => {
            const info = DATA.champs[anchorId];
            if (!info) return;
            (info.pairs || []).forEach(entry => {
                const candidateId = String(entry.id);
                if (pickedSet.has(candidateId)) return;
                const row = byCandidate.get(candidateId) || {
                    id: candidateId,
                    coverage: 0,
                    zSum: 0,
                    liftSum: 0,
                    wrSum: 0,
                    minGames: Number.POSITIVE_INFINITY,
                };
                row.coverage += 1;
                row.zSum += entry.z;
                row.liftSum += entry.lift;
                row.wrSum += entry.wr;
                row.minGames = Math.min(row.minGames, entry.g);
                byCandidate.set(candidateId, row);
            });
        });
        return [...byCandidate.values()]
            .map(row => {
                const coverageRatio = row.coverage / want;
                const pairFitScore = row.liftSum / want;
                const afterIds = [...teamPicks, row.id];
                const afterTeamComp = teamComposition(afterIds);
                const compositionDelta = teamCompositionScore(afterIds) - beforeComposition;
                const compositionCoverage = 0.5 + 0.5 * coverageRatio;
                const tableContribution = compositionWeight * compositionDelta * compositionCoverage;
                const damageMixDelta = damageMixScore(afterTeamComp) - beforeDamageMix;
                const damageMixContribution = clampAbs(
                    damageMixWeight * damageMixDelta * compositionCoverage,
                    damageMixClamp,
                );
                const compositionContribution = tableContribution + damageMixContribution;
                return {
                    ...row,
                    full: row.coverage === want,
                    coverageRatio,
                    pairFitScore,
                    compositionDelta,
                    tableContribution,
                    damageMixDelta,
                    damageMixContribution,
                    compositionContribution,
                    beforeAdShare: beforeTeamComp.adShare,
                    afterAdShare: afterTeamComp.adShare,
                    beforeFrontGroup: beforeTeamComp.frontGroup,
                    afterFrontGroup: afterTeamComp.frontGroup,
                    beforePokeGroup: beforeTeamComp.pokeGroup,
                    afterPokeGroup: afterTeamComp.pokeGroup,
                    beforeWaveGroup: beforeTeamComp.waveGroup,
                    afterWaveGroup: afterTeamComp.waveGroup,
                    beforeEngageGroup: beforeTeamComp.engageGroup,
                    afterEngageGroup: afterTeamComp.engageGroup,
                    beforeAllLacksGroup: beforeTeamComp.allLacksGroup,
                    afterAllLacksGroup: afterTeamComp.allLacksGroup,
                    fitScore: pairFitScore + compositionContribution,
                    zAvg: row.zSum / row.coverage,
                    liftAvg: row.liftSum / row.coverage,
                    wrAvg: row.wrSum / row.coverage,
                };
            })
            .sort((a, b) =>
                b.fitScore - a.fitScore ||
                b.pairFitScore - a.pairFitScore ||
                b.liftAvg - a.liftAvg ||
                b.zAvg - a.zAvg ||
                Number(b.full) - Number(a.full) ||
                b.coverage - a.coverage ||
                b.minGames - a.minGames
            );
    }

    function recScoreClass(score) {
        if (score >= 0.09) return 'fit-top';
        if (score >= 0.07) return 'fit-strong';
        if (score >= 0.05) return 'fit-solid';
        if (score >= 0.02) return 'fit-soft';
        return 'fit-floor';
    }

    function confidenceLabel(row) {
        const strongCoverage = row.coverageRatio >= 0.75;
        const enoughGames = row.minGames >= 60;
        const signal = Math.abs(row.zAvg || 0);
        if (strongCoverage && enoughGames && signal >= 1.0) {
            return pickLang('可信度高', 'High confidence');
        }
        if (row.coverageRatio >= 0.5 && row.minGames >= 40 && signal >= 0.6) {
            return pickLang('可信度中', 'Medium confidence');
        }
        return pickLang('樣本偏早', 'Early signal');
    }

    function compReasonLabel(row) {
        const value = row.compositionContribution || 0;
        const abs = Math.abs(value);
        const mixValue = row.damageMixContribution || 0;
        if (Math.abs(mixValue) >= 0.004) {
            const addsAD = Number(row.afterAdShare || 0) > Number(row.beforeAdShare || 0);
            if (mixValue > 0) {
                if (currentLang === 'en') return addsAD ? `adds AD ${signed(value)}` : `adds AP ${signed(value)}`;
                return addsAD ? `補AD ${signed(value)}` : `補AP ${signed(value)}`;
            }
            if (currentLang === 'en') return `damage skew ${signed(value)}`;
            return `傷害偏科 ${signed(value)}`;
        }
        if (value > 0.001) {
            if (row.beforeFrontGroup !== row.afterFrontGroup && row.afterFrontGroup !== '0 front') {
                return pickLang(`補前排 ${signed(value)}`, `adds frontline ${signed(value)}`);
            }
            if (row.beforePokeGroup === 'poke lack' && row.afterPokeGroup === 'poke ok') {
                return pickLang(`補Poke ${signed(value)}`, `adds poke ${signed(value)}`);
            }
            if (row.beforeWaveGroup === 'wave lack' && row.afterWaveGroup === 'wave ok') {
                return pickLang(`補清兵 ${signed(value)}`, `adds waveclear ${signed(value)}`);
            }
            if (row.beforeEngageGroup === 'engage lack' && row.afterEngageGroup === 'engage ok') {
                return pickLang(`補開戰 ${signed(value)}`, `adds engage ${signed(value)}`);
            }
            if (row.beforeAllLacksGroup !== row.afterAllLacksGroup) {
                return pickLang(`補陣容 ${signed(value)}`, `rounds team ${signed(value)}`);
            }
        }
        if (abs < 0.001) return pickLang('陣容中性', 'team neutral');
        if (value > 0) return pickLang(`陣容加分 ${signed(value)}`, `team +${(value * 100).toFixed(1)}%`);
        return pickLang(`陣容扣分 ${signed(value)}`, `team ${(value * 100).toFixed(1)}%`);
    }

    function recMetaHtml(row, name) {
        const copy = tr();
        const scoreClass = row.leastFit ? 'fit-worst' : recScoreClass(row.fitScore);
        const scoreLabel = row.leastFit
            ? copy.leastFitLabel
            : (pickLang('推薦度', 'Fit'));
        const confidence = confidenceLabel(row);
        const pairClass = row.pairFitScore >= 0 ? 'good' : 'bad';
        const compClass = row.compositionContribution > 0.001 ? 'good' : (row.compositionContribution < -0.001 ? 'bad' : 'muted');
        const pairLabel = currentLang === 'en'
            ? `pair ${signed(row.pairFitScore)}`
            : `搭配 ${signed(row.pairFitScore)}`;
        return `
            <span class="rec-titleline">
                <span class="rec-name">${escHtml(name)}</span>
                <span class="rec-score ${scoreClass}">${scoreLabel} ${signed(row.fitScore)}</span>
            </span>
            <span class="rec-detail">
                <span class="${pairClass}">${escHtml(pairLabel)}</span>
                <span class="${compClass}">${escHtml(compReasonLabel(row))}</span>
                <span class="muted">${escHtml(confidence)}</span>
            </span>
        `;
    }

    function recommendationDisplayRows(recs) {
        if (!recs.length) return [];
        const rows = recs.slice(0, REC_LIST_LIMIT).map(row => ({ ...row, leastFit: false }));
        if (recs.length <= 1) return rows;

        const worst = { ...recs[recs.length - 1], leastFit: true };
        if (recs.length > REC_LIST_LIMIT) {
            rows[REC_LIST_LIMIT - 1] = worst;
        } else {
            rows[rows.length - 1] = { ...rows[rows.length - 1], leastFit: true };
        }
        return rows;
    }

    /**
     * Look up ordered pair row A→B in champ A's top-pairs list (may be absent —
     * payload only keeps ~24 partners per champ).
     */
    function pairEntry(fromId, toId) {
        const pairs = (DATA.champs[String(fromId)] && DATA.champs[String(fromId)].pairs) || [];
        const want = String(toId);
        for (let k = 0; k < pairs.length; k += 1) {
            if (String(pairs[k].id) === want) return pairs[k];
        }
        return null;
    }

    /**
     * Unordered pair lift for {a,b}. Each champ only stores a short top-pairs
     * list, so A→B and B→A are often missing one side. Prefer the average when
     * both exist; otherwise the single known direction. Order-invariant.
     */
    function pairLiftBetween(aId, bId) {
        const ab = pairEntry(aId, bId);
        const ba = pairEntry(bId, aId);
        if (!ab && !ba) return null;
        if (ab && ba) {
            const lift = (Number(ab.lift || 0) + Number(ba.lift || 0)) / 2;
            const ga = Number(ab.g || 0) || 0;
            const gb = Number(ba.g || 0) || 0;
            const g = ga && gb ? Math.min(ga, gb) : (ga || gb);
            return { lift, g };
        }
        const p = ab || ba;
        return { lift: Number(p.lift || 0), g: Number(p.g || 0) || 0 };
    }

    /**
     * Full 5-man roster evaluation for the side panel.
     * estWr ≈ mean(champ WR) + mean(known pair lifts) + composition table score.
     * Dims = mean capability percentiles across the five champions.
     *
     * Pair lifts are looked up bidirectionally so the same 5-set always scores
     * the same regardless of pick order (each champ only ships ~24 pair rows).
     */
    function evaluateFullTeam(ids) {
        const list = (ids || []).map(String).filter(Boolean);
        let wrSum = 0;
        let wrN = 0;
        const meanComp = {};
        COMP_STAT_KEYS.forEach(k => { meanComp[k] = 0; });
        list.forEach(cid => {
            const info = DATA.champs[cid];
            if (!info) return;
            if (Number.isFinite(Number(info.wr))) {
                wrSum += Number(info.wr);
                wrN += 1;
            }
            const comp = info.comp || {};
            COMP_STAT_KEYS.forEach(k => {
                meanComp[k] += Number(comp[k] || 0);
            });
        });
        const nChamp = Math.max(1, wrN || list.length);
        COMP_STAT_KEYS.forEach(k => { meanComp[k] /= nChamp; });
        const baseWr = wrN ? wrSum / wrN : 0.5;

        let liftSum = 0;
        let pairN = 0;
        let minGames = Number.POSITIVE_INFINITY;
        for (let i = 0; i < list.length; i += 1) {
            for (let j = i + 1; j < list.length; j += 1) {
                const hit = pairLiftBetween(list[i], list[j]);
                if (!hit) continue;
                liftSum += hit.lift;
                pairN += 1;
                if (hit.g > 0) minGames = Math.min(minGames, hit.g);
            }
        }
        const pairLift = pairN ? liftSum / pairN : 0;
        const compositionScore = teamCompositionScore(list);
        const teamComp = teamComposition(list);
        // Soft clamp so noisy pair stacks don't print absurd 70%+ estimates.
        const estRaw = baseWr + pairLift + compositionScore;
        const estWr = Math.max(0.35, Math.min(0.65, estRaw));

        const cap = compCapPct(meanComp);
        // Roster radar: 前排 / 輸出 / 開戰 / 清兵 / 續航 / 控場.
        const abilityAxes = TEAM_RADAR_AXES.map(a => ({
            label: pickLang(a.zh, a.en),
            pct: cap[a.key] || 0,
        }));

        let confKey = 'low';
        if (pairN >= 8 && minGames >= 40) confKey = 'high';
        else if (pairN >= 5 && minGames >= 25) confKey = 'mid';

        // Early/late tempo from mean short-game / long-game WR (not kill-spree snowball).
        const earlyWr = Number(meanComp.early_wr);
        const lateWr = Number(meanComp.late_wr);
        const hasTempoWr = Number.isFinite(earlyWr) && Number.isFinite(lateWr);
        const earlyPct = hasTempoWr ? Math.max(0, Math.min(1, earlyWr)) : 0.5;
        const latePct = hasTempoWr ? Math.max(0, Math.min(1, lateWr)) : 0.5;
        const tempoSum = earlyPct + latePct;
        // Marker 0 = pure early, 1 = pure late; balanced rosters sit near 0.5.
        const tempoPos = tempoSum > 1e-6 ? latePct / tempoSum : 0.5;
        // Absolute WR% points: ~3pp tilt counts as a lean (same cut as champ stage card).
        const TEMPO_CUT = 0.03;
        const tempoDiff = hasTempoWr ? (lateWr - earlyWr) : 0; // + = late-leaning
        let tempoKey = 'balanced';
        if (hasTempoWr && tempoDiff >= TEMPO_CUT) tempoKey = 'late';
        else if (hasTempoWr && tempoDiff <= -TEMPO_CUT) tempoKey = 'early';

        return {
            baseWr,
            pairLift,
            compositionScore,
            estWr,
            pairN,
            pairTotal: TEAM_PAIR_TOTAL,
            minGames: Number.isFinite(minGames) ? minGames : 0,
            confKey,
            abilityAxes,
            teamComp,
            meanComp,
            cap,
            earlyPct,
            latePct,
            tempoPos,
            tempoKey,
        };
    }

    function buildTeamEvalHtml(ids) {
        const copy = tr();
        const ev = evaluateFullTeam(ids);
        const wrTone = ev.estWr >= 0.53 ? 'is-good' : (ev.estWr <= 0.47 ? 'is-bad' : 'is-even');
        const radar = compRadarSvg(ev.abilityAxes, copy.teamDimsTitle);
        // One block: radar + 6 dim bars graded 偏弱/普通/充足 by mean capability pct.
        // Cuts align with bar fill: <40 偏弱 · 40–64 普通 · ≥65 充足.
        const dimRows = TEAM_RADAR_AXES.map(d => {
            const pct01 = Number(ev.cap[d.key] || 0);
            const fill = Math.round(pct01 * 100);
            let grade = 'mid';
            if (pct01 < 0.40) grade = 'bad';
            else if (pct01 >= 0.65) grade = 'good';
            const tag = grade === 'bad' ? copy.teamDimBad
                : grade === 'good' ? copy.teamDimGood
                : copy.teamDimMid;
            return `<div class="team-comp-row is-${grade}">
                <span class="team-comp-name">${escHtml(pickLang(d.zh, d.en))}</span>
                <span class="ab-bar"><span style="width:${fill}%"></span></span>
                <span class="ab-val">${fill}</span>
                <span class="team-comp-tag">${escHtml(tag)}</span>
            </div>`;
        }).join('');
        return `
            <div class="team-eval">
                <div class="team-eval-wr ${wrTone}">
                    <div class="team-wr-num">${pct(ev.estWr)}</div>
                    <div class="team-wr-side">
                        <span class="team-wr-label">${escHtml(copy.teamEstWr)}</span>
                        <span class="team-wr-meta">${escHtml(String(ids.length))}/${MAX_TEAM_PICKS}</span>
                    </div>
                    <div class="team-wr-breakdown">
                        <span>${escHtml(copy.draftChampStrength || copy.teamBaseWr)} ${pct(ev.baseWr)}</span>
                        <span>${escHtml(copy.teamCompAdj)} <b class="team-delta ${signedToneClass(ev.compositionScore)}">${signed(ev.compositionScore)}</b></span>
                    </div>
                </div>
                <div class="team-eval-block">
                    <div class="team-eval-h">${escHtml(copy.teamDimsTitle)}</div>
                    <div class="team-dims-split">
                        <div class="team-eval-radar">${radar}</div>
                        <div class="team-comp-list">${dimRows}</div>
                    </div>
                </div>
            </div>
        `;
    }

    /** Dual horizontal bar: ally (theme accent) vs enemy (gray). value is 0–1 display fraction. */
    function draftCompareBarRow(label, allyFrac, enemyFrac, allyText, enemyText) {
        const a = Math.max(0, Math.min(1, Number(allyFrac) || 0));
        const e = Math.max(0, Math.min(1, Number(enemyFrac) || 0));
        return `
            <div class="draft-cmp-row">
                <span class="draft-cmp-label">${escHtml(label)}</span>
                <div class="draft-cmp-bars">
                    <div class="draft-cmp-line is-ally" title="${escHtml(allyText)}">
                        <div class="draft-cmp-track">
                            <span class="draft-cmp-fill" style="width:${(a * 100).toFixed(1)}%"></span>
                        </div>
                        <span class="draft-cmp-val">${escHtml(allyText)}</span>
                    </div>
                    <div class="draft-cmp-line is-enemy" title="${escHtml(enemyText)}">
                        <div class="draft-cmp-track">
                            <span class="draft-cmp-fill" style="width:${(e * 100).toFixed(1)}%"></span>
                        </div>
                        <span class="draft-cmp-val">${escHtml(enemyText)}</span>
                    </div>
                </div>
            </div>`;
    }

    /**
     * Matchup panel: overlaid dual radar (accent / gray) + dual bars.
     * Works for partial rosters (1–5 each side); composition score scales with size.
     * Bars: champ strength, est WR, composition only (no pair lift / tempo).
     */
    function buildMatchupCompareHtml(allyEv, enemyEv, allyCount, enemyCount) {
        const copy = tr();
        const allyAxes = allyEv.abilityAxes || [];
        const enemyAxes = (enemyEv.abilityAxes || []).map((a, i) => ({
            label: (allyAxes[i] && allyAxes[i].label) || a.label,
            pct: a.pct,
        }));
        // Ally = site theme accent (gold); enemy = gray-white.
        const radar = compRadarOverlaySvg([
            {
                axes: allyAxes,
                stroke: 'var(--accent, #f5c518)',
                fill: 'color-mix(in srgb, var(--accent, #f5c518) 20%, transparent)',
                dot: 'var(--accent, #f5c518)',
            },
            {
                axes: enemyAxes.length ? enemyAxes : allyAxes.map(a => ({ label: a.label, pct: 0 })),
                stroke: 'rgba(196,200,208,0.88)',
                fill: 'rgba(160,166,176,0.14)',
                dot: 'rgba(210,214,220,0.95)',
            },
        ], copy.draftRadarTitle || copy.teamDimsTitle);

        // Signed composition: map ±8pp → 0–1 around 0.5 for bar width.
        const liftFrac = v => Math.max(0, Math.min(1, 0.5 + (Number(v) || 0) / 0.16));
        const liftTxt = v => signed(Number(v) || 0);
        const rows = [
            draftCompareBarRow(
                copy.draftChampStrength || copy.teamBaseWr,
                allyEv.baseWr, enemyEv.baseWr,
                pct(allyEv.baseWr), pct(enemyEv.baseWr),
            ),
            draftCompareBarRow(
                copy.teamEstWr,
                allyEv.estWr, enemyEv.estWr,
                pct(allyEv.estWr), pct(enemyEv.estWr),
            ),
            draftCompareBarRow(
                copy.teamCompAdj,
                liftFrac(allyEv.compositionScore), liftFrac(enemyEv.compositionScore),
                liftTxt(allyEv.compositionScore), liftTxt(enemyEv.compositionScore),
            ),
        ];
        const legAlly = copy.draftLegendAlly || 'Ally';
        const legEnemy = copy.draftLegendEnemy || 'Enemy';

        return `
            <div class="draft-matchup">
                <div class="draft-matchup-radar">
                    <div class="draft-matchup-head">
                        <span class="draft-matchup-title">${escHtml(copy.draftRadarTitle || copy.teamDimsTitle)}</span>
                        <span class="draft-matchup-legend">
                            <span class="draft-leg is-ally"><i></i>${escHtml(legAlly)}</span>
                            <span class="draft-leg is-enemy"><i></i>${escHtml(legEnemy)}</span>
                        </span>
                    </div>
                    <div class="draft-matchup-svg">${radar}</div>
                </div>
                <div class="draft-matchup-bars">
                    <div class="draft-matchup-head">
                        <span class="draft-matchup-title">${escHtml(copy.draftCompareExtras || copy.draftCompareTitle)}</span>
                        <span class="draft-matchup-legend">
                            <span class="draft-leg is-ally"><i></i>${escHtml(legAlly)}</span>
                            <span class="draft-leg is-enemy"><i></i>${escHtml(legEnemy)}</span>
                        </span>
                    </div>
                    <div class="draft-cmp-list">${rows.join('')}</div>
                </div>
            </div>`;
    }

    // ---- Draft final WR: Composition LR (same formula as recommend_gui.predict_matchup_prob) ----
    // Export is built at site-render time from models/composition_lr_pooled_recency_7d.
    // P(ally wins) = sigmoid(logit_ally − logit_enemy + intercept) where each team's
    // logit is the full composition feature vector · coef (identity + team signals).
    let draftFeatureIndex = null;

    function draftGetFeatureIndex(model) {
        if (draftFeatureIndex && draftFeatureIndex.model === model) return draftFeatureIndex.map;
        const map = Object.create(null);
        (model.feature_names || []).forEach((name, idx) => { map[name] = idx; });
        draftFeatureIndex = { model, map };
        return map;
    }

    function draftAdBinIndex(adShare) {
        if (adShare < 0.35) return 0;
        if (adShare < 0.45) return 1;
        if (adShare < 0.55) return 2;
        if (adShare < 0.65) return 3;
        return 4;
    }

    function draftCountGroupIndex(count) {
        if (count <= 0) return 0;
        if (count === 1) return 1;
        return 2;
    }

    function draftTeamProfile(teamIds, model) {
        const profiles = model.profiles || {};
        const scoreCols = (model.meta && model.meta.score_columns) || [];
        const roleCols = (model.meta && model.meta.role_columns) || [];
        const lackThr = (model.meta && model.meta.lack_thresholds) || {};
        const coreCols = (model.meta && model.meta.core_columns) || [];
        let physical = 0;
        let magic = 0;
        let trueDpm = 0;
        const scoreSums = Object.create(null);
        const roles = Object.create(null);
        scoreCols.forEach(name => { scoreSums[name] = 0; });
        roleCols.forEach(role => { roles[role] = 0; });
        const rows = [];
        for (let i = 0; i < teamIds.length; i += 1) {
            const profile = profiles[String(teamIds[i])];
            if (!profile) return null;
            rows.push(profile);
            physical += Number(profile.physical_dpm) || 0;
            magic += Number(profile.magic_dpm) || 0;
            trueDpm += Number(profile.true_dpm) || 0;
            scoreCols.forEach(name => {
                scoreSums[name] += Number((profile.scores || {})[name]) || 0;
            });
            roleCols.forEach(role => {
                roles[role] += Number((profile.roles || {})[role]) || 0;
            });
        }
        const adApDen = Math.max(physical + magic, 1e-9);
        const allDen = Math.max(physical + magic + trueDpm, 1e-9);
        const adShare = physical / adApDen;
        const trueShare = trueDpm / allDen;
        const lacks = Object.create(null);
        scoreCols.forEach(name => {
            lacks[name] = scoreSums[name] < Number(lackThr[name] || 0) ? 1 : 0;
        });
        const frontCount = rows.reduce((n, profile) => (
            n + ((Number((profile.scores || {}).frontline_score) || 0) >= 2.0 ? 1 : 0)
        ), 0);
        return {
            ad_share: adShare,
            true_share: trueShare,
            ad_ap_balance: 1.0 - Math.abs(adShare - (magic / adApDen)),
            front_count: frontCount,
            front_sum: scoreSums.frontline_score || 0,
            score_sums: scoreSums,
            lacks,
            roles,
            core_lacks_count: coreCols.reduce((n, name) => n + (lacks[name] || 0), 0),
            all_lacks_count: scoreCols.reduce((n, name) => n + (lacks[name] || 0), 0),
        };
    }

    /** Single-team composition feature vector · coef (matches recommend.py). */
    function draftTeamLogitContribution(teamIds, model) {
        const featureNames = model.feature_names || [];
        const coef = model.coef || [];
        if (!featureNames.length || coef.length !== featureNames.length) return null;
        const featIdx = draftGetFeatureIndex(model);
        const x = new Float64Array(featureNames.length);
        for (let i = 0; i < teamIds.length; i += 1) {
            const cid = String(teamIds[i]);
            const fIdx = featIdx[`champion:${cid}`];
            if (fIdx === undefined) return null;
            x[fIdx] = 1;
        }
        const team = draftTeamProfile(teamIds, model);
        if (!team) return null;
        const scoreCols = (model.meta && model.meta.score_columns) || [];
        const roleCols = (model.meta && model.meta.role_columns) || [];
        const adBins = (model.meta && model.meta.ad_bins) || [];
        const frontGroups = (model.meta && model.meta.front_groups) || [];
        const waveGroups = (model.meta && model.meta.wave_groups) || [];
        const engageGroups = (model.meta && model.meta.engage_groups) || [];
        const pokeGroups = (model.meta && model.meta.poke_groups) || [];

        const set = (name, value) => {
            const idx = featIdx[name];
            if (idx !== undefined) x[idx] = value;
        };
        set('ad_share', team.ad_share);
        set('ad_ap_balance', team.ad_ap_balance);
        set('true_share', team.true_share);
        set('front_count', team.front_count);
        set('front_sum', team.front_sum);
        set('core_lacks_count', team.core_lacks_count);
        set('all_lacks_count', team.all_lacks_count);
        scoreCols.forEach(name => {
            set(`sum_${name}`, team.score_sums[name] || 0);
            set(`lack_${name}`, team.lacks[name] || 0);
        });
        roleCols.forEach(role => {
            set(`role_${String(role).toLowerCase()}`, team.roles[role] || 0);
        });

        const adBin = adBins[draftAdBinIndex(team.ad_share)];
        const frontGroup = frontGroups[draftCountGroupIndex(team.front_count)];
        const waveGroup = waveGroups[team.lacks.wave_clear_score === 0 ? 1 : 0];
        const engageGroup = engageGroups[team.lacks.engage_score === 0 ? 1 : 0];
        const pokeGroup = pokeGroups[team.lacks.poke_score === 0 ? 1 : 0];
        if (frontGroup && adBin) set(`ad_front:${frontGroup}:${adBin}`, 1);
        if (waveGroup && engageGroup) set(`wave_engage:${waveGroup}:${engageGroup}`, 1);
        if (frontGroup && pokeGroup) set(`poke_front:${frontGroup}:${pokeGroup}`, 1);
        roleCols.forEach(role => {
            if (adBin) set(`role_ad:${adBin}:${String(role).toLowerCase()}`, team.roles[role] || 0);
        });

        let total = 0;
        for (let i = 0; i < coef.length; i += 1) total += x[i] * Number(coef[i] || 0);
        return total;
    }

    /** Full 5v5 Composition LR: sigmoid(ally_logit − enemy_logit + intercept). */
    function draftCompositionLrWinrate(allyIds, enemyIds) {
        const model = DATA && DATA.draftModel;
        if (!model || model.kind !== 'composition_lr') return null;
        if (!Array.isArray(allyIds) || !Array.isArray(enemyIds) || allyIds.length !== 5 || enemyIds.length !== 5) {
            return null;
        }
        const myLogit = draftTeamLogitContribution(allyIds, model);
        const enemyLogit = draftTeamLogitContribution(enemyIds, model);
        if (myLogit == null || enemyLogit == null) return null;
        const logit = myLogit - enemyLogit + Number(model.intercept || 0);
        return 1 / (1 + Math.exp(-logit));
    }

    /** Ally vs optional enemy: team details remain contextual; final WR is Composition LR. */
    function evaluateMatchup(allyIds, enemyIds) {
        const ally = allyIds && allyIds.length ? evaluateFullTeam(allyIds) : null;
        const enemy = enemyIds && enemyIds.length ? evaluateFullTeam(enemyIds) : null;
        const finalWr = draftCompositionLrWinrate(allyIds, enemyIds);
        return { ally, enemy, finalWr };
    }

    function draftPickList(side) {
        return side === 'enemy' ? enemyPicks : teamPicks;
    }

    /**
     * Lock-in bar art: use landscape splash (not tall loading).
     * Loading 308×560 in a short wide bar forces a razor-thin vertical crop that
     * routinely shears heads; splash ~1215×717 matches the bar aspect so the
     * full head can stay in frame.
     * Per-champ crop overrides: docs/api/draft-slot-crops.json (editor at
     * docs/tools/draft-slot-crop.html).
     */
    /** Fandom full HD is often 6–10k px; browsers drop some <img> loads. Cap width. */
    function draftScaleFandomHdUrl(url) {
        const u = String(url || '');
        if (!/static\.wikia\.nocookie\.net/i.test(u)) return u;
        if (/scale-to-width-down/i.test(u)) return u;
        // .../revision/latest?cb=… → .../revision/latest/scale-to-width-down/1600?cb=…
        return u.replace(/\/revision\/latest(?=\?|$)/i, '/revision/latest/scale-to-width-down/1600');
    }

    function draftCdragonSplashUrl(alias) {
        const low = String(alias || '').toLowerCase();
        if (!low) return '';
        return (
            'https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/'
            + `global/default/assets/characters/${low}/skins/base/images/`
            + `${low}_splash_uncentered_0.jpg`
        );
    }

    function draftSlotArtUrl(info, cid) {
        if (!info) return '';
        const alias = String(info.alias || info.name_en || '').replace(/[^A-Za-z0-9]/g, '');
        // 1) Wiki HD (or Universe remote) from docs/api/draft-splash-hd.json
        //    Prefer remote Fandom with scale-to-width-down for reliable bar loads.
        if (DRAFT_SPLASH_HD && DRAFT_SPLASH_HD.byCid) {
            const hit = DRAFT_SPLASH_HD.byCid[String(cid != null ? cid : info.id || '')];
            if (hit && hit.url) {
                const u = String(hit.url);
                if (/^https?:\/\//i.test(u)) return draftScaleFandomHdUrl(u);
                // site-relative path → absolute against origin (not <base href>)
                return `${window.location.origin}/${u.replace(/^\.?\//, '')}`;
            }
        }
        // 2) CommunityDragon uncentered splash (1215×717)
        if (alias) return draftCdragonSplashUrl(alias);
        // 3) Data Dragon fallback
        if (alias) {
            return `https://ddragon.leagueoflegends.com/cdn/img/champion/splash/${alias}_0.jpg`;
        }
        return info.image || '';
    }

    function draftSlotCropFor(cid) {
        const map = DRAFT_SLOT_CROPS;
        const d = (map && map.default) || {};
        const c = (map && map.champs && map.champs[String(cid)]) || {};
        return {
            x: Number(c.x != null ? c.x : d.x),
            y: Number(c.y != null ? c.y : d.y),
            zoom: Number(c.zoom != null ? c.zoom : d.zoom),
            // mirror=true ⇒ source art faces LEFT (editor toggle). Default false = faces RIGHT.
            mirror: !!(c.mirror != null ? c.mirror : d.mirror),
        };
    }

    function draftSlotCropStyle(cid) {
        if (!DRAFT_SLOT_CROPS) return '';
        const { x, y, zoom } = draftSlotCropFor(cid);
        const parts = [];
        if (Number.isFinite(x)) parts.push(`--slot-art-x:${x}%`);
        if (Number.isFinite(y)) parts.push(`--slot-art-y:${y}%`);
        if (Number.isFinite(zoom)) parts.push(`--slot-art-zoom:${zoom}`);
        return parts.length ? ` style="${parts.join(';')}"` : '';
    }

    function renderDraftSlots(side) {
        const host = document.getElementById(side === 'enemy' ? 'draft-enemy-slots' : 'draft-ally-slots');
        if (!host) return;
        const copy = tr();
        const list = draftPickList(side);
        const chips = [];
        list.forEach((cid) => {
            const info = DATA.champs[cid];
            const name = info ? champName(info, cid) : ('#' + cid);
            const art = draftSlotArtUrl(info, cid);
            const icon = info && info.image ? info.image : '';
            const alias = String((info && (info.alias || info.name_en)) || '').replace(/[^A-Za-z0-9]/g, '');
            const cdragonSplash = draftCdragonSplashUrl(alias);
            const ddragonSplash = alias
                ? `https://ddragon.leagueoflegends.com/cdn/img/champion/splash/${alias}_0.jpg`
                : '';
            // Fandom Wiki CDN 404s when Referer is arammeta.com — strip referrer.
            // Cascade: CDragon → DDragon → square icon (skip urls equal to primary).
            const chain = [cdragonSplash, ddragonSplash, icon].filter(
                (u) => u && u !== art
            );
            // Unique preserve order
            const seen = new Set();
            const fbs = [];
            chain.forEach((u) => {
                if (!seen.has(u)) { seen.add(u); fbs.push(u); }
            });
            let onerr = '';
            if (fbs.length) {
                const payload = escHtml(JSON.stringify(fbs));
                onerr = ` data-draft-fb="${payload}" onerror="(function(el){try{var q=JSON.parse(el.getAttribute('data-draft-fb')||'[]');if(!q.length){el.onerror=null;el.classList.add('is-icon-fallback');return;}var n=q.shift();el.setAttribute('data-draft-fb',JSON.stringify(q));if(!q.length)el.classList.add('is-icon-fallback');el.src=n;}catch(e){el.onerror=null;el.classList.add('is-icon-fallback');}})(this)"`;
            }
            const cropStyle = draftSlotCropStyle(cid);
            const mirrored = DRAFT_SLOT_CROPS && draftSlotCropFor(cid).mirror
                ? ' is-source-mirrored' : '';
            chips.push(
                `<button class="draft-slot is-filled" type="button" data-draft-remove="${side}" data-cid="${cid}" `
                + `title="${escHtml(copy.removePick(name))}">`
                + (art
                    /* is-source-mirrored flips when splash faces left (crop editor). */
                    ? `<span class="draft-slot-art-wrap${mirrored}" aria-hidden="true">`
                        + `<img class="draft-slot-art" loading="lazy" decoding="async" `
                        + `referrerpolicy="no-referrer" src="${escHtml(art)}" alt="" `
                        + cropStyle
                        + onerr
                        + `></span>`
                    : '<span class="draft-slot-ph" aria-hidden="true"></span>')
                + `<span class="draft-slot-shade" aria-hidden="true"></span>`
                + `<span class="draft-slot-name">${escHtml(name)}</span>`
                + `<span class="draft-slot-x" aria-hidden="true">&times;</span>`
                + `</button>`
            );
        });
        for (let i = list.length; i < MAX_TEAM_PICKS; i += 1) {
            chips.push(
                `<button class="draft-slot is-empty" type="button" data-draft-target="${side}" `
                + `aria-label="${escHtml(copy.pickEmpty)}">`
                + `<span class="draft-slot-shade" aria-hidden="true"></span>`
                + `<span class="draft-slot-name">${escHtml(copy.pickEmpty)}</span>`
                + `</button>`
            );
        }
        host.innerHTML = chips.join('');
    }

    function draftChampRows() {
        const q = (draftQuery || '').trim();
        const role = draftRole || '';
        const allySet = new Set(teamPicks);
        const enemySet = new Set(enemyPicks);
        return Object.entries(DATA.champs || {})
            .map(([cid, info]) => ({
                cid: String(cid),
                info,
                wr: Number(info.wr) || 0,
                name: champName(info, cid),
            }))
            .filter(row => {
                if (role && !(row.info.tags || []).includes(role)) return false;
                if (!q) return true;
                const blob = [
                    row.name,
                    row.info.name_en || '',
                    row.info.alias || '',
                    row.info.name_zh || '',
                    row.cid,
                ].join(' ');
                return searchMatchesText(blob, q);
            })
            .sort((a, b) => b.wr - a.wr);
    }

    // Same Bayes-WR cutoffs as scripts/tierlist_engine.py assign_tier.
    function draftAssignTier(bayesWr) {
        const w = Number(bayesWr) || 0;
        if (w >= 0.55) return 'OP';
        if (w >= 0.52) return 'T1';
        if (w >= 0.50) return 'T2';
        if (w >= 0.48) return 'T3';
        if (w >= 0.46) return 'T4';
        return 'T5';
    }

    function renderDraftChampList() {
        const host = document.getElementById('draft-champ-list');
        if (!host) return;
        const copy = tr();
        const allySet = new Set(teamPicks);
        const enemySet = new Set(enemyPicks);
        const rows = draftChampRows();
        if (!rows.length) {
            host.innerHTML = `<div class="panel-empty">${escHtml(copy.emptyCopy)}</div>`;
            return;
        }
        const tierColors = ((DATA && DATA.tiers) || {}).colors || {};
        const tierOrder = (((DATA && DATA.tiers) || {}).order) || ['OP', 'T1', 'T2', 'T3', 'T4', 'T5'];
        const byTier = {};
        rows.forEach(row => {
            const tier = draftAssignTier(row.wr);
            (byTier[tier] = byTier[tier] || []).push(row);
        });
        const champBtn = (row) => {
            const onAlly = allySet.has(row.cid);
            const onEnemy = enemySet.has(row.cid);
            let state = '';
            if (onAlly) state = ' is-ally';
            else if (onEnemy) state = ' is-enemy';
            const image = row.info.image || '';
            const wrTxt = pct(row.wr);
            const tier = draftAssignTier(row.wr);
            const tierColor = (tierColors[tier] && tierColors[tier].color) || '#555';
            return (
                `<button type="button" class="draft-champ${state}" data-draft-pick="${row.cid}" `
                + `data-tier="${tier}" style="--tier-color:${tierColor}" role="option" `
                + `aria-selected="${onAlly || onEnemy ? 'true' : 'false'}" `
                + `title="${escHtml(row.name)} · ${tier} · ${wrTxt}">`
                + (image ? `<img loading="lazy" src="${image}" alt="">` : '<span class="draft-champ-ph"></span>')
                + `<span class="wr">${wrTxt}</span>`
                + `<span class="name">${escHtml(row.name)}</span>`
                + `</button>`
            );
        };
        // Tier groups each force a new grid row via full-width break (zero padding).
        const parts = [];
        tierOrder.forEach((tier) => {
            const list = byTier[tier];
            if (!list || !list.length) return;
            if (parts.length) {
                parts.push('<div class="draft-tier-break" aria-hidden="true"></div>');
            }
            list.forEach(row => { parts.push(champBtn(row)); });
        });
        host.innerHTML = parts.join('');
    }

    function renderDraftRoleChips() {
        const host = document.getElementById('draft-role-chips');
        if (!host) return;
        // Rebuild when language changes (labels are zh/en); only skip when
        // same locale already painted — still refresh active state.
        const langKey = currentLang || 'zh';
        if (host.dataset.ready === '1' && host.dataset.lang === langKey) {
            host.querySelectorAll('.chip').forEach(c => {
                c.classList.toggle('active', (c.getAttribute('data-draft-role') || '') === draftRole);
            });
            return;
        }
        const roles = [
            { role: '', zh: '★ All', en: '★ All' },
            { role: 'Assassin', zh: '刺客', en: 'Assassin' },
            { role: 'Fighter', zh: '戰士', en: 'Fighter' },
            { role: 'Mage', zh: '法師', en: 'Mage' },
            { role: 'Marksman', zh: '射手', en: 'Marksman' },
            { role: 'Support', zh: '輔助', en: 'Support' },
            { role: 'Tank', zh: '坦克', en: 'Tank' },
        ];
        host.innerHTML = roles.map(r => {
            const label = pickLang(r.zh, r.en);
            const active = (draftRole || '') === r.role ? ' active' : '';
            // data-role keeps home .chip[data-role] --role-color hooks; draft
            // CSS still forces accent gold on .active so 戰士 stays 亮黃.
            return (
                `<button type="button" class="chip${active}" data-role="${escHtml(r.role)}" `
                + `data-draft-role="${escHtml(r.role)}" `
                + `data-label-zh="${escHtml(r.zh)}" data-label-en="${escHtml(r.en)}">${escHtml(label)}</button>`
            );
        }).join('');
        host.dataset.ready = '1';
        host.dataset.lang = langKey;
    }

    function draftMetricValue(value) {
        return value === null || value === undefined || value === '' || !Number.isFinite(Number(value))
            ? '—'
            : pct(Number(value));
    }

    function draftMetricTone(value) {
        if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return 'is-empty';
        if (Number(value) >= 0.53) return 'is-good';
        if (Number(value) <= 0.47) return 'is-bad';
        return 'is-even';
    }

    function buildDraftMetricsHtml() {
        const copy = tr();
        const mu = evaluateMatchup(teamPicks, enemyPicks);
        const fullDraft = teamPicks.length === MAX_TEAM_PICKS && enemyPicks.length === MAX_TEAM_PICKS;
        const note = !fullDraft ? copy.draftMetricPartial
            : (mu.finalWr == null ? copy.draftMetricUnavailable : copy.draftMetricFinalNote);
        return `
            <div class="draft-metric final ${draftMetricTone(mu.finalWr)}">
                <span class="draft-metric-label">${escHtml(copy.draftMetricFinal)}</span>
                <strong class="draft-metric-value">${draftMetricValue(fullDraft ? mu.finalWr : null)}</strong>
                <span class="draft-metric-note">${escHtml(note || '')}</span>
            </div>`;
    }

    function renderDraftView() {
        const active = draftView === 'analysis' ? 'analysis' : 'draft';
        draftView = active;
        document.querySelectorAll('[data-draft-view]').forEach(btn => {
            const isActive = btn.getAttribute('data-draft-view') === active;
            btn.classList.toggle('is-active', isActive);
            btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
        });
        document.querySelectorAll('[data-draft-pane]').forEach(pane => {
            const isActive = pane.getAttribute('data-draft-pane') === active;
            pane.classList.toggle('is-active', isActive);
            pane.hidden = !isActive;
        });
    }

    function buildDraftResultHtml() {
        const copy = tr();
        const mu = evaluateMatchup(teamPicks, enemyPicks);
        if (!teamPicks.length && !enemyPicks.length) {
            return `<div class="panel-empty">${escHtml(copy.draftEmpty || copy.panelEmpty)}</div>`;
        }
        const parts = [];
        if (pickNotice) {
            parts.push(`<div class="pick-note">${escHtml(pickNotice)}</div>`);
        }
        // Both sides have ≥1 pick → overlaid radar + dual bars (partial rosters OK).
        // Only final LR WR still requires full 5v5 (see buildDraftMetricsHtml).
        if (mu.ally && mu.enemy) {
            parts.push(buildMatchupCompareHtml(
                mu.ally, mu.enemy, teamPicks.length, enemyPicks.length,
            ));
        } else if (mu.ally) {
            parts.push(`<div class="draft-eval-block"><div class="draft-eval-label">${escHtml(copy.draftAllyEval || '我方陣容')}</div>${buildTeamEvalHtml(teamPicks)}</div>`);
        } else if (mu.enemy) {
            parts.push(`<div class="draft-eval-block is-enemy"><div class="draft-eval-label">${escHtml(copy.draftEnemyEval || '對手陣容')}</div>${buildTeamEvalHtml(enemyPicks)}</div>`);
        }
        // Recommendations when ally not full and no focus on enemy-only.
        if (teamPicks.length > 0 && teamPicks.length < MAX_TEAM_PICKS && draftSide === 'ally') {
            const recs = aggregateRecommendations();
            if (recs.length) {
                const recHtml = recommendationDisplayRows(recs).map((row, idx) => {
                    const info = DATA.champs[row.id];
                    const name = info ? champName(info, row.id) : ('#' + row.id);
                    const image = info && info.image ? info.image : '';
                    return (
                        `<button type="button" class="rec-row draft-rec-row" data-draft-pick="${row.id}">`
                        + `<span class="rec-rank">${idx + 1}</span>`
                        + (image ? `<img loading="lazy" src="${image}" alt="">` : '')
                        + `<span class="rec-name">${escHtml(name)}</span>`
                        + `</button>`
                    );
                }).join('');
                parts.push(`
                    <div class="draft-eval-block">
                        <div class="draft-eval-label">${escHtml(copy.sideTitle)}</div>
                        <div class="draft-rec-list">${recHtml}</div>
                    </div>`);
            }
        }
        return parts.join('') || `<div class="panel-empty">${escHtml(copy.draftEmpty || '')}</div>`;
    }

    function renderDraft() {
        const shell = document.querySelector('.view-draft');
        if (!shell) return;
        renderDraftRoleChips();
        renderDraftSlots('ally');
        renderDraftSlots('enemy');
        renderDraftChampList();
        renderDraftView();

        const copy = tr();
        const mu = evaluateMatchup(teamPicks, enemyPicks);
        const allyWrEl = document.getElementById('draft-ally-wr');
        const enemyWrEl = document.getElementById('draft-enemy-wr');
        const metricsEl = document.getElementById('draft-metrics');
        const resultEl = document.getElementById('draft-result');
        if (allyWrEl) {
            allyWrEl.textContent = mu.ally ? pct(mu.ally.estWr) : '—';
        }
        if (enemyWrEl) {
            enemyWrEl.textContent = mu.enemy ? pct(mu.enemy.estWr) : '—';
        }
        const shellEl = shell.querySelector('.draft-shell');
        if (shellEl) shellEl.classList.toggle('is-target-enemy', draftSide === 'enemy');
        document.querySelectorAll('.draft-side-select').forEach(btn => {
            const t = btn.getAttribute('data-draft-target');
            btn.classList.toggle('is-active', t === draftSide);
        });
        document.querySelectorAll('.draft-side').forEach(el => {
            el.classList.toggle('is-targeting', el.getAttribute('data-draft-side') === draftSide);
        });
        if (metricsEl) metricsEl.innerHTML = buildDraftMetricsHtml();
        if (resultEl) resultEl.innerHTML = buildDraftResultHtml();

        const searchEl = document.getElementById('draft-search');
        if (searchEl) {
            if (searchEl.value !== draftQuery) searchEl.value = draftQuery;
            // Keep placeholder/aria in sync even if applyLanguage skipped update.
            searchEl.placeholder = copy.searchPlaceholderMobile;
            searchEl.setAttribute('aria-label', copy.searchAria);
        }
    }

    // ---- Meta Pick mini-game: 10 pool → pick 5 → lock → WR / one-time hint ----
    // A full run is exactly 5 completed rounds; avg_rank = mean of integer ranks.
    // Server re-scores every submit — never trust client ranks remotely.
    const META_PICK_POOL = 10;
    const META_PICK_NEED = 5;
    const META_PICK_ROUNDS = 5;
    const META_PICK_MIN_GAMES = 50;
    // Build-time inject; empty string = remote board/submit unavailable.
    const META_PICK_API_BASE = "";
    const metaPick = {
        phase: 'idle', // idle | picking | miss_offer | reveal
        poolIds: [],
        optimalIds: [],
        optimalScore: 0,
        /** All C(10,5) team est-WR scores for this pool (for PR / rank). */
        allScores: [],
        comboTotal: 0,
        pickedIds: [],
        pinnedIds: [],
        hintUsed: false,
        notice: '',
        noticeKind: '',
        missMissing: 0,
        dealt: false,
    };
    /** 5-round run state (leaderboard MVP). */
    const metaPickSession = {
        rounds: [], // { pool_ids, picked_ids, rank, total }
        recordedThisReveal: false,
        settled: false,
        submitState: 'idle', // idle | submitting | ok | err
        submitMessage: '',
        nickname: '',
        boardLoaded: false,
        boardLoading: false,
        boardError: '',
        boardEntries: null,
        boardTotal: 0,
        boardPatch: '',
    };

    function metaPickApiBase() {
        const base = (typeof META_PICK_API_BASE === 'string' ? META_PICK_API_BASE : '').trim();
        return base.replace(/\/+$/, '');
    }

    function metaPickSnapshotPatch() {
        return String((DATA && (DATA.patch_prefix || DATA.patch)) || '').trim();
    }

    /** Canonical numeric/string sort so Meta Pick est-WR is order-invariant. */
    function metaPickCanonicalIds(ids) {
        return (ids || []).map(String).slice().sort((a, b) => {
            const na = Number(a);
            const nb = Number(b);
            const aNum = Number.isFinite(na) && String(na) === a;
            const bNum = Number.isFinite(nb) && String(nb) === b;
            if (aNum && bNum) return na - nb;
            if (aNum) return -1;
            if (bNum) return 1;
            return a.localeCompare(b, undefined, { numeric: true });
        });
    }

    function metaPickShuffle(arr, rng) {
        const a = arr.slice();
        const rand = typeof rng === 'function' ? rng : Math.random;
        for (let i = a.length - 1; i > 0; i -= 1) {
            const j = Math.floor(rand() * (i + 1));
            const t = a[i];
            a[i] = a[j];
            a[j] = t;
        }
        return a;
    }

    function metaPickSampleFrom(band, n, rng) {
        if (n <= 0 || !band.length) return [];
        return metaPickShuffle(band, rng).slice(0, Math.min(n, band.length));
    }

    function metaPickEligibleIds() {
        const champs = (DATA && DATA.champs) || {};
        return Object.keys(champs).filter(cid => {
            const info = champs[cid];
            if (!info) return false;
            if (!Number.isFinite(Number(info.wr))) return false;
            return (Number(info.g) || 0) >= META_PICK_MIN_GAMES;
        });
    }

    /** Stratified sample: mix high / mid / low solo WR so boards stay interesting. */
    function metaPickSamplePool(eligible, n, rng) {
        const need = n || META_PICK_POOL;
        const sorted = eligible.slice().sort((a, b) => {
            const d = Number(DATA.champs[b].wr) - Number(DATA.champs[a].wr);
            if (d) return d;
            return String(a).localeCompare(String(b), undefined, { numeric: true });
        });
        if (sorted.length <= need) return metaPickShuffle(sorted, rng);
        const third = Math.max(1, Math.floor(sorted.length / 3));
        const high = sorted.slice(0, third);
        const mid = sorted.slice(third, third * 2);
        const low = sorted.slice(third * 2);
        let pick = []
            .concat(metaPickSampleFrom(high, 3, rng))
            .concat(metaPickSampleFrom(mid, 4, rng))
            .concat(metaPickSampleFrom(low, 3, rng));
        // Top up if a band was thin.
        if (pick.length < need) {
            const used = new Set(pick);
            const rest = metaPickShuffle(sorted.filter(id => !used.has(id)), rng);
            pick = pick.concat(rest.slice(0, need - pick.length));
        }
        return metaPickShuffle(pick.slice(0, need), rng);
    }

    function metaPickCombinations(arr, k) {
        const out = [];
        const path = [];
        function rec(start) {
            if (path.length === k) {
                out.push(path.slice());
                return;
            }
            for (let i = start; i < arr.length; i += 1) {
                path.push(arr[i]);
                rec(i + 1);
                path.pop();
            }
        }
        rec(0);
        return out;
    }

    function metaPickScoreTeam(ids) {
        if (!ids || !ids.length) return 0.5;
        // Meta Pick only: sort ids so pair-walk + composition match server.
        // Draft evaluateFullTeam stays order-as-picked (do not change globally).
        return Number(evaluateFullTeam(metaPickCanonicalIds(ids)).estWr) || 0.5;
    }

    /**
     * Score every k-subset of the pool (C(n,k)). Returns best team + all scores
     * so reveal can rank the player's est WR among the full distribution.
     */
    function metaPickScoreAllTeams(pool, k) {
        const need = k || META_PICK_NEED;
        const combos = metaPickCombinations(pool, need);
        const scores = [];
        let bestIds = null;
        let bestScore = -Infinity;
        let bestKey = '';
        combos.forEach(ids => {
            const score = metaPickScoreTeam(ids);
            scores.push(score);
            const key = ids.slice().sort((a, b) => String(a).localeCompare(String(b), undefined, { numeric: true })).join(',');
            if (score > bestScore + 1e-12 || (Math.abs(score - bestScore) <= 1e-12 && (bestIds == null || key < bestKey))) {
                bestScore = score;
                bestIds = ids.slice();
                bestKey = key;
            }
        });
        return {
            ids: bestIds || [],
            score: Number.isFinite(bestScore) ? bestScore : 0.5,
            scores,
            total: scores.length,
        };
    }

    function metaPickBestTeamOf(pool, k) {
        const all = metaPickScoreAllTeams(pool, k);
        return { ids: all.ids, score: all.score };
    }

    function metaPickSetsEqual(a, b) {
        if (!a || !b || a.length !== b.length) return false;
        const sa = a.map(String).slice().sort();
        const sb = b.map(String).slice().sort();
        for (let i = 0; i < sa.length; i += 1) {
            if (sa[i] !== sb[i]) return false;
        }
        return true;
    }

    function metaPickMissingCount(userIds, optimalIds) {
        const u = new Set((userIds || []).map(String));
        let miss = 0;
        (optimalIds || []).forEach(id => {
            if (!u.has(String(id))) miss += 1;
        });
        return miss;
    }

    /**
     * Rank player's team est WR among all C(10,5) scores.
     * rank 1 = best; PR = classic percentile (higher = better).
     * PR = 100 * (n_worse + 0.5 * n_tie) / n
     */
    function metaPickRankAmong(userScore, allScores) {
        const scores = Array.isArray(allScores) ? allScores : [];
        const n = scores.length;
        if (!n) {
            return { rank: 1, total: 0, pr: 0, worse: 0, ties: 0, grade: 'F' };
        }
        const u = Number(userScore);
        let better = 0;
        let worse = 0;
        let ties = 0;
        scores.forEach(s => {
            const v = Number(s);
            if (v > u + 1e-12) better += 1;
            else if (v < u - 1e-12) worse += 1;
            else ties += 1;
        });
        const rank = better + 1;
        const prRaw = 100 * (worse + 0.5 * ties) / n;
        const pr = Math.round(prRaw * 10) / 10;
        let grade = 'F';
        if (rank === 1) grade = 'S';
        else if (pr >= 90) grade = 'A';
        else if (pr >= 70) grade = 'B';
        else if (pr >= 50) grade = 'C';
        else if (pr >= 30) grade = 'D';
        return { rank, total: n, pr, worse, ties, better, grade };
    }

    /**
     * Approximate PR from rank among `total` combos (singleton-tie assumption).
     * Matches metaPickRankAmong: PR = 100 * (worse + 0.5 * ties) / n
     * with better = rank-1, ties = 1, worse = n - rank.
     * Used for per-round metrics only; settlement/board hero score is OVR.
     */
    function metaPickPrFromRank(rank, total) {
        const r = Number(rank);
        const n = Math.max(1, Number(total) || 252);
        if (!Number.isFinite(r) || r < 1) return 0;
        const pr = 100 * (n - r + 0.5) / n;
        return Math.round(Math.max(0, Math.min(100, pr)) * 10) / 10;
    }

    /**
     * Display OVR 1–99 from rank among `total` (99 = #1 best, 1 = last).
     * Linear in rank space; floor so only true #1 maps to 99 (avoids #1 and #2
     * both rounding to 99 when n≈252). Accepts fractional ranks.
     */
    function metaPickOvrFromRank(rank, total) {
        const r = Number(rank);
        const n = Math.max(2, Number(total) || 252);
        if (!Number.isFinite(r) || r < 1) return 1;
        const clamped = Math.min(n, Math.max(1, r));
        if (clamped <= 1) return 99;
        // ranks (1, n] → (99, 1]; floor keeps #2 at 98, last at 1
        const raw = 99 - (clamped - 1) * 98 / (n - 1);
        return Math.max(1, Math.min(98, Math.floor(raw + 1e-9)));
    }

    /**
     * Grade letter from rank among `total` combos — same ladder as metaPickRankAmong
     * when ties are a singleton at that rank (display/settlement chips).
     */
    function metaPickGradeFromRank(rank, total) {
        const r = Number(rank);
        const n = Math.max(1, Number(total) || 252);
        if (!Number.isFinite(r) || r < 1) return 'F';
        if (r === 1) return 'S';
        return metaPickGradeFromOvr(metaPickOvrFromRank(r, n));
    }

    /** Grade from OVR (1–99, higher better). S only at perfect 99. */
    function metaPickGradeFromOvr(ovr) {
        const v = Number(ovr);
        if (!Number.isFinite(v)) return 'F';
        if (v >= 99) return 'S';
        if (v >= 90) return 'A';
        if (v >= 70) return 'B';
        if (v >= 50) return 'C';
        if (v >= 30) return 'D';
        return 'F';
    }

    /** @deprecated alias — prefer metaPickGradeFromOvr for settlement. */
    function metaPickGradeFromPr(pr) {
        return metaPickGradeFromOvr(pr);
    }

    /** CSS class for grade colors (shared with analysis letter grades). */
    function metaPickGradeClass(grade) {
        const g = String(grade || 'F').toUpperCase();
        if (g === 'S') return 'is-grade-s';
        if (g === 'A' || g === 'A+') return 'is-grade-a';
        if (g === 'B' || g === 'B+') return 'is-grade-b';
        if (g === 'C' || g === 'C+') return 'is-grade-c';
        if (g === 'D') return 'is-grade-d';
        return 'is-grade-f';
    }

    /** Prefer an optimal champ the user did not already have correct. */
    function metaPickHintChamp(optimalIds, previousUserIds, rng) {
        const prev = new Set((previousUserIds || []).map(String));
        const missing = (optimalIds || []).filter(id => !prev.has(String(id)));
        const pool = missing.length ? missing : (optimalIds || []).slice();
        if (!pool.length) return null;
        return metaPickSampleFrom(pool, 1, rng)[0] || null;
    }

    function metaPickResetRound() {
        metaPick.phase = 'idle';
        metaPick.poolIds = [];
        metaPick.optimalIds = [];
        metaPick.optimalScore = 0;
        metaPick.allScores = [];
        metaPick.comboTotal = 0;
        metaPick.pickedIds = [];
        metaPick.pinnedIds = [];
        metaPick.hintUsed = false;
        metaPick.notice = '';
        metaPick.noticeKind = '';
        metaPick.missMissing = 0;
        metaPick.dealt = false;
    }

    function metaPickDealRound() {
        const copy = tr();
        metaPickResetRound();
        metaPickSession.recordedThisReveal = false;
        metaPickSession.settled = false;
        metaPickSession.submitState = 'idle';
        metaPickSession.submitMessage = '';
        if (!DATA || !DATA.champs) {
            metaPick.phase = 'idle';
            metaPick.notice = copy.gameWaitingData;
            metaPick.noticeKind = '';
            return;
        }
        const eligible = metaPickEligibleIds();
        if (eligible.length < META_PICK_NEED) {
            metaPick.phase = 'idle';
            metaPick.notice = copy.gameNoPool;
            metaPick.noticeKind = '';
            return;
        }
        const pool = metaPickSamplePool(eligible, META_PICK_POOL);
        // Score full C(10,5) once per deal — best answer + PR distribution.
        const all = metaPickScoreAllTeams(pool, META_PICK_NEED);
        metaPick.poolIds = pool;
        metaPick.optimalIds = all.ids.map(String);
        metaPick.optimalScore = all.score;
        metaPick.allScores = all.scores;
        metaPick.comboTotal = all.total;
        metaPick.pickedIds = [];
        metaPick.pinnedIds = [];
        metaPick.hintUsed = false;
        metaPick.phase = 'picking';
        metaPick.dealt = true;
        metaPick.notice = '';
        metaPick.noticeKind = '';
        metaPick.missMissing = 0;
    }

    /** Record rank once when a round first enters reveal (client preview only). */
    function metaPickRecordRoundIfNeeded() {
        if (metaPickSession.recordedThisReveal) return;
        if (metaPick.phase !== 'reveal') return;
        if (!metaPick.poolIds.length || metaPick.pickedIds.length !== META_PICK_NEED) return;
        const userScore = metaPickScoreTeam(metaPick.pickedIds);
        let scores = metaPick.allScores;
        if (!scores || !scores.length) {
            scores = metaPickScoreAllTeams(metaPick.poolIds, META_PICK_NEED).scores;
            metaPick.allScores = scores;
            metaPick.comboTotal = scores.length;
        }
        const rankInfo = metaPickRankAmong(userScore, scores);
        metaPickSession.rounds.push({
            pool_ids: metaPick.poolIds.map(String),
            picked_ids: metaPick.pickedIds.map(String),
            rank: rankInfo.rank,
            total: rankInfo.total || metaPick.comboTotal || 252,
            grade: rankInfo.grade,
            pr: rankInfo.pr,
        });
        metaPickSession.recordedThisReveal = true;
        // Do NOT auto-set settled here: the fifth reveal must stay visible with
        // #game-show-settle. Settlement is only entered when that button is clicked
        // (metaPickNextRound may still guard length>=5 as a fallback).
    }

    function metaPickSessionAvgRank() {
        const ranks = metaPickSession.rounds.map(r => Number(r.rank)).filter(n => Number.isFinite(n));
        if (!ranks.length) return null;
        return ranks.reduce((a, b) => a + b, 0) / ranks.length;
    }

    /**
     * Mean OVR across recorded rounds (each round mapped #rank→OVR, then averaged).
     * Falls back to OVR(avg_rank) if needed.
     */
    function metaPickSessionAvgOvr() {
        const rounds = metaPickSession.rounds || [];
        if (!rounds.length) return null;
        const ovrs = rounds.map((round) => {
            const r = Number(round.rank);
            const t = Number(round.total) || 252;
            if (!Number.isFinite(r)) return null;
            return metaPickOvrFromRank(r, t);
        }).filter(v => v != null && Number.isFinite(v));
        if (!ovrs.length) {
            const avgR = metaPickSessionAvgRank();
            if (avgR == null) return null;
            return metaPickOvrFromRank(avgR, (rounds[0] && rounds[0].total) || 252);
        }
        // Integer OVR per round; mean rounded to nearest int for the hero digit.
        return Math.round(ovrs.reduce((a, b) => a + b, 0) / ovrs.length);
    }

    function metaPickFormatOvr(ovr) {
        const v = Number(ovr);
        if (!Number.isFinite(v)) return '—';
        return String(Math.round(Math.max(1, Math.min(99, v))));
    }

    function metaPickResetSession() {
        metaPickSession.rounds = [];
        metaPickSession.recordedThisReveal = false;
        metaPickSession.settled = false;
        metaPickSession.submitState = 'idle';
        metaPickSession.submitMessage = '';
        // Keep nickname draft for convenience.
    }

    function metaPickNextRound() {
        if (metaPickSession.rounds.length >= META_PICK_ROUNDS) {
            metaPickSession.settled = true;
            renderMetaPick();
            return;
        }
        metaPickDealRound();
        renderMetaPick();
    }

    function metaPickRestartRun() {
        metaPickResetSession();
        metaPickDealRound();
        renderMetaPick();
        metaPickLoadLeaderboard({ force: true });
    }

    function metaPickNormalizeNickClient(raw) {
        const text = String(raw || '').trim().replace(/\s+/g, ' ');
        const n = [...text].length; // Unicode code points
        return { text, n, ok: n >= 2 && n <= 16 };
    }

    async function metaPickSubmitRun() {
        const copy = tr();
        const base = metaPickApiBase();
        if (!base) {
            metaPickSession.submitState = 'err';
            metaPickSession.submitMessage = copy.gameBoardUnavailable || 'Leaderboard unavailable';
            renderMetaPick();
            return;
        }
        // One successful submit locks this 5-round run (server also rejects replay dupes).
        if (metaPickSession.submitState === 'ok') {
            metaPickSession.submitMessage = copy.gameSubmitLocked || 'Already submitted this run';
            renderMetaPick();
            return;
        }
        if (metaPickSession.submitState === 'submitting') {
            return;
        }
        if (metaPickSession.rounds.length !== META_PICK_ROUNDS) {
            metaPickSession.submitState = 'err';
            metaPickSession.submitMessage = copy.gameNeedFiveRounds || 'Finish 5 rounds first';
            renderMetaPick();
            return;
        }
        const nick = metaPickNormalizeNickClient(metaPickSession.nickname);
        if (!nick.ok) {
            metaPickSession.submitState = 'err';
            metaPickSession.submitMessage = copy.gameNickInvalid || 'Nickname must be 2–16 characters';
            renderMetaPick();
            return;
        }
        const patch = metaPickSnapshotPatch();
        if (!patch) {
            metaPickSession.submitState = 'err';
            metaPickSession.submitMessage = copy.gamePatchMissing || 'Missing patch snapshot';
            renderMetaPick();
            return;
        }
        metaPickSession.submitState = 'submitting';
        metaPickSession.submitMessage = copy.gameSubmitting || 'Submitting…';
        renderMetaPick();
        const body = {
            nickname: nick.text,
            patch,
            rounds: metaPickSession.rounds.map(r => ({
                pool_ids: r.pool_ids,
                picked_ids: r.picked_ids,
            })),
        };
        try {
            const res = await fetch(`${base}/api/meta-pick/runs`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            let data = null;
            try { data = await res.json(); } catch { data = null; }
            if (!res.ok) {
                const detail = (data && (data.detail || data.message)) || (`HTTP ${res.status}`);
                const detailStr = String(detail);
                metaPickSession.submitState = 'err';
                // Server 409 on same-run / different-nickname (and related integrity races).
                if (res.status === 409 && /already submitted|換暱稱|nickname/i.test(detailStr)) {
                    metaPickSession.submitMessage = copy.gameSubmitDup || detailStr;
                } else {
                    metaPickSession.submitMessage = detailStr;
                }
                renderMetaPick();
                return;
            }
            metaPickSession.submitState = 'ok';
            if (data && data.updated === false) {
                // Retained best (entry/retained), not the rejected current-run avg_rank.
                const keptRaw = (data.entry && data.entry.avg_rank != null)
                    ? data.entry.avg_rank
                    : (data.retained && data.retained.avg_rank != null)
                        ? data.retained.avg_rank
                        : null;
                const keptTotal = (data.entry && data.entry.total_combos)
                    || (data.retained && data.retained.total_combos)
                    || 252;
                const keptOvr = keptRaw != null
                    ? metaPickFormatOvr(metaPickOvrFromRank(Number(keptRaw), keptTotal))
                    : metaPickFormatOvr(metaPickSessionAvgOvr());
                metaPickSession.submitMessage = (copy.gameSubmitKept || ((a) => `Best kept · OVR ${a}`))(keptOvr);
            } else {
                const avgRank = data && data.avg_rank != null
                    ? Number(data.avg_rank)
                    : metaPickSessionAvgRank();
                const totalCombos = (data && data.total_combos) || 252;
                const avg = avgRank != null
                    ? metaPickFormatOvr(metaPickOvrFromRank(avgRank, totalCombos))
                    : metaPickFormatOvr(metaPickSessionAvgOvr());
                metaPickSession.submitMessage = (copy.gameSubmitOk || ((a) => `Submitted · avg OVR ${a}`))(avg);
            }
            renderMetaPick();
            metaPickLoadLeaderboard({ force: true });
        } catch (err) {
            metaPickSession.submitState = 'err';
            metaPickSession.submitMessage = copy.gameSubmitFail || 'Submit failed';
            renderMetaPick();
        }
    }

    async function metaPickLoadLeaderboard(opts) {
        const force = !!(opts && opts.force);
        const base = metaPickApiBase();
        const body = document.getElementById('game-board-body');
        const copy = tr();
        if (!body) return;
        if (!base) {
            metaPickSession.boardLoaded = true;
            metaPickSession.boardError = 'unavailable';
            body.innerHTML = `<p class="game-board-empty">${escHtml(copy.gameBoardUnavailable || 'Leaderboard unavailable')}</p>`;
            return;
        }
        if (metaPickSession.boardLoaded && !force) {
            if (metaPickSession.boardEntries) metaPickRenderLeaderboard();
            return;
        }
        if (metaPickSession.boardLoading && !force) return;
        metaPickSession.boardLoading = true;
        body.innerHTML = `<p class="game-board-empty">${escHtml(copy.gameBoardLoading || 'Loading…')}</p>`;
        const patch = metaPickSnapshotPatch();
        const q = patch ? `?patch=${encodeURIComponent(patch)}&limit=50` : '?limit=50';
        try {
            const res = await fetch(`${base}/api/meta-pick/leaderboard${q}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            metaPickSession.boardLoaded = true;
            metaPickSession.boardError = '';
            metaPickSession.boardEntries = Array.isArray(data.entries) ? data.entries : [];
            metaPickSession.boardTotal = Number(data.total) || metaPickSession.boardEntries.length;
            metaPickSession.boardPatch = String(data.patch || patch || '');
            metaPickRenderLeaderboard();
        } catch (err) {
            metaPickSession.boardLoaded = true;
            metaPickSession.boardError = 'fail';
            body.innerHTML = `<p class="game-board-empty">${escHtml(copy.gameBoardFail || 'Could not load leaderboard')}</p>`;
        } finally {
            metaPickSession.boardLoading = false;
        }
    }

    function metaPickRenderLeaderboard() {
        const body = document.getElementById('game-board-body');
        if (!body) return;
        const copy = tr();
        const entries = metaPickSession.boardEntries || [];
        if (!entries.length) {
            body.innerHTML = `<p class="game-board-empty">${escHtml(copy.gameBoardEmpty || 'No scores yet')}</p>`;
            return;
        }
        const rows = entries.map((e, i) => {
            const avgRank = Number(e.avg_rank);
            const totalCombos = Number(e.total_combos) || 252;
            const avgOvr = Number.isFinite(avgRank)
                ? metaPickOvrFromRank(avgRank, totalCombos)
                : null;
            const avgTxt = avgOvr == null ? '—' : metaPickFormatOvr(avgOvr);
            const avgG = avgOvr == null ? '' : metaPickGradeClass(metaPickGradeFromOvr(avgOvr));
            const rankList = Array.isArray(e.ranks) ? e.ranks : [];
            const ranksHtml = rankList.map((rk) => {
                const r = Number(rk);
                const ovr = Number.isFinite(r) ? metaPickOvrFromRank(r, totalCombos) : null;
                const gCls = ovr == null ? '' : metaPickGradeClass(metaPickGradeFromOvr(ovr));
                const ovrTxt = ovr == null ? '—' : metaPickFormatOvr(ovr);
                return `<span class="game-board-rk ${gCls}" title="#${escHtml(String(rk))}">${escHtml(ovrTxt)}</span>`;
            }).join('<span class="game-board-rk-sep"> · </span>');
            return (
                `<tr>`
                + `<td class="game-board-pos">${i + 1}</td>`
                + `<td class="game-board-nick">`
                + `<span class="game-board-nick-text">${escHtml(String(e.nickname || ''))}</span>`
                + `</td>`
                + `<td class="game-board-avg ${avgG}" title="${Number.isFinite(avgRank) ? `#${avgRank.toFixed(1)} / ${totalCombos}` : ''}">${escHtml(avgTxt)}</td>`
                + `<td class="game-board-ranks">${ranksHtml}</td>`
                + `</tr>`
            );
        }).join('');
        const patchLabel = metaPickSession.boardPatch
            ? escHtml(metaPickSession.boardPatch)
            : '';
        body.innerHTML = (
            (patchLabel ? `<div class="game-board-meta">${escHtml(copy.gameBoardPatch || 'Patch')} ${patchLabel}</div>` : '')
            + `<table class="game-board-table">`
            + `<thead><tr>`
            + `<th>#</th>`
            + `<th>${escHtml(copy.gameBoardNick || 'Name')}</th>`
            + `<th>${escHtml(copy.gameBoardAvg || 'Avg')}</th>`
            + `<th>${escHtml(copy.gameBoardRanks || 'Rounds')}</th>`
            + `</tr></thead>`
            + `<tbody>${rows}</tbody></table>`
        );
    }

    function metaPickSettlementHtml(copy) {
        const rounds = metaPickSession.rounds || [];
        const avgRank = metaPickSessionAvgRank();
        const avgOvr = metaPickSessionAvgOvr();
        const avgOvrTxt = avgOvr == null ? '—' : metaPickFormatOvr(avgOvr);
        const total = Number((rounds[0] && rounds[0].total) || 252) || 252;
        const avgRankTxt = avgRank == null ? '—' : avgRank.toFixed(1);
        const avgGrade = avgOvr == null
            ? 'F'
            : (avgRank === 1 || avgOvr >= 99 ? 'S' : metaPickGradeFromOvr(avgOvr));
        const avgGradeCls = metaPickGradeClass(avgGrade);
        const rankSub = (copy.gameSettleRankSub || ((a, t) => `Avg rank #${a} / ${t}`))(avgRankTxt, total);
        const rankChips = rounds.map((round, i) => {
            const r = Number(round.rank);
            const t = Number(round.total) || total;
            const ovr = metaPickOvrFromRank(r, t);
            const grade = round.grade || metaPickGradeFromOvr(ovr);
            const gCls = metaPickGradeClass(grade);
            const ovrTxt = metaPickFormatOvr(ovr);
            return (
                `<span class="game-settle-chip" title="#${r} / ${t} · ${escHtml(grade)}">`
                + `<span class="game-settle-chip-n">${escHtml(copy.gameRoundN ? copy.gameRoundN(i + 1) : `R${i + 1}`)}</span>`
                + `<span class="game-settle-chip-r ${gCls}">${escHtml(ovrTxt)}</span>`
                + `</span>`
            );
        }).join('');
        const nick = metaPickSession.nickname || '';
        const base = metaPickApiBase();
        const locked = metaPickSession.submitState === 'ok';
        const canSubmit = !!base
            && metaPickSession.submitState !== 'submitting'
            && !locked;
        let status = '';
        if (metaPickSession.submitMessage) {
            const kind = metaPickSession.submitState === 'ok' ? 'ok'
                : metaPickSession.submitState === 'err' ? 'miss' : '';
            status = `<div class="game-settle-status${kind ? ` is-${kind}` : ''}">${escHtml(metaPickSession.submitMessage)}</div>`;
        } else if (!base) {
            status = `<div class="game-settle-status is-miss">${escHtml(copy.gameBoardUnavailable || 'Leaderboard unavailable')}</div>`;
        }
        return (
            `<div class="game-settle-card">`
            + `<div class="game-settle-kicker">${escHtml(copy.gameSettleTitle || 'Run complete')}</div>`
            + `<div class="game-settle-avg" title="${escHtml(rankSub)} · ${escHtml(avgGrade)}">`
            + `<span class="game-settle-avg-prefix">OVR</span>`
            + `<span class="game-settle-avg-num ${avgGradeCls}">${escHtml(avgOvrTxt)}</span>`
            + `</div>`
            + `<div class="game-settle-avg-sub">${escHtml(rankSub)}</div>`
            + `<div class="game-settle-ranks">${rankChips}</div>`
            + `<form class="game-settle-form" id="game-settle-form" autocomplete="nickname">`
            + `<label class="game-settle-label" for="game-nick">`
            + `${escHtml(copy.gameNickLabel || 'Nickname')}`
            + `</label>`
            + `<input type="text" id="game-nick" name="nickname" maxlength="32" `
            + `class="game-settle-input" value="${escHtml(nick)}" `
            + `placeholder="${escHtml(copy.gameNickPlaceholder || '2–16 characters')}" `
            + `aria-label="${escHtml(copy.gameNickLabel || 'Nickname')}"`
            + `${locked ? ' readonly' : ''}>`
            + `<div class="game-settle-actions">`
            + `<button type="submit" class="tool-btn" id="game-submit" ${canSubmit ? '' : 'disabled'}>`
            + `${escHtml(metaPickSession.submitState === 'submitting'
                ? (copy.gameSubmitting || 'Submitting…')
                : locked
                    ? (copy.gameSubmitLocked || 'Already submitted')
                    : (copy.gameSubmit || 'Submit score'))}</button>`
            + `<button type="button" class="tool-btn ghost" id="game-restart">`
            + `${escHtml(copy.gameRestart || 'Play again')}</button>`
            + `</div>`
            + `</form>`
            + status
            + `</div>`
        );
    }

    function metaPickEnsureDealt() {
        if (!metaPick.dealt || !metaPick.poolIds.length) metaPickDealRound();
    }

    function metaPickToggle(cid) {
        if (metaPick.phase !== 'picking') return;
        cid = String(cid);
        if (!metaPick.poolIds.map(String).includes(cid)) return;
        // 提示英雄可故意取消（想挑戰低分）；仍保留「提示」角標方便辨識
        const idx = metaPick.pickedIds.map(String).indexOf(cid);
        if (idx >= 0) {
            metaPick.pickedIds = metaPick.pickedIds.filter(x => String(x) !== cid);
            metaPick.notice = '';
            metaPick.noticeKind = '';
            return;
        }
        if (metaPick.pickedIds.length >= META_PICK_NEED) {
            metaPick.notice = tr().gameMaxOnly;
            metaPick.noticeKind = 'miss';
            return;
        }
        metaPick.pickedIds.push(cid);
        metaPick.notice = '';
        metaPick.noticeKind = '';
        // After the first two free picks: if they are not both optimal, auto-hint once.
        metaPickMaybeAutoHintAfterTwo();
    }

    /** First two picks both in optimal set? Otherwise one-time auto hint. */
    function metaPickMaybeAutoHintAfterTwo() {
        if (metaPick.hintUsed || metaPick.phase !== 'picking') return;
        // Only free-pick phase before any pin; count exact first two.
        if (metaPick.pinnedIds.length || metaPick.pickedIds.length !== 2) return;
        const opt = new Set(metaPick.optimalIds.map(String));
        const bothHit = metaPick.pickedIds.every(id => opt.has(String(id)));
        if (bothHit) return;
        metaPickApplyHint({ auto: true });
    }

    function metaPickLock() {
        if (metaPick.phase !== 'picking') return;
        const copy = tr();
        if (metaPick.pickedIds.length !== META_PICK_NEED) {
            metaPick.notice = copy.gameNeedFive;
            metaPick.noticeKind = 'miss';
            renderMetaPick();
            return;
        }
        const hit = metaPickSetsEqual(metaPick.pickedIds, metaPick.optimalIds);
        if (hit) {
            metaPick.phase = 'reveal';
            metaPick.notice = copy.gamePerfect;
            metaPick.noticeKind = 'ok';
            metaPick.missMissing = 0;
            renderMetaPick();
            return;
        }
        const missing = metaPickMissingCount(metaPick.pickedIds, metaPick.optimalIds);
        metaPick.missMissing = missing;
        // Hint already auto-fired (or skipped because first two were both correct).
        // On lock miss with hint remaining, offer manual hint once; else reveal.
        if (!metaPick.hintUsed) {
            metaPick.phase = 'miss_offer';
            metaPick.notice = '';
            metaPick.noticeKind = 'miss';
            renderMetaPick();
            return;
        }
        metaPick.phase = 'reveal';
        metaPick.notice = '';
        metaPick.noticeKind = 'miss';
        renderMetaPick();
    }

    /**
     * One-time hint: clear free picks, pin one optimal champ (prefer one not
     * already correct in previous selection). `fromAuto` only affects copy.
     */
    function metaPickApplyHint(opts) {
        const fromAuto = !!(opts && opts.auto);
        if (metaPick.hintUsed) return false;
        if (metaPick.phase !== 'picking' && metaPick.phase !== 'miss_offer') return false;
        const prev = metaPick.pickedIds.slice();
        const hintId = metaPickHintChamp(metaPick.optimalIds, prev);
        if (!hintId) {
            metaPick.phase = 'reveal';
            return true;
        }
        metaPick.hintUsed = true;
        metaPick.pinnedIds = [String(hintId)];
        metaPick.pickedIds = [String(hintId)];
        metaPick.phase = 'picking';
        const copy = tr();
        const info = (DATA.champs && DATA.champs[String(hintId)]) || {};
        const name = champName(info, String(hintId));
        const fmt = fromAuto
            ? (copy.gameHintAuto || copy.gameHintUsed)
            : copy.gameHintUsed;
        metaPick.notice = typeof fmt === 'function'
            ? fmt(name)
            : `提示：${name}`;
        metaPick.noticeKind = 'ok';
        metaPick.missMissing = 0;
        return true;
    }

    function metaPickUseHint() {
        if (metaPick.phase !== 'miss_offer' || metaPick.hintUsed) return;
        metaPickApplyHint({ auto: false });
        renderMetaPick();
    }

    function metaPickShowAnswer() {
        if (metaPick.phase !== 'miss_offer' && metaPick.phase !== 'picking') return;
        // If still picking with a full roster after hint, allow "give up" only via lock.
        if (metaPick.phase === 'miss_offer') {
            metaPick.phase = 'reveal';
            metaPick.notice = '';
            metaPick.noticeKind = 'miss';
            renderMetaPick();
        }
    }

    function metaPickPlayAgain() {
        // Legacy single-round restart: only within a run (next-round path uses
        // metaPickNextRound; full-run restart uses metaPickRestartRun).
        metaPickDealRound();
        renderMetaPick();
    }

    function metaPickWrToneClass(wr) {
        if (wr >= 0.53) return 'is-good';
        if (wr <= 0.47) return 'is-bad';
        return 'is-even';
    }

    /** Status of one champ relative to picked set and best set. */
    function metaPickChampRole(cid, pickedSet, bestSet) {
        const id = String(cid);
        const inPick = pickedSet.has(id);
        const inBest = bestSet.has(id);
        if (inPick && inBest) return 'both';
        if (inPick) return 'yours';
        if (inBest) return 'best';
        return 'neither';
    }

    /**
     * Arrange best-5 to mirror user pick order as much as possible:
     * same champ → same slot; only-best champs fill the slots user missed.
     */
    function metaPickAlignBestToUser(userIds, bestIds) {
        const user = (userIds || []).map(String);
        const best = (bestIds || []).map(String);
        const bestSet = new Set(best);
        const out = new Array(Math.max(user.length, META_PICK_NEED)).fill(null);
        const used = new Set();
        user.forEach((id, i) => {
            if (bestSet.has(id)) {
                out[i] = id;
                used.add(id);
            }
        });
        const rest = best.filter(id => !used.has(id));
        let r = 0;
        for (let i = 0; i < out.length; i += 1) {
            if (out[i] == null && r < rest.length) {
                out[i] = rest[r];
                r += 1;
            }
        }
        // Drop trailing nulls if any, keep first META_PICK_NEED.
        return out.filter(Boolean).slice(0, META_PICK_NEED);
    }

    function metaPickRoleLabel(role, copy) {
        if (role === 'both') return copy.gameTagBoth || '雙方';
        if (role === 'yours') return copy.gameTagYours || '你的';
        if (role === 'best') return copy.gameTagBest || '最佳';
        return copy.gameTagMiss || '—';
    }

    /** Letter grade from 0–1 capability percentile (S … F). */
    function metaPickLetterGrade(pct01) {
        const p = Number(pct01);
        if (!Number.isFinite(p)) return '—';
        if (p >= 0.90) return 'S';
        if (p >= 0.80) return 'A+';
        if (p >= 0.70) return 'A';
        if (p >= 0.60) return 'B+';
        if (p >= 0.50) return 'B';
        if (p >= 0.40) return 'C+';
        if (p >= 0.30) return 'C';
        if (p >= 0.20) return 'D';
        return 'F';
    }

    /**
     * Grade AP–AD mix health: closer to empirical ideal (~40% AD) → higher letter.
     * Maps distance-from-target into the same S…F ladder as capability percentiles.
     */
    function metaPickMixGrade(adShare) {
        const target = Number(((DATA.recommendation_composition || {}).damage_mix || {}).target_ad_share);
        const ideal = Number.isFinite(target) ? target : 0.4;
        const dist = Math.abs((Number(adShare) || 0.5) - ideal);
        // dist 0 → 1.0, dist 0.25+ → 0
        const score = Math.max(0, Math.min(1, 1 - dist / 0.25));
        return metaPickLetterGrade(score);
    }

    /**
     * 英雄強度 = 5 人單獨勝率平均（原始 WR，約 40%–60%）。
     * 雷達軸：把 40%→0、60%→1 線性映射（超出夾住），讓圖形有開合。
     * 不再用「相對 173 隻單人的 PR」——那會擠在 80–95、也不是 C(n,5) PR。
     */
    function metaPickTeamStrength(ids) {
        const wrs = [];
        (ids || []).forEach(rawId => {
            const wr = Number((DATA.champs[String(rawId)] || {}).wr);
            if (Number.isFinite(wr)) wrs.push(wr);
        });
        const meanWr = wrs.length
            ? wrs.reduce((a, b) => a + b, 0) / wrs.length
            : 0.5;
        const lo = 0.40;
        const hi = 0.60;
        const radar01 = Math.max(0, Math.min(1, (meanWr - lo) / (hi - lo)));
        return { meanWr, radar01 };
    }

    /**
     * Real damage mix from phys / magic / true DPM.
     * Returns integers ad + ap + trueDmg === 100 (largest-remainder).
     * Never uses object key `true` (boolean keyword footgun).
     */
    function metaPickDamageMix(sums) {
        const p = Math.max(0, Number(sums && sums.phys) || 0);
        const m = Math.max(0, Number(sums && sums.magic) || 0);
        const t = Math.max(0, Number(sums && sums.trueDmg) || 0);
        const total = p + m + t;
        if (!(total > 0)) {
            return { ad: 50, ap: 50, trueDmg: 0, adShare: 0.5 };
        }
        // Fixed order AD → AP → True so remainder tie-breaks prefer AD
        const vals = [(p / total) * 100, (m / total) * 100, (t / total) * 100];
        const ns = vals.map(v => Math.floor(v));
        let left = 100 - (ns[0] + ns[1] + ns[2]);
        const order = [0, 1, 2].sort((a, b) => {
            const fa = vals[a] - ns[a];
            const fb = vals[b] - ns[b];
            return (fb - fa) || (a - b);
        });
        for (let k = 0; k < order.length && left > 0; k += 1) {
            ns[order[k]] += 1;
            left -= 1;
        }
        const ad = ns[0];
        const ap = ns[1];
        const trueDmg = ns[2];
        const adDen = p + m;
        return {
            ad,
            ap,
            trueDmg,
            adShare: adDen > 0 ? p / adDen : 0.5,
        };
    }

    /** Sum phys/magic/true DPM from roster — always bracket-access for "true". */
    function metaPickTeamDamageSums(ids) {
        let phys = 0;
        let magic = 0;
        let trueDmg = 0;
        (ids || []).forEach(rawId => {
            const info = (DATA.champs && DATA.champs[String(rawId)]) || {};
            const comp = info.comp || {};
            phys += Number(comp.phys != null ? comp.phys : comp.physical) || 0;
            magic += Number(comp.magic) || 0;
            trueDmg += Number(
                comp['true'] != null ? comp['true'] : comp.true_damage
            ) || 0;
        });
        return { phys, magic, trueDmg };
    }

    /**
     * Map signed lift (pair / composition) onto radar 0–1.
     * 0.5 = neutral; ±scale ≈ full stretch.
     */
    function metaPickSignedLift01(lift, scale) {
        const s = Number(scale) > 0 ? Number(scale) : 0.06;
        return Math.max(0, Math.min(1, 0.5 + (Number(lift) || 0) / s));
    }

    /**
     * 6-axis profile: 輸出 / 前排 / 開戰 / 清兵 / 默契 / 英雄強度
     * （AP+AD 融合成輸出；默契 = 搭檔 pair lift 化學反應）
     */
    function metaPickSixAxes(ids, copy) {
        const ev = evaluateFullTeam(ids || []);
        const cap = ev.cap || {};
        const strength = metaPickTeamStrength(ids);
        // 輸出：優先用 damage 能力百分位；缺則取 phys/magic 平均
        const dmg = Number(cap.damage);
        const dmg01 = Number.isFinite(dmg) && dmg > 0
            ? dmg
            : ((Number(cap.phys) || 0) + (Number(cap.magic) || 0)) / 2;
        // 默契：隊伍平均 pair lift → 0–1（±6pp 拉滿）
        const chem01 = metaPickSignedLift01(ev.pairLift, 0.06);
        return {
            ev,
            cap,
            strength,
            chem01,
            axes: [
                { label: copy.gameAxisDamage || '輸出', pct: dmg01 },
                { label: copy.gameAxisFront || '前排', pct: Number(cap.front) || 0 },
                { label: copy.gameAxisEngage || '開戰', pct: Number(cap.engage) || 0 },
                { label: copy.gameAxisWave || '清兵', pct: Number(cap.wave) || 0 },
                { label: copy.gameAxisChem || '默契', pct: chem01 },
                { label: copy.gameAxisStrength || '英雄強度', pct: strength.radar01 },
            ],
        };
    }

    /**
     * Dual 6-axis radar (黃=你的 / 灰=最佳) + best-team letter grades on the right.
     */
    function metaPickAnalysisHtml(yourIds, bestIds, copy) {
        const yours = metaPickSixAxes(yourIds, copy);
        const best = metaPickSixAxes(bestIds, copy);
        const cap = best.cap;
        // 英雄強度：原始 5 人平均 solo WR（如 52.3%），不是 PR
        const strengthTxt = pct(best.strength.meanWr);
        // Re-sum damage dims from champ ids (more reliable than teamComposition.sums alone).
        const mix = metaPickDamageMix(metaPickTeamDamageSums(bestIds));
        const adPct = Math.max(0, Math.min(100, Number(mix.ad) || 0));
        const apPct = Math.max(0, Math.min(100, Number(mix.ap) || 0));
        const truePct = Math.max(0, Math.min(100, Number(mix.trueDmg) || 0));
        const mixNoteFn = copy.gameMixNote || ((ad, ap, tr) => `AD ${ad}% · AP ${ap}% · True ${tr}%`);
        const mixNote = mixNoteFn(adPct, apPct, truePct);

        // 灰 = 你的選擇 · 黃 = 最佳 5 人（最佳用強調色）
        const radar = compRadarOverlaySvg([
            {
                axes: yours.axes,
                stroke: 'rgba(196,200,208,0.90)',
                fill: 'rgba(160,166,176,0.14)',
                dot: 'rgba(210,214,220,0.95)',
            },
            {
                axes: best.axes,
                stroke: 'var(--accent, #f5c518)',
                fill: 'color-mix(in srgb, var(--accent, #f5c518) 22%, transparent)',
                dot: 'var(--accent, #f5c518)',
            },
        ], copy.gameAnalysisTitle || copy.teamDimsTitle || '');

        const grades = [
            {
                label: copy.gameEvalStrength || '英雄強度',
                value: strengthTxt,
                kind: 'num',
                tip: copy.gameEvalStrengthTip || '5 人單獨勝率平均',
            },
            {
                label: copy.gameEvalWave || '清兵',
                value: metaPickLetterGrade(cap.wave),
                kind: 'letter',
                tip: Math.round((Number(cap.wave) || 0) * 100),
            },
            {
                label: copy.gameEvalMix || '傷害構成',
                value: metaPickMixGrade(mix.adShare),
                kind: 'letter',
                tip: mixNote,
            },
            {
                label: copy.gameEvalDamage || '輸出',
                value: metaPickLetterGrade(cap.damage),
                kind: 'letter',
                tip: Math.round((Number(cap.damage) || 0) * 100),
            },
            {
                label: copy.gameEvalFront || '前排',
                value: metaPickLetterGrade(cap.front),
                kind: 'letter',
                tip: Math.round((Number(cap.front) || 0) * 100),
            },
            {
                label: copy.gameEvalEngage || '開戰',
                value: metaPickLetterGrade(cap.engage),
                kind: 'letter',
                tip: Math.round((Number(cap.engage) || 0) * 100),
            },
        ];

        const gradeRows = grades.map(g => {
            const gradeCls = g.kind === 'letter'
                ? `is-grade-${String(g.value).replace('+', 'p').toLowerCase()}`
                : 'is-num';
            return (
                `<div class="game-an-grade ${gradeCls}" title="${escHtml(String(g.tip))}">`
                + `<span class="game-an-grade-label">${escHtml(g.label)}</span>`
                + `<span class="game-an-grade-val">${escHtml(g.value)}</span>`
                + `</div>`
            );
        }).join('');

        const bestEstWr = Number(best.ev.estWr);
        const bestWrTone = metaPickWrToneClass(bestEstWr);
        // flex-basis % 填滿 bar；圖例永遠三段。
        // Class names: is-phys / is-magic / is-true — NEVER is-ad (adblockers hide .is-ad).
        const mixSeg = (cls, n) => {
            const w = Math.max(0, Math.min(100, Number(n) || 0));
            return `<span class="game-an-mix-seg ${cls}" style="flex:0 0 ${w}%;width:${w}%"></span>`;
        };
        const mixLab = (cls, name, n) => (
            `<span class="game-an-mix-lab ${cls}">`
            + `<i aria-hidden="true"></i>`
            + `${escHtml(name)}&nbsp;${Math.max(0, Number(n) || 0)}%`
            + `</span>`
        );
        const mixBar = (
            `<div class="game-an-mix" title="${escHtml(mixNote)}" data-mix-phys="${adPct}" data-mix-magic="${apPct}" data-mix-true="${truePct}">`
            + `<div class="game-an-mix-main">`
            + `<div class="game-an-mix-bar" role="img" aria-label="${escHtml(mixNote)}">`
            + mixSeg('is-phys', adPct)
            + mixSeg('is-magic', apPct)
            + mixSeg('is-true', truePct)
            + `</div>`
            + `<div class="game-an-mix-labels">`
            + mixLab('is-phys', copy.gameMixAd || 'AD', adPct)
            + mixLab('is-magic', copy.gameMixAp || 'AP', apPct)
            + mixLab('is-true', copy.gameMixTrue || 'True', truePct)
            + `</div>`
            + `</div></div>`
        );

        const legend = (
            `<div class="game-an-legend" aria-label="${escHtml(copy.gameAnalysisTitle || '')}">`
            + `<span class="game-an-legend-item is-yours">`
            + `<span class="game-an-swatch is-yours"></span>${escHtml(copy.gameAnalysisYours || '你的選擇')}`
            + `</span>`
            + `<span class="game-an-legend-item is-best">`
            + `<span class="game-an-swatch is-best"></span>${escHtml(copy.gameAnalysisBest || '最佳 5 人')}`
            + `</span>`
            + `</div>`
        );

        return (
            `<div class="game-analysis">`
            + `<div class="game-an-head">`
            + `<div class="game-an-head-left">`
            + `<div class="game-an-head-row">`
            + `<span class="game-an-head-title">${escHtml(copy.gameAnalysisTitle || '最強隊伍評價')}</span>`
            + `</div>`
            + mixBar
            + `</div>`
            + `<div class="game-an-wr-hero ${bestWrTone}" title="${escHtml(copy.gameBestEstWr || '最佳陣容勝率')}">`
            + `<span class="game-an-wr-num">${pct(bestEstWr)}</span>`
            + `<span class="game-an-wr-label">${escHtml(copy.gameBestEstWr || copy.gameOptimalWr || '最佳陣容勝率')}</span>`
            + `</div>`
            + `</div>`
            + `<div class="game-an-dims-split is-best-only">`
            + `<div class="game-an-radar-col">`
            + `<div class="game-an-radar draft-matchup-svg">${radar}</div>`
            + legend
            + `</div>`
            + `<div class="game-an-grades" aria-label="${escHtml(copy.gameAnalysisTitle || '')}">`
            + gradeRows
            + `</div>`
            + `</div>`
            + `</div>`
        );
    }

    /** One horizontal roster strip of faces (+ optional role mark on reveal). */
    function metaPickRosterFacesHtml(ids, opts) {
        const copy = opts.copy || tr();
        const pickedSet = opts.pickedSet || new Set();
        const bestSet = opts.bestSet || new Set();
        const markRoles = !!opts.markRoles;
        // 陣容對照：與主頁相同 — icon + tier 框 + 勝率左下角
        const homeStyle = !!opts.homeStyle || markRoles;
        const pinSet = opts.pinSet || new Set();
        const padTo = opts.padTo != null ? opts.padTo : META_PICK_NEED;
        const list = (ids || []).map(String);
        const tierColors = ((DATA && DATA.tiers) || {}).colors || {};
        const cells = [];
        for (let i = 0; i < padTo; i += 1) {
            const cid = list[i];
            if (!cid) {
                cells.push(`<div class="game-face is-empty" aria-hidden="true"><span>${i + 1}</span></div>`);
                continue;
            }
            const info = (DATA.champs && DATA.champs[cid]) || {};
            const name = champName(info, cid);
            const img = info.image
                ? `<img loading="lazy" src="${info.image}" alt="${escHtml(name)}">`
                : '';
            const wr = Number(info.wr);
            const role = markRoles ? metaPickChampRole(cid, pickedSet, bestSet) : '';
            const isPin = pinSet.has(String(cid));
            const tier = homeStyle ? draftAssignTier(wr) : '';
            const tierColor = (tier && tierColors[tier] && tierColors[tier].color) || '#555';
            const cls = [
                'game-face',
                homeStyle ? 'is-home' : '',
                role ? `is-${role}` : '',
                isPin ? 'is-pinned' : '',
            ].filter(Boolean).join(' ');
            // Pick phase: pin hint only. 陣容對照不顯示「選中」字樣（靠灰階/全彩區分）.
            let mark = '';
            if (!markRoles && isPin) {
                mark = `<span class="game-face-mark is-hint">${escHtml(copy.gameHintBadge)}</span>`;
            }
            const wrHtml = (homeStyle && Number.isFinite(wr))
                ? `<span class="wr">${pct(wr)}</span>`
                : '';
            const titleBits = [name];
            if (homeStyle && tier) titleBits.push(tier);
            if (homeStyle && Number.isFinite(wr)) titleBits.push(pct(wr));
            if (markRoles && role) titleBits.push(metaPickRoleLabel(role, copy));
            const styleAttr = homeStyle ? ` style="--tier-color:${tierColor}"` : '';
            const tierAttr = homeStyle && tier ? ` data-tier="${tier}"` : '';
            cells.push(
                `<div class="${cls}"${tierAttr}${styleAttr} title="${escHtml(titleBits.join(' · '))}">`
                + img
                + wrHtml
                + mark
                + `</div>`
            );
        }
        return `<div class="game-faces">${cells.join('')}</div>`;
    }

    function renderMetaPick() {
        const shell = document.querySelector('.view-game');
        if (!shell) return;
        metaPickEnsureDealt();
        const copy = tr();
        // Capture rank exactly once when a round reaches reveal.
        if (metaPick.phase === 'reveal') metaPickRecordRoundIfNeeded();
        const revealing = metaPick.phase === 'reveal';
        const settling = metaPickSession.settled && metaPickSession.rounds.length >= META_PICK_ROUNDS;
        const interactive = metaPick.phase === 'picking' && !settling;
        const pinned = new Set(metaPick.pinnedIds.map(String));
        const picked = new Set(metaPick.pickedIds.map(String));
        const optimal = new Set(metaPick.optimalIds.map(String));
        // After recording on reveal, rounds.length already includes this round.
        const displayRound = revealing
            ? Math.min(META_PICK_ROUNDS, metaPickSession.rounds.length)
            : Math.min(META_PICK_ROUNDS, metaPickSession.rounds.length + 1);

        // Bottom footer: lock / next / settle / miss-offer.
        const actions = document.getElementById('game-actions');
        if (actions) {
            if (settling) {
                actions.innerHTML = (
                    `<button type="button" class="tool-btn game-cta" id="game-restart">`
                    + `${escHtml(copy.gameRestart || copy.gamePlayAgain)}</button>`
                );
            } else if (metaPick.phase === 'reveal') {
                const done = metaPickSession.rounds.length >= META_PICK_ROUNDS;
                if (done) {
                    actions.innerHTML = (
                        `<button type="button" class="tool-btn game-cta" id="game-show-settle">`
                        + `${escHtml(copy.gameShowSettle || 'Settlement')}</button>`
                    );
                } else {
                    actions.innerHTML = (
                        `<button type="button" class="tool-btn game-cta" id="game-next-round">`
                        + `${escHtml(copy.gameNextRound || 'Next round')}</button>`
                    );
                }
            } else if (metaPick.phase === 'miss_offer') {
                const hit = Math.max(0, META_PICK_NEED - (Number(metaPick.missMissing) || 0));
                const scoreTxt = (copy.gameMissScore || ((h, t) => `答對：${h}/${t}`))(hit, META_PICK_NEED);
                actions.innerHTML = (
                    `<div class="game-miss-bar">`
                    + `<div class="game-miss-score">${escHtml(scoreTxt)}</div>`
                    + `<div class="game-miss-btns">`
                    + `<button type="button" class="tool-btn" id="game-hint">${escHtml(copy.gameHint)}</button>`
                    + `<button type="button" class="tool-btn ghost" id="game-reveal">${escHtml(copy.gameReveal)}</button>`
                    + `</div></div>`
                );
            } else {
                const canLock = metaPick.pickedIds.length === META_PICK_NEED;
                actions.innerHTML = (
                    `<button type="button" class="tool-btn game-cta" id="game-lock" ${canLock ? '' : 'disabled'}>`
                    + `${escHtml(copy.gameLock)}</button>`
                );
            }
        }
        const footer = document.getElementById('game-footer');
        if (footer) {
            footer.hidden = settling;
            footer.classList.toggle('is-miss', metaPick.phase === 'miss_offer');
        }

        const progress = document.getElementById('game-progress');
        if (progress) {
            const roundLabel = (copy.gameRoundOf || ((a, b) => `Round ${a}/${b}`))(
                Math.max(1, displayRound || 1),
                META_PICK_ROUNDS,
            );
            if (settling) {
                progress.hidden = false;
                progress.textContent = (copy.gameRoundOf || ((a, b) => `Round ${a}/${b}`))(
                    META_PICK_ROUNDS, META_PICK_ROUNDS,
                );
            } else if (metaPick.phase === 'miss_offer') {
                progress.hidden = false;
                progress.textContent = `${roundLabel} · ${copy.gamePickCount(metaPick.pickedIds.length, META_PICK_NEED)}`;
            } else if (revealing) {
                progress.hidden = false;
                progress.textContent = roundLabel;
            } else {
                progress.hidden = false;
                progress.textContent = `${roundLabel} · ${copy.gamePickCount(metaPick.pickedIds.length, META_PICK_NEED)}`;
            }
        }

        // Top notice: only auto-hint / other transient messages (not miss-offer).
        const notice = document.getElementById('game-notice');
        if (notice) {
            let html = '';
            if (metaPick.phase === 'miss_offer') {
                notice.hidden = true;
                notice.className = 'game-notice';
            } else if (metaPick.notice && !revealing) {
                html = escHtml(metaPick.notice);
                notice.hidden = false;
                notice.className = 'game-notice' + (metaPick.noticeKind ? ` is-${metaPick.noticeKind}` : '');
            } else {
                notice.hidden = true;
                notice.className = 'game-notice';
            }
            notice.innerHTML = html;
        }

        // During pick: show selected slots. During reveal: hide — comparison lives in result.
        const slots = document.getElementById('game-slots');
        if (slots) {
            if (revealing) {
                slots.hidden = true;
                slots.innerHTML = '';
            } else {
                slots.hidden = false;
                slots.innerHTML = metaPickRosterFacesHtml(metaPick.pickedIds, {
                    copy,
                    pinSet: pinned,
                    padTo: META_PICK_NEED,
                });
            }
        }

        const pool = document.getElementById('game-pool');
        if (pool) {
            if (revealing) {
                // Pool review is folded into the result panel for clearer hierarchy.
                pool.hidden = true;
                pool.innerHTML = '';
            } else if (!metaPick.poolIds.length) {
                pool.hidden = false;
                pool.innerHTML = `<div class="panel-empty">${escHtml(metaPick.notice || copy.gameWaitingData)}</div>`;
            } else {
                pool.hidden = false;
                pool.innerHTML = metaPick.poolIds.map(cid => {
                    const id = String(cid);
                    const info = DATA.champs[id] || {};
                    const name = champName(info, id);
                    const img = info.image ? `<img loading="lazy" src="${info.image}" alt="">` : '';
                    const isPin = pinned.has(id);
                    const isPicked = picked.has(id);
                    const cls = [
                        'game-tile',
                        isPicked ? 'is-picked' : '',
                        isPin ? 'is-pinned' : '',
                    ].filter(Boolean).join(' ');
                    // 提示可取消選取；僅在非選人階段 disabled
                    const disabled = !interactive ? ' disabled' : '';
                    const pinMark = isPin
                        ? `<span class="game-tile-mark">${escHtml(copy.gameHintBadge)}</span>`
                        : '';
                    return (
                        `<button type="button" class="${cls}" data-game-pick="${id}" role="option" `
                        + `aria-selected="${isPicked ? 'true' : 'false'}"${disabled}>`
                        + pinMark
                        + `<span class="game-tile-art">${img}</span>`
                        + `<span class="game-tile-name">${escHtml(name)}</span>`
                        + `</button>`
                    );
                }).join('');
            }
        }

        const settle = document.getElementById('game-settle');
        if (settle) {
            if (settling) {
                settle.hidden = false;
                settle.innerHTML = metaPickSettlementHtml(copy);
            } else {
                settle.hidden = true;
                settle.innerHTML = '';
            }
        }

        const result = document.getElementById('game-result');
        if (result) {
            if (!revealing || settling) {
                result.hidden = true;
                result.innerHTML = '';
            } else {
                // Always re-score both with evaluateFullTeam (order-invariant pair
                // lifts). Do not prefer a stale optimalScore from deal-time if the
                // scorer changed; same 5-set must print the same WR.
                const userScore = metaPickScoreTeam(metaPick.pickedIds);
                const bestScore = metaPickScoreTeam(metaPick.optimalIds);
                // Perfect roster → scores must match; guard against any residual drift.
                const perfectHit = metaPickSetsEqual(metaPick.pickedIds, metaPick.optimalIds);
                const showUser = perfectHit ? bestScore : userScore;
                let scores = metaPick.allScores;
                if (!scores || !scores.length) {
                    const all = metaPickScoreAllTeams(metaPick.poolIds, META_PICK_NEED);
                    scores = all.scores;
                    metaPick.allScores = scores;
                    metaPick.comboTotal = scores.length;
                    metaPick.optimalScore = all.score;
                }
                const rankInfo = metaPickRankAmong(showUser, scores);
                const deltaPp = (showUser - bestScore) * 100;
                const deltaTxt = (deltaPp >= 0 ? '+' : '') + deltaPp.toFixed(1) + ' pp';
                // Unique #1 (or perfect set): show gold "#1/252" — mid-rank PR formula
                // only reaches ~99.8 for C(10,5)=252 and feels wrong next to「完全正確」.
                const isRankOne = perfectHit || rankInfo.rank === 1;
                const prTxt = (copy.gamePrValue || (p => `PR ${p}`))(rankInfo.pr);
                const rankTxt = (copy.gameRankOf || ((r, t) => `#${r} / ${t}`))(rankInfo.rank, rankInfo.total);
                const rankTopTxt = `#${rankInfo.rank}/${rankInfo.total}`;

                // Keep the user's pick order; align best-5 into the same slots
                // (shared champs stay put; only-best fill the missed slots).
                const yourIds = metaPick.pickedIds.map(String);
                const bestIds = metaPickAlignBestToUser(yourIds, metaPick.optimalIds);

                const yourFaces = metaPickRosterFacesHtml(yourIds, {
                    copy, pickedSet: picked, bestSet: optimal, markRoles: true, padTo: META_PICK_NEED,
                });
                const bestFaces = metaPickRosterFacesHtml(bestIds, {
                    copy, pickedSet: picked, bestSet: optimal, markRoles: true, padTo: META_PICK_NEED,
                });

                const ranked = metaPick.poolIds.slice().sort((a, b) => {
                    const d = Number(DATA.champs[b].wr) - Number(DATA.champs[a].wr);
                    if (d) return d;
                    return String(a).localeCompare(String(b), undefined, { numeric: true });
                });
                // 塗鴉風白圈：選取 — 緊貼頭像+名字
                const doodleRing = (
                    `<svg class="game-review-doodle" viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true" focusable="false">`
                    + `<ellipse cx="50" cy="20" rx="46" ry="15.5" fill="none" stroke="currentColor"`
                    + ` stroke-width="2.35" stroke-linecap="round"`
                    + ` vector-effect="non-scaling-stroke"`
                    + ` transform="rotate(-3.5 50 20)"/>`
                    + `<ellipse cx="50.5" cy="20.5" rx="44.5" ry="14.2" fill="none" stroke="currentColor"`
                    + ` stroke-width="1.15" stroke-linecap="round" opacity="0.55"`
                    + ` vector-effect="non-scaling-stroke"`
                    + ` transform="rotate(2 50 20)"/>`
                    + `</svg>`
                );
                const listHtml = ranked.map((cid, i) => {
                    const id = String(cid);
                    const info = DATA.champs[id] || {};
                    const name = champName(info, id);
                    const img = info.image
                        ? `<img loading="lazy" src="${info.image}" alt="">`
                        : '<span class="game-review-ph"></span>';
                    const wr = Number(info.wr);
                    const role = metaPickChampRole(id, picked, optimal);
                    const isPicked = role === 'yours' || role === 'both';
                    const isCorrect = role === 'best' || role === 'both';
                    // 選取=白圈 · 正解=黃字+全彩 · 未選=灰階
                    return (
                        `<li class="game-review-row is-${role}" title="${escHtml(metaPickRoleLabel(role, copy))}">`
                        + `<span class="game-review-rank">${i + 1}</span>`
                        + `<span class="game-review-main${isPicked ? ' is-picked' : ''}${isCorrect ? ' is-correct' : ''}">`
                        + `<span class="game-review-face">${img}</span>`
                        + `<span class="game-review-name">${escHtml(name)}</span>`
                        + (isPicked ? doodleRing : '')
                        + `</span>`
                        + `<span class="game-review-wr">${Number.isFinite(wr) ? pct(wr) : '—'}</span>`
                        + `</li>`
                    );
                }).join('');

                const noticeLine = metaPick.notice
                    ? `<div class="game-result-banner is-${metaPick.noticeKind || 'ok'}">${escHtml(metaPick.notice)}</div>`
                    : '';

                result.hidden = false;
                result.innerHTML = (
                    noticeLine
                    + `<div class="game-metrics" role="group" aria-label="${escHtml(copy.gameGrade)}">`
                    + `<div class="game-metric">`
                    + `<span class="game-metric-label">${escHtml(copy.gameYourWr)}</span>`
                    + `<span class="game-metric-value ${metaPickWrToneClass(showUser)}">${pct(showUser)}</span>`
                    + `</div>`
                    + `<div class="game-metric">`
                    + `<span class="game-metric-label">${escHtml(copy.gameOptimalWr)}</span>`
                    + `<span class="game-metric-value ${metaPickWrToneClass(bestScore)}">${pct(bestScore)}</span>`
                    + `<span class="game-metric-sub">${escHtml(deltaTxt)}</span>`
                    + `</div>`
                    + `<div class="game-metric">`
                    + `<span class="game-metric-label">${escHtml(copy.gamePr || 'PR')}</span>`
                    + (isRankOne
                        // Gold #1/252 as the hero number — no mid-99.x PR, no duplicate sub-rank.
                        ? `<span class="game-metric-value is-rank-top" title="${escHtml(prTxt)} · ${escHtml(rankInfo.grade)}">${escHtml(rankTopTxt)}</span>`
                        : (`<span class="game-metric-value">${escHtml(prTxt)} <span class="game-metric-grade ${metaPickGradeClass(rankInfo.grade)}">${escHtml(rankInfo.grade)}</span></span>`
                            + `<span class="game-metric-sub ${metaPickGradeClass(rankInfo.grade)}">${escHtml(rankTxt)}</span>`))
                    + `</div>`
                    + `</div>`

                    + `<div class="game-compare">`
                    + `<div class="game-compare-head">${escHtml(copy.gameCompareTitle || '陣容對照')}</div>`
                    + `<div class="game-compare-row is-yours">`
                    + `<div class="game-compare-meta">`
                    + `<span class="game-compare-label">${escHtml(copy.gameYourTeam || '你的選擇')}</span>`
                    + `<span class="game-compare-wr ${metaPickWrToneClass(showUser)}">${pct(showUser)}</span>`
                    + `</div>`
                    + yourFaces
                    + `</div>`
                    + `<div class="game-compare-row is-best">`
                    + `<div class="game-compare-meta">`
                    + `<span class="game-compare-label">${escHtml(copy.gameBestTeam || '最佳 5 人')}</span>`
                    + `<span class="game-compare-wr ${metaPickWrToneClass(bestScore)}">${pct(bestScore)}</span>`
                    + `</div>`
                    + bestFaces
                    + `</div>`
                    + `</div>`

                    + `<div class="game-panels">`
                    + `<div class="game-tabs" role="tablist" aria-label="${escHtml(copy.gameTabPool || 'tabs')}">`
                    + `<button type="button" class="game-tab is-active" role="tab" aria-selected="true" data-game-tab="pool">`
                    + `${escHtml(copy.gameTabPool || copy.gamePoolReview || '英雄池')}</button>`
                    + `<button type="button" class="game-tab" role="tab" aria-selected="false" data-game-tab="analysis">`
                    + `${escHtml(copy.gameTabAnalysis || '隊伍分析')}</button>`
                    + `</div>`
                    + `<div class="game-tab-panel is-active" data-game-tab-panel="pool" role="tabpanel">`
                    + `<ol class="game-review-list">${listHtml}</ol>`
                    + `<div class="game-legend game-legend-under-pool" aria-label="legend">`
                    + `<span class="game-legend-item is-picked"><i></i>${escHtml(copy.gameLegendPicked || '選取（白色圓圈）')}</span>`
                    + `<span class="game-legend-item is-neither"><i></i>${escHtml(copy.gameLegendNeither || '未選（灰階）')}</span>`
                    + `<span class="game-legend-item is-correct">${escHtml(copy.gameLegendCorrect || '最佳（黃字）')}</span>`
                    + `</div>`
                    + `</div>`
                    + `<div class="game-tab-panel" data-game-tab-panel="analysis" role="tabpanel" hidden>`
                    + metaPickAnalysisHtml(yourIds, bestIds, copy)
                    + `</div>`
                    + `</div>`
                );
            }
        }

        // Hide pick chrome while settlement is front-and-center.
        if (settling) {
            const poolEl = document.getElementById('game-pool');
            if (poolEl) { poolEl.hidden = true; poolEl.innerHTML = ''; }
            const slotsEl = document.getElementById('game-slots');
            if (slotsEl) { slotsEl.hidden = true; slotsEl.innerHTML = ''; }
            const noticeEl = document.getElementById('game-notice');
            if (noticeEl) { noticeEl.hidden = true; noticeEl.innerHTML = ''; }
        }

        // Leaderboard under the game (best-effort; no-op if base empty).
        metaPickLoadLeaderboard();
    }

    function setDraftSide(side) {
        draftSide = side === 'enemy' ? 'enemy' : 'ally';
        pickNotice = '';
        renderDraft();
    }

    function toggleDraftPick(cid) {
        cid = String(cid);
        pickNotice = '';
        // Continuous 10-pick: after ally is full, next adds land on enemy.
        let side = draftSide;
        let list = draftPickList(side);
        let other = side === 'enemy' ? teamPicks : enemyPicks;
        const idx = list.indexOf(cid);
        if (idx !== -1) {
            list.splice(idx, 1);
            renderDraft();
            return;
        }
        if (other.includes(cid)) {
            pickNotice = tr().draftOnOtherSide || tr().maxOnly(MAX_TEAM_PICKS);
            renderDraft();
            return;
        }
        if (list.length >= MAX_TEAM_PICKS) {
            if (
                side === 'ally'
                && enemyPicks.length < MAX_TEAM_PICKS
                && !enemyPicks.includes(cid)
                && !teamPicks.includes(cid)
            ) {
                draftSide = 'enemy';
                side = 'enemy';
                list = enemyPicks;
                other = teamPicks;
            } else {
                pickNotice = tr().maxOnly(MAX_TEAM_PICKS);
                renderDraft();
                return;
            }
        }
        list.push(cid);
        // After 5th ally, hand off targeting so the next click is enemy.
        if (
            side === 'ally'
            && teamPicks.length >= MAX_TEAM_PICKS
            && enemyPicks.length < MAX_TEAM_PICKS
        ) {
            draftSide = 'enemy';
        }
        // Keep teamPicks as ally for aggregateRecommendations.
        renderDraft();
    }

    function clearDraft() {
        teamPicks = [];
        enemyPicks = [];
        draftSide = 'ally';
        pickNotice = '';
        renderDraft();
    }

    function renderSidePanel() {
        const copy = tr();
        const shell = document.querySelector('.app-shell');
        const panel = document.getElementById('side-panel');
        const fab = document.getElementById('rec-fab');
        const slots = document.getElementById('pick-slots');
        const note = document.getElementById('pick-note');
        const recList = document.getElementById('rec-list');
        if (!shell || !panel || !slots || !note || !recList) return;

        const showPanel = recommendMode && teamPicks.length > 0;
        const isFullTeam = teamPicks.length >= MAX_TEAM_PICKS;
        const isMobile = window.matchMedia('(max-width: 700px)').matches;
        if (!showPanel || !isMobile) recModalOpen = false;
        shell.classList.toggle('with-side-panel', showPanel && !isMobile);
        document.body.classList.toggle('rec-modal-open', showPanel && isMobile && recModalOpen);
        panel.classList.toggle('is-modal-open', showPanel && isMobile && recModalOpen);
        panel.classList.toggle('is-hidden', !showPanel || (isMobile && !recModalOpen));
        panel.classList.toggle('is-full-team', showPanel && isFullTeam);
        if (fab) {
            fab.classList.toggle('is-hidden', !(showPanel && isMobile && !recModalOpen));
            fab.textContent = copy.openRecs(teamPicks.length);
        }
        // Title / subtitle swap when the roster is complete.
        const sideTitle = document.getElementById('side-title');
        const sideSub = document.getElementById('side-sub');
        if (sideTitle) {
            sideTitle.textContent = isFullTeam
                ? (copy.sideTitleFull || copy.sideTitle)
                : copy.sideTitle;
        }
        if (sideSub) {
            sideSub.innerHTML = isFullTeam
                ? (copy.sideSubFull || copy.sideSub)
                : copy.sideSub;
        }
        if (!showPanel) return;

        const chips = [];
        teamPicks.forEach(cid => {
            const info = DATA.champs[cid];
            const name = info ? champName(info, cid) : ('#' + cid);
            const image = info && info.image ? info.image : '';
            chips.push(
                `<button class="pick-chip" type="button" data-remove-cid="${cid}" title="${escHtml(copy.removePick(name))}">` +
                (image ? `<img loading="lazy" src="${image}" alt="">` : '') +
                `<span>${escHtml(name)}</span></button>`
            );
        });
        for (let i = teamPicks.length; i < MAX_TEAM_PICKS; i += 1) {
            chips.push(`<div class="pick-chip empty"><span>${copy.pickEmpty}</span></div>`);
        }
        slots.innerHTML = chips.join('');

        if (isFullTeam) {
            // Don't keep a sticky "max 5" notice once the roster is full.
            pickNotice = '';
            note.textContent = copy.teamPickFullNote || '';
            recList.innerHTML = buildTeamEvalHtml(teamPicks);
            return;
        }

        const recs = aggregateRecommendations();
        const want = teamPicks.length;
        const hasFull = recs.some(row => row.full);
        if (pickNotice) {
            note.textContent = pickNotice;
        } else if (!teamPicks.length) {
            note.textContent = copy.pickNoteEmpty(MAX_TEAM_PICKS);
        } else if (want > 1 && !hasFull) {
            note.textContent = copy.pickNotePartial(want);
        } else {
            note.textContent = copy.pickNoteReady(want, DATA.min_synergy_games);
        }

        if (!teamPicks.length) {
            recList.innerHTML = `<div class="panel-empty">${copy.panelEmpty}</div>`;
            return;
        }
        if (!recs.length) {
            recList.innerHTML = `<div class="panel-empty">${copy.panelNoData}</div>`;
            return;
        }

        recList.innerHTML = recommendationDisplayRows(recs).map((row, idx) => {
            const info = DATA.champs[row.id];
            const name = info ? champName(info, row.id) : ('#' + row.id);
            const image = info && info.image ? info.image : '';
            const confidence = confidenceLabel(row);
            const meta = recMetaHtml(row, name);
            const title = row.leastFit
                ? copy.leastFitRowTitle(name, signed(row.fitScore), signed(row.pairFitScore), signed(row.compositionContribution), confidence)
                : copy.recRowTitle(name, signed(row.fitScore), signed(row.pairFitScore), signed(row.compositionContribution), confidence);
            return `
                <button class="rec-row${row.leastFit ? ' least-fit' : ''}" type="button" data-cid="${row.id}" title="${escHtml(title)}">
                    <span class="rec-rank">${idx + 1}</span>
                    ${image ? `<img loading="lazy" src="${image}" alt="">` : '<div style="width:40px;height:40px;border-radius:8px;background:var(--hover)"></div>'}
                    <span class="rec-main">
                        <span class="rec-meta">${meta}</span>
                    </span>
                </button>
            `;
        }).join('');
    }

    function updateChampCardCopy() {
        document.querySelectorAll('.champ').forEach(champ => {
            const cid = champ.getAttribute('data-cid');
            const info = DATA.champs[cid];
            if (!info) return;
            const name = champName(info, cid);
            const alias = info.alias || '';
            const tier = champ.getAttribute('data-tier') || '';
            const wr = champ.getAttribute('data-wr') || '';
            const games = champ.getAttribute('data-games') || '';
            const raw = champ.getAttribute('data-raw-wr') || '';
            const nameEl = champ.querySelector('.name');
            if (nameEl) nameEl.textContent = name;
            champ.setAttribute('title', tr().champCardTitle(name, wr, games, raw));
            champ.setAttribute('aria-label', tr().champCardAria(name, alias, tier, wr));
        });
    }

    function changeLabels() {
        const changes = DATA.patchChanges || {};
        const range = changes.baselinePatch && changes.currentPatch
            ? `${changes.baselinePatch} -> ${changes.currentPatch}`
            : PATCH_LABEL;
        if (currentLang === 'en') {
            return {
                button: 'Patch changes',
                kicker: range,
                title: 'What moved this patch',
                close: 'Close patch changes',
                tabs: { heroes: 'Heroes', augments: 'Augments', items: 'Items', champItems: 'Hero x item' },
                summaryBase: 'Compared with',
                summarySample: 'Sample',
                summaryRule: 'Signal',
                summaryRuleText: `heroes >= ${changes.minHeroGames || 0} games`,
                noData: 'No baseline patch data is available for this build.',
                heroUp: 'Biggest hero climbs',
                heroDown: 'Biggest hero drops',
                itemUp: 'Item win-rate climbs',
                itemDown: 'Item win-rate drops',
                augmentUp: 'Augment win-rate climbs',
                augmentDown: 'Augment win-rate drops',
                augmentNote: 'By augment picks this patch; only augments with enough sample in both patches are shown.',
                champItemUp: 'Hero-item spikes',
                champItemDown: 'Hero-item slumps',
                itemNote: 'Core items only; boots and augment-gated rewards are excluded. Hero x item compares item lift against that hero baseline.',
                games: 'games',
                uses: 'uses',
                lift: 'lift',
            };
        }
        const zhLabels = {
            button: '版本變動',
            kicker: range,
            title: '這版誰變多了',
            close: '關閉版本變動',
            tabs: { heroes: '英雄', augments: '增幅', items: '裝備', champItems: '英雄×裝備' },
            summaryBase: '比較基準',
            summarySample: '樣本',
            summaryRule: '訊號門檻',
            summaryRuleText: `英雄 >= ${changes.minHeroGames || 0} 場`,
            noData: '這次 build 沒有可比較的上一版資料。',
            heroUp: '勝率提升最多',
            heroDown: '勝率下降最多',
            itemUp: '裝備勝率提升',
            itemDown: '裝備勝率下降',
            augmentUp: '增幅勝率提升',
            augmentDown: '增幅勝率下降',
            augmentNote: '依本版增幅選用次數比較；只列兩版樣本都足夠的增幅。',
            champItemUp: '搭配突然變好',
            champItemDown: '搭配突然變差',
            itemNote: '只看核心裝備，不含鞋子與增幅限定獎勵；英雄×裝備比較的是相對該英雄 baseline 的 lift 變動。',
            games: '場',
            uses: '次',
            lift: 'lift',
        };
        return currentLang === 'zh-CN' ? localizeZhCN(zhLabels) : zhLabels;
    }

    function fmtInt(n) {
        return Number(n || 0).toLocaleString(currentLang === 'en' ? 'en-US' : (currentLang === 'zh-CN' ? 'zh-CN' : 'zh-TW'));
    }

    function localizedEntityName(entity) {
        if (!entity) return '';
        if (currentLang === 'en') {
            return entity.name_en || entity.alias || entity.name || entity.id || '';
        }
        if (currentLang === 'zh-CN') {
            const id = entity.id != null ? String(entity.id) : '';
            // Champ / item / augment official CN when we have an id.
            if (id && NAMES_ZH_CN) {
                if (NAMES_ZH_CN.champs && NAMES_ZH_CN.champs[id]) return NAMES_ZH_CN.champs[id];
                const itemCn = itemCnName(id);
                if (itemCn) return itemCn;
                const augRow = NAMES_ZH_CN.augs && NAMES_ZH_CN.augs[id];
                if (augRow && augRow.n) return augRow.n;
            }
            if (entity.name_cn) return entity.name_cn;
            // Compose from nested items (item-pair change rows).
            if (Array.isArray(entity.items) && entity.items.length) {
                const parts = entity.items.map(it => itemDisplayName(it)).filter(Boolean);
                if (parts.length) return parts.join(' + ');
            }
            return t2s(entity.name_zh || entity.name || entity.name_en || entity.alias || id || '');
        }
        return entity.name_zh || entity.name || entity.name_en || entity.alias || entity.id || '';
    }

    function changeDeltaClass(value) {
        return Number(value || 0) >= 0 ? 'up' : 'down';
    }

    function changeHeroRow(row) {
        const labels = changeLabels();
        const name = localizedEntityName(row);
        const title = `${name} ${signed(row.delta || 0)}`;
        const meta = `${pct(row.baseline_wr || 0)} -> ${pct(row.current_wr || 0)} · ${fmtInt(row.current_games)} ${labels.games} · ${row.baseline_tier || ''}->${row.current_tier || ''}`;
        return `
            <button class="change-row" type="button" data-change-cid="${row.id}" title="${escHtml(title)}">
                <img class="change-icon" src="${escHtml(row.image || '')}" alt="">
                <span>
                    <span class="change-name">${escHtml(name)}</span>
                    <span class="change-meta">${escHtml(meta)}</span>
                </span>
                <span class="change-delta ${changeDeltaClass(row.delta)}">${signed(row.delta || 0)}</span>
            </button>
        `;
    }

    function changeItemRow(row) {
        const labels = changeLabels();
        const name = localizedEntityName(row);
        const title = `${name} ${signed(row.delta || 0)}`;
        const meta = `${pct(row.baseline_wr || 0)} -> ${pct(row.current_wr || 0)} · ${fmtInt(row.current_games)} ${labels.uses}`;
        return `
            <div class="change-row" title="${escHtml(title)}">
                <img class="change-icon" src="${escHtml(row.icon || '')}" alt="">
                <span>
                    <span class="change-name">${escHtml(name)}</span>
                    <span class="change-meta">${escHtml(meta)}</span>
                </span>
                <span class="change-delta ${changeDeltaClass(row.delta)}">${signed(row.delta || 0)}</span>
            </div>
        `;
    }

    function changeChampItemRow(row) {
        const labels = changeLabels();
        const champ = row.champ || {};
        const item = row.item || {};
        const champName = localizedEntityName(champ);
        const itemName = localizedEntityName(item);
        const title = `${champName} + ${itemName} ${signed(row.delta || 0)}`;
        const meta = `${labels.lift} ${signed(row.baseline_lift || 0)} -> ${signed(row.current_lift || 0)} · WR ${pct(row.current_wr || 0)} · ${fmtInt(row.current_games)} ${labels.uses}`;
        return `
            <button class="change-row" type="button" data-change-cid="${champ.id}" title="${escHtml(title)}">
                <span class="change-duo">
                    <img src="${escHtml(champ.image || '')}" alt="">
                    <img src="${escHtml(item.icon || '')}" alt="">
                </span>
                <span>
                    <span class="change-name">${escHtml(champName)} + ${escHtml(itemName)}</span>
                    <span class="change-meta">${escHtml(meta)}</span>
                </span>
                <span class="change-delta ${changeDeltaClass(row.delta)}">${signed(row.delta || 0)}</span>
            </button>
        `;
    }

    function changeColumn(title, rows, renderer) {
        const labels = changeLabels();
        const body = rows && rows.length
            ? rows.map(renderer).join('')
            : `<div class="change-empty">${escHtml(labels.noData)}</div>`;
        return `
            <div class="change-column">
                <h3 class="change-column-title">${escHtml(title)}</h3>
                <div class="change-list">${body}</div>
            </div>
        `;
    }

    function renderChangeTabContent(changes, labels) {
        if (!changes || !changes.currentPatch) {
            return `<div class="change-empty">${escHtml(labels.noData)}</div>`;
        }
        if (activeUpdateTab === 'items') {
            return `
                <div class="change-grid">
                    ${changeColumn(labels.itemUp, changes.itemRisers || [], changeItemRow)}
                    ${changeColumn(labels.itemDown, changes.itemFallers || [], changeItemRow)}
                </div>
                <div class="change-meta" style="margin-top:10px">${escHtml(labels.itemNote)}</div>
            `;
        }
        if (activeUpdateTab === 'champItems') {
            return `
                <div class="change-grid">
                    ${changeColumn(labels.champItemUp, changes.champItemRisers || [], changeChampItemRow)}
                    ${changeColumn(labels.champItemDown, changes.champItemFallers || [], changeChampItemRow)}
                </div>
                <div class="change-meta" style="margin-top:10px">${escHtml(labels.itemNote)}</div>
            `;
        }
        if (activeUpdateTab === 'augments') {
            return `
                <div class="change-grid">
                    ${changeColumn(labels.augmentUp, changes.augmentRisers || [], changeItemRow)}
                    ${changeColumn(labels.augmentDown, changes.augmentFallers || [], changeItemRow)}
                </div>
                <div class="change-meta" style="margin-top:10px">${escHtml(labels.augmentNote)}</div>
            `;
        }
        return `
            <div class="change-grid">
                ${changeColumn(labels.heroUp, changes.heroRisers || [], changeHeroRow)}
                ${changeColumn(labels.heroDown, changes.heroFallers || [], changeHeroRow)}
            </div>
        `;
    }

    function renderUpdatesPanel() {
        const copy = tr();
        const labels = changeLabels();
        const changes = DATA.patchChanges || {};
        if (!['heroes', 'augments', 'items', 'champItems'].includes(activeUpdateTab)) {
            activeUpdateTab = 'heroes';
        }
        const button = document.getElementById('updates-toggle');
        const panel = document.getElementById('updates-panel');
        const kicker = document.getElementById('updates-kicker');
        const title = document.getElementById('updates-title');
        const close = document.getElementById('updates-close');
        const list = document.getElementById('updates-list');
        if (button) {
            button.textContent = labels.button || copy.updatesButton;
            button.setAttribute('aria-expanded', updatesOpen ? 'true' : 'false');
        }
        if (kicker) kicker.textContent = labels.kicker || copy.updatesKicker;
        if (title) title.textContent = labels.title || copy.updatesTitle;
        if (close) close.setAttribute('aria-label', labels.close || copy.updatesClose);
        if (list) {
            const tabHtml = Object.entries(labels.tabs).map(([key, label]) => `
                <button class="change-tab${activeUpdateTab === key ? ' active' : ''}" type="button"
                        data-change-tab="${key}" aria-pressed="${activeUpdateTab === key ? 'true' : 'false'}">
                    ${escHtml(label)}
                </button>
            `).join('');
            const summary = changes.currentPatch ? `
                <div class="change-summary">
                    <span class="change-chip">${escHtml(labels.summaryBase)} ${escHtml(changes.baselinePatch || '')}</span>
                    <span class="change-chip">${escHtml(labels.summarySample)} ${fmtInt(changes.currentGames)} / ${fmtInt(changes.baselineGames)}</span>
                    <span class="change-chip">${escHtml(labels.summaryRule)} ${escHtml(labels.summaryRuleText)}</span>
                </div>
            ` : '';
            list.innerHTML = `
                ${summary}
                <div class="change-tabs" role="tablist">${tabHtml}</div>
                ${renderChangeTabContent(changes, labels)}
            `;
        }
    }

    function articleField(a, base) {
        if (currentLang === 'en') return a[base + '_en'] || a[base + '_zh'] || '';
        const zh = a[base + '_zh'] || a[base + '_en'] || '';
        // body_* is HTML; still safe to run char-level t2s on tags/attributes that don't use trad chars.
        return currentLang === 'zh-CN' ? t2s(zh) : zh;
    }
    // ---- Column cover art: an auto-generated 16:9 SVG "首圖" per article ----
    // We have no splash art, so each cover is a themed vector poster built from
    // the article's own fields: an accent colour, a short poster "hook" (lines
    // split on '|'), and a data-flavoured motif that previews the piece.  Pure
    // vector => crisp, tiny, theme-agnostic; a new article gets a cover for
    // free (just pick cover_motif + cover_accent + cover_zh/en).
    function _coverLines(s) {
        return String(s || '').split('|').map(t => t.trim()).filter(Boolean);
    }
    function _coverMotif(kind, acc) {
        const red = '#e2574b';
        if (kind === 'diverge') {
            // Two trend lines splitting from a shared point: rewards-skill (up,
            // accent) vs farms-low-elo (down, red).
            return '<g fill="none" stroke-linecap="round" stroke-linejoin="round">'
                + '<polyline points="222,150 286,116 356,54" stroke="' + acc + '" stroke-width="5"/>'
                + '<polyline points="222,150 286,160 356,194" stroke="' + red + '" stroke-width="5" opacity="0.92"/>'
                + '<circle cx="356" cy="54" r="7" fill="' + acc + '"/>'
                + '<circle cx="356" cy="194" r="7" fill="' + red + '"/>'
                + '<circle cx="222" cy="150" r="4.5" fill="#cfd6df"/></g>';
        }
        if (kind === 'scatter') {
            // The four-quadrant champion map, in miniature: dashed medians +
            // win-rate-coloured dots.
            const dots = [[250,68,'#48b868'],[300,90,'#48b868'],[214,96,'#d0b23a'],[344,62,'#48b868'],
                [276,128,'#d0b23a'],[330,150,'#d64545'],[236,150,'#d64545'],[362,110,'#48b868'],
                [298,176,'#d64545'],[206,140,'#d0b23a'],[256,168,'#d64545'],[320,120,'#48b868'],
                [230,116,'#d0b23a'],[348,182,'#d64545']]
                .map(d => '<circle cx="' + d[0] + '" cy="' + d[1] + '" r="6" fill="' + d[2] + '" opacity="0.92" stroke="#0b0e14" stroke-width="1.5"/>').join('');
            return '<g><line x1="284" y1="38" x2="284" y2="206" stroke="' + acc + '" stroke-width="1.5" stroke-dasharray="4 5" opacity="0.5"/>'
                + '<line x1="178" y1="122" x2="394" y2="122" stroke="' + acc + '" stroke-width="1.5" stroke-dasharray="4 5" opacity="0.5"/>'
                + dots + '</g>';
        }
        if (kind === 'blade') {
            // A bold diagonal slash with a motion trail and a white glint — the
            // dash-strike of "Draw Your Sword".
            return '<g stroke-linecap="round">'
                + '<line x1="170" y1="202" x2="388" y2="40" stroke="' + acc + '" stroke-width="3" opacity="0.16"/>'
                + '<line x1="190" y1="202" x2="392" y2="52" stroke="' + acc + '" stroke-width="6" opacity="0.32"/>'
                + '<line x1="210" y1="204" x2="396" y2="66" stroke="' + acc + '" stroke-width="12" opacity="0.95"/>'
                + '<line x1="222" y1="194" x2="386" y2="76" stroke="#ffffff" stroke-width="2" opacity="0.8"/></g>';
        }
        // 'tiers' (default): a mini tier-list — S/A/B rows with placeholder cells.
        const rows = [['S','#ff5d5d',62],['A','#ffb13d',104],['B','#5dd0ff',146]];
        let out = '';
        for (const r of rows) {
            const y = r[2];
            out += '<rect x="214" y="' + (y-16) + '" width="30" height="30" rx="7" fill="' + r[1] + '"/>'
                + '<text x="229" y="' + (y+5) + '" text-anchor="middle" fill="#0b0e14" font-size="17" font-weight="800">' + r[0] + '</text>';
            for (let i = 0; i < 4; i++) {
                out += '<rect x="' + (256 + i*34) + '" y="' + (y-13) + '" width="26" height="26" rx="6" fill="#ffffff" opacity="' + (0.16 - i*0.032).toFixed(3) + '"/>';
            }
        }
        return '<g>' + out + '</g>';
    }
    function articleCover(a, uid, par) {
        const acc = a.cover_accent || '#f5c518';
        const motif = a.cover_motif || 'tiers';
        const hook = articleField(a, 'cover') || articleField(a, 'kicker');
        const kick = String(articleField(a, 'kicker') || '').toUpperCase();
        const lines = _coverLines(hook);
        const isCJK = /[㐀-鿿]/.test(hook);
        const longest = lines.reduce((m, l) => Math.max(m, l.length), 0);
        const fs = isCJK ? (longest <= 4 ? 46 : longest <= 6 ? 38 : 30)
                         : (longest <= 6 ? 44 : longest <= 10 ? 34 : 26);
        const lh = Math.round(fs * 1.12);
        const baseY = 198;
        const startY = baseY - (lines.length - 1) * lh;
        const tspans = lines.map((l, i) =>
            '<text x="30" y="' + (startY + i*lh) + '" fill="#ffffff" font-size="' + fs + '" font-weight="800" letter-spacing="' + (isCJK ? 1 : 0.4) + '">' + escHtml(l) + '</text>'
        ).join('');
        const par2 = par || 'xMidYMid slice';
        return '<svg class="cover-svg" viewBox="0 0 400 225" preserveAspectRatio="' + par2 + '" role="img" '
            + 'aria-label="' + escHtml(hook.replace(/\\|/g, ' ')) + '" xmlns="http://www.w3.org/2000/svg">'
            + '<defs>'
            + '<linearGradient id="cb-' + uid + '" x1="0" y1="0" x2="1" y2="1">'
            + '<stop offset="0" stop-color="#0b0e14"/><stop offset="1" stop-color="#161b25"/></linearGradient>'
            + '<radialGradient id="cg-' + uid + '" cx="0.78" cy="0.16" r="0.95">'
            + '<stop offset="0" stop-color="' + acc + '" stop-opacity="0.42"/>'
            + '<stop offset="0.5" stop-color="' + acc + '" stop-opacity="0.06"/>'
            + '<stop offset="1" stop-color="' + acc + '" stop-opacity="0"/></radialGradient>'
            + '<linearGradient id="cs-' + uid + '" x1="0" y1="1" x2="0.5" y2="0.2">'
            + '<stop offset="0" stop-color="#05070b" stop-opacity="0.92"/>'
            + '<stop offset="1" stop-color="#05070b" stop-opacity="0"/></linearGradient></defs>'
            + '<rect width="400" height="225" fill="url(#cb-' + uid + ')"/>'
            + '<rect width="400" height="225" fill="url(#cg-' + uid + ')"/>'
            + _coverMotif(motif, acc)
            + '<rect width="400" height="225" fill="url(#cs-' + uid + ')"/>'
            + '<text x="30" y="' + (startY - fs - 4) + '" fill="' + acc + '" font-size="12.5" font-weight="700" letter-spacing="2.4">' + escHtml(kick) + '</text>'
            + tspans
            + '</svg>';
    }
    // When an article ships a hand-made 16:9 banner (cover_image_zh/en, hosted
    // under assets/covers/) use it; otherwise fall back to the generated vector
    // cover. Bilingual — articleField picks the current language's image and the
    // column re-renders on a language switch, so the EN/ZH banner swaps with it.
    function articleCoverMedia(a, uid, par) {
        const src = articleField(a, 'cover_image');
        if (src) {
            return '<img class="cover-img" src="' + escHtml(src) + '" alt="'
                + escHtml(articleField(a, 'title')) + '" loading="lazy">';
        }
        return articleCover(a, uid, par);
    }
    function renderColumnList() {
        columnArticle = null;
        document.title = BASE_TITLE;
        if (_scatterRO) { _scatterRO.disconnect(); _scatterRO = null; }
        const host = document.getElementById('column-host');
        if (!host) return;
        const head = pickLang('專欄', 'Articles');
        const sub = pickLang('資料背後的思考與玩法解析。', 'The thinking behind the data, and how to play it.');
        const cards = ARTICLES.map(a => `
            <div class="article-card" data-article="${escHtml(a.id)}" role="button" tabindex="0"
                 aria-label="${escHtml(articleField(a, 'title'))}">
                <div class="article-cover">${articleCoverMedia(a, escHtml(a.id))}</div>
                <div class="article-card-body">
                    <span class="article-kicker">${escHtml(articleField(a, 'kicker'))}</span>
                    <h3>${escHtml(articleField(a, 'title'))}</h3>
                    <p>${escHtml(articleField(a, 'summary'))}</p>
                    <span class="article-meta">${escHtml(a.date)}</span>
                </div>
            </div>`).join('');
        host.innerHTML = `<h2 class="section-head">${escHtml(head)}</h2>`
            + `<p class="section-sub">${escHtml(sub)}</p>`
            + `<div class="article-list">${cards}</div>`;
    }
    function renderArticle(id) {
        const a = ARTICLES.find(x => x.id === id);
        if (!a) { renderColumnList(); return; }
        columnArticle = id;
        if (_scatterRO) { _scatterRO.disconnect(); _scatterRO = null; }
        const host = document.getElementById('column-host');
        if (!host) return;
        const back = pickLang('返回專欄', 'Back to Articles');
        const share = pickLang('複製連結', 'Copy link');
        host.innerHTML = `<div class="article-reader">`
            + `<div class="article-toolbar">`
            + `<button class="article-back" data-article-back type="button">← ${escHtml(back)}</button>`
            + `<button class="article-share" data-article-share type="button" aria-live="polite">${LINK_ICON}<span>${escHtml(share)}</span></button>`
            + `</div>`
            + `<div class="article-hero${articleField(a, 'cover_image') ? ' article-hero--img' : ''}">${articleCoverMedia(a, escHtml(a.id) + '-hero', 'xMinYMax slice')}</div>`
            + `<h1>${escHtml(articleField(a, 'title'))}</h1>`
            + `<div class="article-meta">${escHtml(articleField(a, 'kicker'))} · ${escHtml(a.date)}</div>`
            + `<div class="article-body">${articleField(a, 'body')}</div></div>`;
        document.title = articleField(a, 'title') + ' · ' + BASE_TITLE;
        if (document.getElementById('scatter-host')) renderScalingSnowballChart();
    }
    // Copy the article's deep link.  Clipboard API first; hidden-textarea
    // execCommand fallback keeps it working on http / older WebViews.
    // Prefer a clean path URL (no hash) so shared links look like
    // https://arammeta.com/column/sprees-not-snowball
    function copyArticleLink(btn) {
        const path = columnArticle
            ? pathForRoute('column', columnArticle)
            : pathForRoute('column');
        const url = location.origin + path;
        const done = () => {
            const label = btn.querySelector('span');
            if (!label) return;
            const prev = label.textContent;
            label.textContent = pickLang('已複製！', 'Copied!');
            btn.classList.add('copied');
            setTimeout(() => {
                label.textContent = prev;
                btn.classList.remove('copied');
            }, 1600);
        };
        const fallback = () => {
            const ta = document.createElement('textarea');
            ta.value = url;
            ta.style.cssText = 'position:fixed;opacity:0';
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); done(); } catch {}
            ta.remove();
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(url).then(done).catch(fallback);
        } else {
            fallback();
        }
    }
    // ---- Scaling x snowball scatter (rendered into the 'scaling-snowball' article) ----
    let _scatterRO = null, _scatterW = 0;
    function scatterWrColor(wr) {
        const t = Math.max(0, Math.min(1, (wr - 0.44) / (0.56 - 0.44)));
        const stops = [[0, [214, 69, 69]], [0.5, [208, 178, 58]], [1, [72, 184, 104]]];
        for (let i = 0; i < stops.length - 1; i++) {
            const [t0, c0] = stops[i], [t1, c1] = stops[i + 1];
            if (t <= t1) { const f = (t - t0) / (t1 - t0); return `rgb(${c0.map((v, k) => Math.round(v + (c1[k] - v) * f)).join(',')})`; }
        }
        return 'rgb(72,184,104)';
    }
    function renderScalingSnowballChart() {
        const host = document.getElementById('scatter-host');
        if (!host) return;
        const W = Math.max(320, Math.round(host.clientWidth || 700));
        const H = Math.max(440, Math.min(620, Math.round(W * 0.74)));
        const rows = [];
        for (const cid in (DATA.champs || {})) {
            const info = DATA.champs[cid], c = info && info.comp;
            if (!c) continue;
            const sx = Number(c.snowball), sy = Number(c.scaling);
            if (!isFinite(sx) || !isFinite(sy)) continue;
            rows.push({ name: champName(info, cid), img: info.image || '', wr: Number(info.wr) || 0, g: Number(info.g) || 0, sx, sy });
        }
        if (!rows.length) { host.innerHTML = ''; return; }
        const xs = rows.map(r => r.sx), ys = rows.map(r => r.sy);
        const med = arr => { const s = [...arr].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
        const xMin = Math.min(...xs), xMax = Math.max(...xs), yMin = Math.min(...ys), yMax = Math.max(...ys);
        const xPad = (xMax - xMin) * 0.06 || 0.1, yPad = (yMax - yMin) * 0.06 || 0.01;
        const X0 = xMin - xPad, X1 = xMax + xPad, Y0 = yMin - yPad, Y1 = yMax + yPad;
        const xMed = med(xs), yMed = med(ys);
        const M = { l: 46, r: 14, t: 14, b: 38 };
        const PW = W - M.l - M.r, PH = H - M.t - M.b;
        const px = v => M.l + (v - X0) / (X1 - X0) * PW;
        const py = v => H - M.b - (v - Y0) / (Y1 - Y0) * PH;
        const mx = px(xMed), my = py(yMed);
        const ICON = Math.max(18, Math.min(30, Math.round(W / 30)));
        // declutter: repulsion + spring back to true position (Gauss-Seidel)
        const pos = rows.map(r => [px(r.sx), py(r.sy)]);
        const tgt = pos.map(p => [p[0], p[1]]);
        for (let i = 0; i < pos.length; i++) { pos[i][0] += ((i * 53) % 7 - 3) * 0.3; pos[i][1] += ((i * 31) % 7 - 3) * 0.3; }
        const mind = ICON * 0.92;
        for (let it = 0; it < 180; it++) {
            for (let i = 0; i < pos.length; i++) {
                let dx = 0, dy = 0;
                for (let j = 0; j < pos.length; j++) {
                    if (i === j) continue;
                    const ax = pos[i][0] - pos[j][0], ay = pos[i][1] - pos[j][1];
                    const d = Math.hypot(ax, ay) || 0.01;
                    if (d < mind) { const f = (mind - d) / d * 0.5; dx += ax * f; dy += ay * f; }
                }
                dx += (tgt[i][0] - pos[i][0]) * 0.06;
                dy += (tgt[i][1] - pos[i][1]) * 0.06;
                pos[i][0] = Math.max(M.l + ICON / 2, Math.min(W - M.r - ICON / 2, pos[i][0] + dx));
                pos[i][1] = Math.max(M.t + ICON / 2, Math.min(H - M.b - ICON / 2, pos[i][1] + dy));
            }
        }
        const en = currentLang === 'en';
        const q = en
            ? { tr: 'Snowball + late', tl: 'Late game', br: 'Snowball', bl: 'Neither' }
            : { tr: zhUi('滾雪球強 · 後期強'), tl: zhUi('後期強'), br: zhUi('滾雪球強'), bl: zhUi('兩者皆弱') };
        const xTitle = en ? 'Snowball  →' : zhUi('滾雪球能力  →');
        const yTitle = en ? 'Late game  →' : zhUi('後期能力  →');
        let s = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" preserveAspectRatio="xMidYMid meet" aria-hidden="true" style="position:absolute;inset:0;pointer-events:none">`;
        s += `<defs><clipPath id="sc-panel"><rect x="${M.l}" y="${M.t}" width="${PW}" height="${PH}" rx="12"/></clipPath></defs>`;
        s += `<rect x="${M.l}" y="${M.t}" width="${PW}" height="${PH}" rx="12" fill="var(--surface-sunken)" stroke="var(--border)"/>`;
        s += `<g clip-path="url(#sc-panel)">`;
        s += `<rect x="${mx}" y="${M.t}" width="${W - M.r - mx}" height="${my - M.t}" fill="rgba(72,184,104,0.09)"/>`;
        s += `<rect x="${M.l}" y="${M.t}" width="${mx - M.l}" height="${my - M.t}" fill="rgba(80,150,230,0.07)"/>`;
        s += `<rect x="${mx}" y="${my}" width="${W - M.r - mx}" height="${H - M.b - my}" fill="rgba(230,150,60,0.07)"/>`;
        s += `<line x1="${mx}" y1="${M.t}" x2="${mx}" y2="${H - M.b}" stroke="var(--border-strong)" stroke-width="1" stroke-dasharray="5 5"/>`;
        s += `<line x1="${M.l}" y1="${my}" x2="${W - M.r}" y2="${my}" stroke="var(--border-strong)" stroke-width="1" stroke-dasharray="5 5"/>`;
        s += `</g>`;
        const qc = 'fill="var(--text-muted)" font-size="12.5" font-weight="600" opacity="0.85"';
        s += `<text x="${W - M.r - 8}" y="${M.t + 18}" text-anchor="end" ${qc}>${q.tr}</text>`;
        s += `<text x="${M.l + 8}" y="${M.t + 18}" ${qc}>${q.tl}</text>`;
        s += `<text x="${W - M.r - 8}" y="${H - M.b - 8}" text-anchor="end" ${qc}>${q.br}</text>`;
        s += `<text x="${M.l + 8}" y="${H - M.b - 8}" ${qc}>${q.bl}</text>`;
        const tc = 'fill="var(--text-dim)" font-size="11"';
        [1.0, 1.5, 2.0, 2.5, 3.0, 3.5].forEach(v => { if (v >= X0 && v <= X1) s += `<text x="${px(v).toFixed(1)}" y="${H - M.b + 16}" text-anchor="middle" ${tc}>${v.toFixed(1)}</text>`; });
        [-0.10, -0.05, 0, 0.05, 0.10, 0.15].forEach(v => { if (v >= Y0 && v <= Y1) s += `<text x="${M.l - 8}" y="${(py(v) + 4).toFixed(1)}" text-anchor="end" ${tc}>${(v > 0 ? '+' : '') + (v * 100).toFixed(0)}%</text>`; });
        s += `<text x="${(M.l + (W - M.r)) / 2}" y="${H - 4}" text-anchor="middle" fill="var(--text-muted)" font-size="12" font-weight="600">${xTitle}</text>`;
        const yc = M.t + PH / 2;
        s += `<text x="14" y="${yc}" text-anchor="middle" transform="rotate(-90 14 ${yc})" fill="var(--text-muted)" font-size="12" font-weight="600">${yTitle}</text>`;
        s += `</svg>`;
        let imgs = '';
        rows.forEach((r, i) => {
            const ring = scatterWrColor(r.wr);
            const tip = en
                ? `${r.name}  ·  late ${signed(r.sy)}  ·  snowball ${r.sx.toFixed(2)}  ·  WR ${pct(r.wr)}  ·  ${r.g} games`
                : zhUi(`${r.name}  ·  後期 ${signed(r.sy)}  ·  滾雪球 ${r.sx.toFixed(2)}  ·  勝率 ${pct(r.wr)}  ·  ${r.g}場`);
            imgs += `<img class="sc-dot" src="${escHtml(r.img)}" alt="" loading="lazy" title="${escHtml(tip)}" `
                + `style="left:${pos[i][0].toFixed(1)}px;top:${pos[i][1].toFixed(1)}px;width:${ICON}px;height:${ICON}px;--ring:${ring}">`;
        });
        host.style.position = 'relative';
        host.style.height = H + 'px';
        host.innerHTML = s + imgs;
        if (_scatterRO) { _scatterRO.disconnect(); }
        _scatterW = W;
        _scatterRO = new ResizeObserver(entries => {
            const w = Math.round(entries[0].contentRect.width);
            if (Math.abs(w - _scatterW) > 16) { _scatterW = w; renderScalingSnowballChart(); }
        });
        _scatterRO.observe(host);
    }
    // Slide the shared nav underline under the active tab.  Reads layout
    // (offsetLeft/Width) so call it after .active toggles and whenever tab
    // widths can change (language switch, resize, fonts load).
    function moveTabIndicator() {
        const bar = document.querySelector('.nav-tabs');
        if (!bar) return;
        const active = bar.querySelector('.nav-tab.active');
        // Home has no tab selected — collapse the underline so it does not
        // stick under a stale tab.
        if (!active) {
            bar.style.setProperty('--ind-w', '0px');
            return;
        }
        bar.style.setProperty('--ind-x', active.offsetLeft + 'px');
        bar.style.setProperty('--ind-w', active.offsetWidth + 'px');
        // On the mobile tab strip the bar scrolls; keep the active tab in view.
        if (bar.scrollWidth > bar.clientWidth + 4) {
            const left = Math.max(0, active.offsetLeft - (bar.clientWidth - active.offsetWidth) / 2);
            const smooth = document.documentElement.classList.contains('ui-ready')
                && !matchMedia('(prefers-reduced-motion: reduce)').matches;
            bar.scrollTo({ left, behavior: smooth ? 'smooth' : 'auto' });
        }
    }
    // The header is one row on desktop, two on mobile (brand row + tab strip).
    // Sticky search rail + champion detail tabs read --header-h /
    // --search-rail-h, so measure real heights instead of hardcoding.
    function syncHeaderHeight() {
        const header = document.querySelector('.site-header');
        if (header) {
            document.documentElement.style.setProperty('--header-h', header.offsetHeight + 'px');
        }
        const rail = document.querySelector('.view-home .search-rail');
        let railH = 0;
        if (rail) {
            const st = getComputedStyle(rail);
            if (st.display !== 'none' && st.visibility !== 'hidden') {
                railH = Math.ceil(rail.getBoundingClientRect().height);
            }
        }
        document.documentElement.style.setProperty('--search-rail-h', railH + 'px');
    }
    // Path routes (shareable, no hash).  English uses an /en prefix so a
    // shared link opens in English without relying on localStorage:
    //   /                  home (zh)
    //   /en                home (en)
    //   /augments          augment tier (zh)
    //   /en/augments       augment tier (en)
    //   /column/<id>       article (zh)
    //   /en/column/<id>    article (en)
    // Legacy '#view' / '#column/id' hashes (and old /settings) migrate once
    // on load so old links still open the right panel.
    function pathForRoute(view, sub) {
        const prefix = langMeta(currentLang).prefix;
        if (!view || view === 'home') return prefix || '/';
        if (view === 'column' && sub) {
            return prefix + '/column/' + encodeURIComponent(sub);
        }
        return prefix + '/' + view;
    }
    function normalizePathname(pathname) {
        let path = pathname || '/';
        if (path.length > 1 && path.endsWith('/')) path = path.slice(0, -1) || '/';
        return path;
    }
    function parseRoute() {
        let path = normalizePathname(location.pathname);

        // Legacy hash deep-links (bookmarks / old shares).
        const rawHash = (location.hash || '').replace(/^#/, '');
        if (rawHash) {
            const cut = rawHash.indexOf('/');
            const hView = cut === -1 ? rawHash : rawHash.slice(0, cut);
            let hSub = cut === -1 ? '' : rawHash.slice(cut + 1);
            try { hSub = decodeURIComponent(hSub); } catch {}
            if (VIEWS.includes(hView)) {
                return {
                    view: hView,
                    sub: hView === 'column' ? (hSub || '') : '',
                    urlLang: null,
                    legacyHash: true,
                };
            }
        }

        const segs = path.replace(/^\//, '').split('/').filter(Boolean);
        // Optional locale prefix: "en" | "zh-CN"; bare paths are traditional zh.
        let urlLang = null;
        if (segs[0] === 'en') {
            urlLang = 'en';
            segs.shift();
        } else if (segs[0] === 'zh-CN') {
            urlLang = 'zh-CN';
            segs.shift();
        }
        if (!segs.length) return { view: 'home', sub: '', urlLang, legacyHash: false };
        // Treat /home and /en/home as /
        if (segs[0] === 'home' && segs.length === 1) {
            return { view: 'home', sub: '', urlLang, legacyHash: false };
        }
        const view = segs[0];
        if (!VIEWS.includes(view)) return { view: 'home', sub: '', urlLang, legacyHash: false };
        let sub = '';
        if (view === 'column' && segs.length > 1) {
            try { sub = decodeURIComponent(segs.slice(1).join('/')); } catch { sub = segs.slice(1).join('/'); }
        }
        return { view, sub, urlLang, legacyHash: false };
    }
    function syncUrlToRoute(view, sub, historyMode) {
        // historyMode: 'push' | 'replace' | 'none'
        if (historyMode === 'none') return;
        const wantPath = pathForRoute(view, sub);
        const curNorm = normalizePathname(location.pathname);
        const needPath = curNorm !== wantPath;
        const needClearHash = Boolean(location.hash);
        if (!needPath && !needClearHash) return;
        const url = wantPath + (location.search || '');
        try {
            if (historyMode === 'push' && needPath) history.pushState(null, '', url);
            else history.replaceState(null, '', url);
        } catch {
            // file:// or locked history — ignore; in-page state still updates.
        }
    }
    function routeFromLocation(instant, historyMode) {
        const { view, sub, legacyHash, urlLang } = parseRoute();
        // URL is source of truth for language on navigation / popstate.
        // (Boot may soft-apply a saved en preference on bare `/` only.)
        // Explicit locale prefix wins; bare path = traditional zh.
        const wantLang = urlLang ? normalizeLang(urlLang) : 'zh';
        if (wantLang !== currentLang) {
            applyLanguage(wantLang, 'none');
        }
        if (view === 'column') columnArticle = sub || null;
        else columnArticle = null;
        // Migrating an old #hash always replaceStates onto the clean path.
        const mode = legacyHash ? 'replace' : (historyMode || 'replace');
        setActiveView(VIEWS.includes(view) ? view : 'home', instant, mode);
    }
    function setActiveView(name, instant, historyMode) {
        // Old /settings bookmarks land on home (settings chrome was removed).
        if (name === 'settings' || !VIEWS.includes(name)) name = 'home';
        if (historyMode == null) historyMode = 'replace';
        const apply = () => {
            const tabs = [...document.querySelectorAll('.nav-tab[data-nav-tab]')];
            let activeTab = null;
            tabs.forEach(t => {
                const on = t.getAttribute('data-nav-tab') === name;
                t.classList.toggle('active', on);
                t.setAttribute('aria-selected', on ? 'true' : 'false');
                t.tabIndex = -1;
                if (on) activeTab = t;
            });
            // Roving tabindex: the selected tab is the only focusable tab.
            if (activeTab) activeTab.tabIndex = 0;
            else if (tabs[0]) tabs[0].tabIndex = 0;
            document.querySelectorAll('.view[data-view]').forEach(v => {
                v.classList.toggle('is-active', v.getAttribute('data-view') === name);
            });
            if (name === 'column') {
                columnArticle ? renderArticle(columnArticle) : renderColumnList();
            } else {
                columnArticle = null;
                document.title = BASE_TITLE;  // leaving an open article restores the base title
            }
            if (name === 'augments') {
                renderAugmentTier();
            }
            if (name === 'draft') {
                renderDraft();
            }
            if (name === 'game') {
                renderMetaPick();
            }
            if (name === 'changes') {
                renderUpdatesPanel();
            }
            // Path is written AFTER render so an invalid article id that fell
            // back to the list normalises to /column (not a 404 path).
            const sub = (name === 'column' && columnArticle) ? columnArticle : '';
            syncUrlToRoute(name, sub, historyMode);
            window.scrollTo(0, 0);
            moveTabIndicator();
        };
        // Cross-fade the panel via the View Transitions API; root is pinned so the
        // header and the scrollTo above don't animate.  Skip on first paint
        // (instant) and for reduced-motion / unsupported browsers.
        const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (instant || reduce || !document.startViewTransition) { apply(); return; }
        // Flag the VT so .vt-running suppresses the indicator CSS tween while
        // the snapshots are captured; clear it once the VT settles.
        const root = document.documentElement;
        root.classList.add('vt-running');
        document.startViewTransition(apply).finished.finally(() => {
            root.classList.remove('vt-running');
        });
    }
    function syncThemeToggleLabels() {
        const btn = document.getElementById('theme-toggle');
        if (!btn) return;
        const copy = tr();
        const isLight = document.documentElement.getAttribute('data-theme') === 'light';
        btn.title = isLight ? copy.themeToDarkTitle : copy.themeToLightTitle;
        btn.setAttribute('aria-label', isLight ? copy.themeToDarkAria : copy.themeToLightAria);
    }
    function applyTheme(theme) {
        const t = theme === 'light' ? 'light' : 'dark';
        const root = document.documentElement;
        // Kill transitions for one tick so var()-driven backgrounds snap to the
        // new palette instead of sticking mid-transition (see .no-theme-transition).
        root.classList.add('no-theme-transition');
        root.setAttribute('data-theme', t);
        setTimeout(() => root.classList.remove('no-theme-transition'), 60);
        syncThemeToggleLabels();
        try { localStorage.setItem(THEME_KEY, t); } catch {}
    }

    function syncLangMenu() {
        const meta = langMeta(currentLang);
        const toggle = document.getElementById('lang-toggle');
        const toggleLabel = document.getElementById('lang-toggle-label');
        if (toggle) {
            toggle.title = meta.label;
            toggle.setAttribute('aria-label', 'Language / 語言: ' + meta.label);
        }
        if (toggleLabel) toggleLabel.textContent = meta.label;
        document.querySelectorAll('.lang-menu-list [data-lang]').forEach(btn => {
            const on = btn.getAttribute('data-lang') === currentLang;
            btn.classList.toggle('is-active', on);
            if (on) btn.setAttribute('aria-current', 'true');
            else btn.removeAttribute('aria-current');
        });
        const menu = document.getElementById('lang-menu');
        if (menu) menu.open = false;
    }
    function applyLanguage(nextLang, historyMode) {
        // historyMode: 'replace' (default, language toggle) | 'none' (URL already correct)
        if (historyMode == null) historyMode = 'replace';
        currentLang = normalizeLang(nextLang);
        _trZhCN = null;  // rebuild simplified COPY proxy after lang change / names load
        const copy = tr();
        document.documentElement.lang = langMeta(currentLang).htmlLang;
        try { localStorage.setItem(LANG_KEY, currentLang); } catch {}

        // Brand wordmark is structured HTML (aram + meta) shared in both langs;
        // only rewrite when headers diverge from the Latin mark.
        const titleEl = document.getElementById('site-title');
        if (titleEl) {
            const nextTitle = currentLang === 'en' ? HEADER_TITLE_EN : HEADER_TITLE_ZH;
            if (nextTitle !== 'arammeta') {
                titleEl.textContent = nextTitle;
            } else if (!titleEl.querySelector('.brand-meta')) {
                titleEl.innerHTML = "<span class='brand-aram'>aram</span><span class='brand-meta'>meta</span>";
                titleEl.setAttribute('aria-label', 'arammeta');
            }
        }
        updateSearchPlaceholder();
        const shownUnit = document.getElementById('shown-unit');
        if (shownUnit) shownUnit.textContent = copy.shownUnit;
        document.querySelectorAll('.tier-count-unit').forEach(el => {
            el.textContent = copy.tierUnit;
        });
        document.querySelectorAll('.chip, .item-role-chip').forEach(chip => {
            if (currentLang === 'en') {
                chip.textContent = chip.getAttribute('data-label-en') || chip.textContent || '';
            } else {
                const zh = chip.getAttribute('data-label-zh') || chip.textContent || '';
                chip.textContent = zhUi(zh);
            }
        });
        const emptyTitle = document.getElementById('empty-title');
        if (emptyTitle) emptyTitle.textContent = copy.emptyTitle;
        const emptyCopy = document.getElementById('empty-copy');
        if (emptyCopy) emptyCopy.textContent = copy.emptyCopy;
        const freshness = document.getElementById('freshness-copy');
        if (freshness) freshness.textContent = copy.freshness();
        const sideTitle = document.getElementById('side-title');
        if (sideTitle) sideTitle.textContent = copy.sideTitle;
        const sideSub = document.getElementById('side-sub');
        if (sideSub) sideSub.innerHTML = copy.sideSub;
        const sideClose = document.getElementById('side-close');
        if (sideClose) sideClose.setAttribute('aria-label', copy.closeRecs);
        syncLangMenu();
        syncThemeToggleLabels();

        // Static chrome strings (nav tabs, augment placeholder) carry their
        // own zh/en text via data-i18n-* attributes; zh-CN falls back to t2s(zh).
        document.querySelectorAll('[data-i18n-zh]').forEach(el => {
            let val;
            if (currentLang === 'en') val = el.getAttribute('data-i18n-en');
            else if (currentLang === 'zh-CN') {
                val = el.getAttribute('data-i18n-zh-cn')
                    || t2s(el.getAttribute('data-i18n-zh') || '');
            } else val = el.getAttribute('data-i18n-zh');
            if (val != null) el.textContent = val;
        });
        if (document.getElementById('view-column')
                && document.getElementById('view-column').classList.contains('is-active')) {
            columnArticle ? renderArticle(columnArticle) : renderColumnList();
        }
        const _augView = document.getElementById('view-augments');
        if (_augView && _augView.classList.contains('is-active')) {
            renderAugmentTier();
        }
        // Draft metrics / team-eval HTML is built via tr(); must re-render or
        // language toggles leave the previous locale's strings on screen while
        // data-i18n chrome has already flipped.
        if (document.querySelector('.view-draft.is-active')) {
            renderDraft();
        }
        if (document.querySelector('.view-game.is-active')) {
            renderMetaPick();
        }

        moveTabIndicator();
        updateChampCardCopy();
        refreshSecondaryRoleBadges();
        renderUpdatesPanel();
        setRecommendMode(recommendMode);
        renderSidePanel();
        if (detailSelected) {
            const champ = document.querySelector(`.champ[data-cid="${detailSelected}"].detail-selected`);
            if (champ) openDetailForChamp(champ, true);
        }
        // Keep the path prefix in sync with language so shared links stay bilingual.
        if (historyMode !== 'none') {
            const active = document.querySelector('.view.is-active');
            let view = (active && active.getAttribute('data-view')) || 'home';
            if (!VIEWS.includes(view)) view = 'home';
            const sub = (view === 'column' && columnArticle) ? columnArticle : '';
            syncUrlToRoute(view, sub, historyMode);
        }
    }

    function setRecommendMode(next) {
        // Home teammate mode retired — Draft tab owns roster picks.
        recommendMode = false;
        recModalOpen = false;
        const btn = document.getElementById('recommend-mode');
        if (!btn) return;
        btn.classList.remove('active');
        btn.setAttribute('aria-pressed', 'false');
        btn.hidden = true;
    }

    function syncDetailModalState() {
        const open = Boolean(detailSelected);
        document.body.classList.toggle('detail-modal-open', open && isMobileViewport());
        // Desktop sticky chrome marker (search overlays right; tabs pin at same top).
        document.body.classList.toggle('detail-open', open);
        syncHeaderHeight();
    }

    function closeDetail() {
        document.querySelectorAll('.detail-host').forEach(h => h.innerHTML = '');
        document.querySelectorAll('.champ.detail-selected').forEach(el => el.classList.remove('detail-selected'));
        detailSelected = null;
        syncDetailModalState();
    }

    // Monotonic token: every open bumps it so a deferred heavy fill can detect
    // that a newer open (or a close) superseded it and abort, avoiding a stale
    // panel flashing in after the user already moved on.
    let detailOpenToken = 0;

    function openDetailForChamp(champ, force = false) {
        const cid = champ.getAttribute('data-cid');
        const block = champ.closest('.tier-block');
        const host  = block.querySelector('.detail-host');

        // Clear any previously selected highlight + detail elsewhere.
        document.querySelectorAll('.champ.detail-selected').forEach(el => {
            if (el !== champ) el.classList.remove('detail-selected');
        });
        document.querySelectorAll('.detail-host').forEach(el => {
            if (el !== host) el.innerHTML = '';
        });

        if (!force && detailSelected === cid && host.firstChild) {
            closeDetail();
            return;
        }

        // Position the detail host right after the last champ in the clicked
        // row, so the panel always pops up directly under the champion you
        // tapped — never hidden far below by other champs.
        const anchor = lastChampInRow(champ);
        if (anchor.nextSibling !== host) {
            anchor.after(host);
        }

        // ---- Two-phase open (INP) --------------------------------------------
        // Detail open is the most frequent interaction and renderDetail builds a
        // large HTML string.  Doing it inside the click handler is the second INP
        // contributor.  Phase 1 (synchronous, cheap): mark selection + paint a
        // skeleton sized to the panel so there is no CLS jump.  Phase 2 (after a
        // yield): the heavy renderDetail fill + highlight/filter passes.
        const token = ++detailOpenToken;
        const dialogAttrs = isMobileViewport()
            ? ` role="dialog" aria-modal="true" aria-labelledby="detail-title-${cid}"`
            : '';
        host.innerHTML = `<div class="detail detail-loading"${dialogAttrs}><div class="detail-skeleton" aria-hidden="true"></div></div>`;
        champ.classList.add('detail-selected');
        detailSelected = cid;
        syncDetailModalState();
        if (!force) {
            trackEvent('champion_detail_open', {
                champion_id: cid,
                champion_name: champ.getAttribute('data-name-en') || '',
                tier: champ.getAttribute('data-tier') || '',
            });
        }

        // Phase 2: fetch this champion's detail shard, then fill the panel after
        // handing the main thread back. Inline/legacy payloads resolve instantly.
        yieldToMain().then(() => ensureChampDetail(cid)).then(() => {
            // Abort if a newer open or a close superseded this one while we waited.
            if (token !== detailOpenToken || detailSelected !== cid) return;
            if (!host.isConnected) return;
            try {
                host.innerHTML = `<div class="detail"${dialogAttrs}>${renderDetail(cid)}</div>`;
                // Skip the document-wide highlight / category sweeps when nothing
                // is active — they walk every card for no effect otherwise.
                if (filterState.q.trim()) applySearchHighlights(host);
                if (augCatFilter.size) applyAugCatFilter(host);
                // Always re-sync chip pressed state (and hide cards if a role /
                // 常見 filter is sticky from a previous champion).
                applySingleItemFilter(host);
            } catch (err) {
                console.error('detail render failed for champ', cid, err);
                return;
            }
            if (isMobileViewport()) {
                host.querySelector('.detail-close')?.focus({ preventScroll: true });
            }
        }).catch(err => {
            if (token !== detailOpenToken || detailSelected !== cid) return;
            console.error('detail load failed for champ', cid, err);
            const message = currentLang === 'en'
                ? 'Champion details could not be loaded.'
                : '英雄詳細資料載入失敗。';
            const retry = currentLang === 'en' ? 'Retry' : '重試';
            host.innerHTML = `
                <div class="detail detail-load-error"${dialogAttrs}>
                    <div class="empty" role="alert">${escHtml(message)}</div>
                    <button type="button" class="detail-retry" data-detail-retry>${escHtml(retry)}</button>
                </div>`;
        });
    }

    function openDetailByCid(cid) {
        // The detail host lives inside the home tier grid; callers can fire from
        // the Settings changelog or a recommend row, so always surface the home
        // view first or the panel would open in a hidden view (invisible).
        setActiveView('home', false, 'push');
        const champ = document.querySelector(`.champ[data-cid="${cid}"]:not(.hidden)`);
        if (!champ) return;
        openDetailForChamp(champ);
        champ.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }

    function toggleTeamPick(cid) {
        pickNotice = '';
        const idx = teamPicks.indexOf(cid);
        if (idx !== -1) {
            teamPicks.splice(idx, 1);
        } else if (teamPicks.length >= MAX_TEAM_PICKS) {
            pickNotice = tr().maxOnly(MAX_TEAM_PICKS);
        } else {
            teamPicks.push(cid);
        }
        syncPickDecorations();
        renderSidePanel();
    }

    document.addEventListener('submit', (ev) => {
        const form = ev.target && ev.target.closest ? ev.target.closest('#game-settle-form') : null;
        if (!form) return;
        ev.preventDefault();
        const nickEl = document.getElementById('game-nick');
        if (nickEl) metaPickSession.nickname = nickEl.value;
        metaPickSubmitRun();
        trackEvent('game_submit_run', {});
    });

    document.addEventListener('click', (ev) => {
        const detailRetry = ev.target.closest('[data-detail-retry]');
        if (detailRetry) {
            const champ = document.querySelector(`.champ[data-cid="${detailSelected}"].detail-selected`);
            if (champ) openDetailForChamp(champ, true);
            return;
        }
        const ghStar = ev.target.closest('.gh-star');
        if (ghStar) {
            trackEvent('github_star_click', { location: 'footer' });
            return;
        }
        const navTab = ev.target.closest('[data-nav-tab]');
        if (navTab) {
            const view = navTab.getAttribute('data-nav-tab');
            setActiveView(view, false, 'push');
            trackEvent('view_change', { view });
            return;
        }
        const themeToggle = ev.target.closest('[data-theme-toggle]');
        if (themeToggle) {
            const cur = document.documentElement.getAttribute('data-theme') === 'light'
                ? 'light' : 'dark';
            const theme = cur === 'light' ? 'dark' : 'light';
            applyTheme(theme);
            trackEvent('theme_toggle', { theme });
            return;
        }
        const articleCard = ev.target.closest('[data-article]');
        if (articleCard) {
            const id = articleCard.getAttribute('data-article');
            // Push a clean path so Back returns to the list and the URL is
            // shareable as https://arammeta.com/column/<id> (no hash).
            columnArticle = id;
            setActiveView('column', false, 'push');
            trackEvent('article_open', { id });
            return;
        }
        if (ev.target.closest('[data-article-back]')) {
            columnArticle = null;
            setActiveView('column', false, 'push');
            return;
        }
        const shareBtn = ev.target.closest('[data-article-share]');
        if (shareBtn) {
            copyArticleLink(shareBtn);
            trackEvent('article_share', { id: columnArticle });
            return;
        }
        const langPick = ev.target.closest('.lang-menu-list [data-lang]');
        if (langPick) {
            const nextLang = normalizeLang(langPick.getAttribute('data-lang'));
            applyLanguage(nextLang);
            trackEvent('language_toggle', { language: nextLang });
            return;
        }
        // Summary click opens/closes <details>; no language flip on the chrome itself.
        if (ev.target.closest('#lang-menu summary')) {
            return;
        }
        const fabBtn = ev.target.closest('#rec-fab');
        if (fabBtn) {
            recModalOpen = true;
            renderSidePanel();
            trackEvent('recommendations_open', { source: 'fab', picks: teamPicks.length });
            return;
        }
        const sideClose = ev.target.closest('#side-close');
        if (sideClose) {
            recModalOpen = false;
            renderSidePanel();
            trackEvent('recommendations_close', { source: 'panel', picks: teamPicks.length });
            return;
        }
        const detailClose = ev.target.closest('.detail-close');
        if (detailClose) {
            closeDetail();
            return;
        }
        const augSortBtn = ev.target.closest('.rlabel-sort[data-sort]');
        if (augSortBtn) {
            const row = augSortBtn.closest('.rarity-row');
            const key = augSortBtn.getAttribute('data-sort') || 'wr';
            sortRarityAugList(row, key);
            trackEvent('aug_rarity_sort', { sort: key, rarity: row && row.getAttribute('data-rarity') });
            return;
        }
        if (isMobileViewport() && ev.target.classList && ev.target.classList.contains('detail-host')) {
            closeDetail();
            return;
        }
        const changeTab = ev.target.closest('[data-change-tab]');
        if (changeTab) {
            activeUpdateTab = changeTab.getAttribute('data-change-tab') || 'heroes';
            renderUpdatesPanel();
            trackEvent('patch_change_tab', { tab: activeUpdateTab });
            return;
        }
        const changeCid = ev.target.closest('[data-change-cid]');
        if (changeCid) {
            openDetailByCid(changeCid.getAttribute('data-change-cid'));
            trackEvent('patch_change_detail_open', { champion_id: changeCid.getAttribute('data-change-cid') });
            return;
        }
        const updatesBtn = ev.target.closest('#updates-toggle');
        if (updatesBtn) {
            updatesOpen = !updatesOpen;
            renderUpdatesPanel();
            trackEvent('updates_toggle', { open: updatesOpen });
            return;
        }
        const updatesClose = ev.target.closest('#updates-close');
        if (updatesClose) {
            updatesOpen = false;
            renderUpdatesPanel();
            trackEvent('updates_close', {});
            return;
        }
        const draftViewBtn = ev.target.closest('[data-draft-view]');
        if (draftViewBtn) {
            draftView = draftViewBtn.getAttribute('data-draft-view') === 'analysis' ? 'analysis' : 'draft';
            renderDraftView();
            trackEvent('draft_view', { view: draftView });
            return;
        }
        const draftTarget = ev.target.closest('[data-draft-target]');
        if (draftTarget) {
            setDraftSide(draftTarget.getAttribute('data-draft-target') || 'ally');
            trackEvent('draft_side', { side: draftSide });
            return;
        }
        const draftRemove = ev.target.closest('[data-draft-remove]');
        if (draftRemove) {
            const side = draftRemove.getAttribute('data-draft-remove') || 'ally';
            const cid = draftRemove.getAttribute('data-cid');
            const list = draftPickList(side);
            const idx = list.indexOf(cid);
            if (idx !== -1) list.splice(idx, 1);
            pickNotice = '';
            renderDraft();
            trackEvent('draft_pick_remove', { side, champion_id: cid });
            return;
        }
        const draftPick = ev.target.closest('[data-draft-pick]');
        if (draftPick) {
            const cid = draftPick.getAttribute('data-draft-pick');
            toggleDraftPick(cid);
            trackEvent('draft_pick_toggle', { side: draftSide, champion_id: cid, ally: teamPicks.length, enemy: enemyPicks.length });
            return;
        }
        const draftRoleBtn = ev.target.closest('[data-draft-role]');
        if (draftRoleBtn) {
            draftRole = draftRoleBtn.getAttribute('data-draft-role') || '';
            const host = document.getElementById('draft-role-chips');
            if (host) host.dataset.ready = '1';
            host?.querySelectorAll('.chip').forEach(c => {
                c.classList.toggle('active', (c.getAttribute('data-draft-role') || '') === draftRole);
            });
            renderDraftChampList();
            trackEvent('draft_role_filter', { role: draftRole || 'all' });
            return;
        }
        if (ev.target.closest('#draft-clear')) {
            clearDraft();
            trackEvent('draft_clear', {});
            return;
        }
        const gameTab = ev.target.closest('[data-game-tab]');
        if (gameTab) {
            const host = gameTab.closest('.game-panels');
            if (host) {
                const name = gameTab.getAttribute('data-game-tab') || 'pool';
                host.querySelectorAll('.game-tab').forEach(btn => {
                    const on = btn.getAttribute('data-game-tab') === name;
                    btn.classList.toggle('is-active', on);
                    btn.setAttribute('aria-selected', on ? 'true' : 'false');
                });
                host.querySelectorAll('[data-game-tab-panel]').forEach(panel => {
                    const on = panel.getAttribute('data-game-tab-panel') === name;
                    panel.classList.toggle('is-active', on);
                    panel.hidden = !on;
                });
                trackEvent('game_result_tab', { tab: name });
            }
            return;
        }
        const gamePick = ev.target.closest('[data-game-pick]');
        if (gamePick) {
            const cid = gamePick.getAttribute('data-game-pick');
            metaPickToggle(cid);
            renderMetaPick();
            trackEvent('game_pick_toggle', { champion_id: cid, picks: metaPick.pickedIds.length });
            return;
        }
        if (ev.target.closest('#game-lock')) {
            metaPickLock();
            trackEvent('game_lock', {
                picks: metaPick.pickedIds.length,
                phase: metaPick.phase,
                hint_used: metaPick.hintUsed,
            });
            return;
        }
        if (ev.target.closest('#game-hint')) {
            metaPickUseHint();
            trackEvent('game_hint', { pinned: metaPick.pinnedIds[0] || '' });
            return;
        }
        if (ev.target.closest('#game-reveal')) {
            metaPickShowAnswer();
            trackEvent('game_reveal_early', {});
            return;
        }
        if (ev.target.closest('#game-play-again') || ev.target.closest('#game-next-round')) {
            metaPickNextRound();
            trackEvent('game_next_round', { rounds: metaPickSession.rounds.length });
            return;
        }
        if (ev.target.closest('#game-show-settle')) {
            metaPickSession.settled = true;
            renderMetaPick();
            trackEvent('game_show_settle', {});
            return;
        }
        if (ev.target.closest('#game-restart')) {
            metaPickRestartRun();
            trackEvent('game_restart_run', {});
            return;
        }
        if (ev.target.closest('#game-submit')) {
            // Form submit handler also covers this; keep click path for safety.
            ev.preventDefault();
            const nickEl = document.getElementById('game-nick');
            if (nickEl) metaPickSession.nickname = nickEl.value;
            metaPickSubmitRun();
            trackEvent('game_submit_run', {});
            return;
        }
        const removeBtn = ev.target.closest('[data-remove-cid]');
        if (removeBtn) {
            const removedCid = removeBtn.getAttribute('data-remove-cid');
            teamPicks = teamPicks.filter(cid => cid !== removeBtn.getAttribute('data-remove-cid'));
            pickNotice = '';
            syncPickDecorations();
            renderSidePanel();
            if (document.querySelector('.view-draft.is-active')) renderDraft();
            trackEvent('team_pick_remove', { champion_id: removedCid, picks: teamPicks.length });
            return;
        }
        const recRow = ev.target.closest('.rec-row:not(.draft-rec-row)');
        if (recRow) {
            recModalOpen = false;
            renderSidePanel();
            const recCid = recRow.getAttribute('data-cid');
            trackEvent('recommendation_click', { champion_id: recCid, picks: teamPicks.length });
            openDetailByCid(recCid);
            return;
        }
        const champ = ev.target.closest('.champ');
        if (!champ) return;
        openDetailForChamp(champ);
    });

    // Draft search input (debounced like home search).
    document.getElementById('draft-search')?.addEventListener('input', (ev) => {
        draftQuery = ev.target.value || '';
        clearTimeout(window.__draftSearchT);
        window.__draftSearchT = setTimeout(() => renderDraftChampList(), 80);
    });

    // When viewport width changes, the row containing the selected champ
    // shifts — re-anchor the detail host so it stays directly under that
    // champ on the new layout.
    let resizeT = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeT);
        resizeT = setTimeout(() => {
            updateSearchPlaceholder();
            renderSidePanel();
            syncHeaderHeight();  // header is 1 row on desktop, 2 on mobile
            moveTabIndicator();
            moveSegThumb();
            if (!detailSelected) return;
            const champ = document.querySelector(`.champ[data-cid="${detailSelected}"].detail-selected`);
            if (!champ) return;
            const host = champ.closest('.tier-block').querySelector('.detail-host');
            const anchor = lastChampInRow(champ);
            if (anchor.nextSibling !== host) anchor.after(host);
            syncDetailModalState();
        }, 120);
    });

    function addSearchTerm(terms, value) {
        if (value === null || value === undefined) return;
        if (Array.isArray(value)) {
            value.forEach(item => addSearchTerm(terms, item));
            return;
        }
        const text = String(value).trim();
        if (text) terms.push(text);
    }

    function addNamedSearchRow(terms, row) {
        if (!row) return;
        addSearchTerm(terms, [
            row.name, row.name_zh, row.name_en,
            row.set, row.set_zh, row.set_en, row.slug,
        ]);
        (row.items || []).forEach(item => {
            addSearchTerm(terms, [item.name, item.name_zh, item.name_en, item.id]);
        });
    }

    function addAugmentSearchRow(terms, row) {
        if (!row) return;
        const aug = (DATA.augs || {})[String(row.id || row.augment_id || '')];
        if (!aug) return;
        addSearchTerm(terms, [
            aug.name, aug.name_zh, aug.name_en,
            aug.set, aug.set_zh, aug.set_en, aug.setSlug,
        ]);
        (aug.sets || []).forEach(setInfo => {
            addSearchTerm(terms, [
                setInfo.name, setInfo.name_zh, setInfo.name_en, setInfo.slug,
            ]);
        });
    }

    // Item rehydration, per champion.  The build stripped item identities
    // (name + icon) into DATA.itemLut to shrink the payload (see
    // _dedupe_item_objects); every embedded item was tagged "ic":1 with its id +
    // per-row stats kept.  Walking the WHOLE DATA.champs tree eagerly at startup
    // was a multi-hundred-ms main-thread block right when the shell looks
    // interactive (top INP contributor).  Instead we rehydrate one champion at a
    // time, idempotently: renderDetail / any item-name/icon reader calls
    // rehydrateChamp(cid) first (cheap guard once done), and a background chunked
    // pass warms the rest.  Backward-compatible: a payload with no itemLut (full
    // item objects) is a no-op.
    const _rehydratedChamps = new Set();
    function rehydrateChamp(cid) {
        const lut = DATA.itemLut;
        if (!lut) return;                       // full-object payload: nothing to do
        const key = String(cid);
        if (_rehydratedChamps.has(key)) return; // idempotent guard
        const info = (DATA.champs || {})[key];
        _rehydratedChamps.add(key);             // mark before walk: missing champ is still "done"
        if (!info) return;
        const ver = DATA.ddv || '';
        (function visit(node) {
            if (Array.isArray(node)) {
                for (let i = 0; i < node.length; i++) visit(node[i]);
            } else if (node && typeof node === 'object') {
                if (node.ic === 1 && 'id' in node) {
                    const m = lut[String(node.id)] || {};
                    const zh = m.z || '';
                    delete node.ic;
                    node.name = zh;
                    node.name_zh = zh;
                    node.name_en = m.e || '';
                    if (m.c) node.name_cn = m.c;
                    node.icon = ver
                        ? ('https://ddragon.leagueoflegends.com/cdn/' + ver + '/img/item/' + node.id + '.png')
                        : '';
                    if (m.dz) node.desc_zh = m.dz;
                    if (m.de) node.desc_en = m.de;
                    if (m.dc) node.desc_cn = m.dc;
                    if (m.p) node.price = m.p;
                }
                for (const k in node) visit(node[k]);
            }
        })(info);
    }

    // Build one champion card's search blob.  Reads item names, so rehydrateChamp
    // for that cid MUST run first (the chunked pass below enriches each card right
    // after rehydrating it, preserving that ordering guarantee).
    function enrichChampCard(champ) {
        const cid = champ.getAttribute('data-cid');
        const info = (DATA.champs || {})[String(cid)];
        if (!info) return;
        const terms = [champ.getAttribute('data-search') || ''];
        addSearchTerm(terms, [info.name, info.name_zh, info.name_en, info.alias, info.tags || []]);
        ['top', 'bot'].forEach(side => {
            Object.values(info[side] || {}).forEach(rows => (rows || []).forEach(row => addAugmentSearchRow(terms, row)));
            ['sets', 'items', 'singleItems', 'boots', 'itemClusters', 'augTypes'].forEach(key => {
                ((info[key] || {})[side] || []).forEach(row => addNamedSearchRow(terms, row));
            });
        });
        const seen = new Set();
        const blob = terms
            .flatMap(term => String(term).toLowerCase().split(/\\s+/))
            .filter(term => {
                if (!term || seen.has(term)) return false;
                seen.add(term);
                return true;
            })
            .join(' ');
        champ.setAttribute('data-search', blob);
    }

    // Background pass: rehydrate + enrich every champion card in small chunks,
    // yielding between chunks so we never hold the main thread long enough to
    // stall a tap.  Grid filtering by champion name works BEFORE this settles
    // (the server-rendered data-search already carries champion names); augment /
    // item term search just gets progressively better as cards are enriched.
    // Resilient: a chunk that throws is logged and skipped, never leaving init
    // half-done silently.
    async function warmChampIndexesInBackground() {
        const cards = Array.from(document.querySelectorAll('.champ[data-cid]'));
        const CHUNK = 16;
        for (let i = 0; i < cards.length; i += CHUNK) {
            const slice = cards.slice(i, i + CHUNK);
            for (const champ of slice) {
                try {
                    rehydrateChamp(champ.getAttribute('data-cid'));
                    enrichChampCard(champ);
                } catch (err) {
                    console.error('index warm failed for champ', champ.getAttribute('data-cid'), err);
                }
            }
            if (i + CHUNK < cards.length) await yieldToMain();
        }
    }

    // Language: URL locale prefix + stub-stashed aram-spa-lang win (see pendingBootLang
    // at top of file).  Bare paths are zh, except a soft preference on home `/`
    // that rewrites to the last chosen non-zh locale.
    {
        const boot = parseRoute();
        if (boot.urlLang) {
            currentLang = normalizeLang(boot.urlLang);
        } else if (pendingBootLang) {
            currentLang = normalizeLang(pendingBootLang);
        } else {
            currentLang = 'zh';
            try {
                const p = normalizePathname(location.pathname);
                const saved = localStorage.getItem(LANG_KEY);
                if ((p === '/' || p === '') && (saved === 'en' || saved === 'zh-CN')) {
                    currentLang = normalizeLang(saved);
                    try {
                        history.replaceState(null, '', (langMeta(currentLang).prefix || '/') + (location.search || ''));
                    } catch {}
                }
            } catch {}
        }
    }

    // First paint depends on the grid already being in the DOM (server-rendered).
    // Chrome was already flipped for /en at the top of this IIFE; applyLanguage
    // here finishes data-bound copy (cards, panels).  historyMode 'none': the
    // following routeFromLocation will write the locale prefix if needed.
    setRecommendMode(false);
    syncPickDecorations();
    renderSidePanel();
    if (currentLang !== 'zh') {
        // Non-default locale must land in the same turn as the first interactive
        // frame so shared /en or /zh-CN links never need a manual language toggle.
        applyLanguage(currentLang, 'none');
    } else {
        refreshSecondaryRoleBadges();
        renderUpdatesPanel();
        syncLangMenu();
    }
    // Warm the search indexes in the background; do not await (keeps init moving).
    whenIdle(() => { warmChampIndexesInBackground().catch(err => console.error('index warm pass failed', err)); });

    // ---- Chrome init: theme, tab routing ----
    try {
        const savedTheme = localStorage.getItem(THEME_KEY);
        applyTheme(savedTheme === 'light' ? 'light' : 'dark');
    } catch { applyTheme('dark'); }
    routeFromLocation(true, 'replace');  // instant: no View Transition on first paint
    // Position the sliding indicator/thumb now (base CSS has transition:none so
    // they snap), commit that layout with a reflow, THEN enable the transitions
    // so only later tab/theme changes animate -- never a grow-from-0 on load.
    // Synchronous (not requestAnimationFrame) so it still runs when the tab is
    // backgrounded / not painting (rAF callbacks are parked there).
    syncHeaderHeight();
    moveTabIndicator();
    void document.documentElement.offsetWidth;  // reflow: commit initial geometry
    document.documentElement.classList.add('ui-ready');
    // Web fonts can resize tab labels after load -- re-anchor once settled.
    window.addEventListener('load', () => { syncHeaderHeight(); moveTabIndicator(); });
    if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(() => { syncHeaderHeight(); moveTabIndicator(); });
    }
    // Browser Back/Forward for path routes (and any leftover hash→path migration).
    window.addEventListener('popstate', () => routeFromLocation(false, 'none'));
    // Old shared links still use #column/id — migrate if hash changes mid-session.
    window.addEventListener('hashchange', () => {
        if (location.hash) routeFromLocation(false, 'replace');
    });
    // WAI-ARIA tablist keyboard nav: arrows / Home / End move focus + activate.
    document.querySelector('.nav-tabs')?.addEventListener('keydown', (ev) => {
        const tabs = [...document.querySelectorAll('.nav-tab[data-nav-tab]')];
        const idx = tabs.indexOf(document.activeElement);
        if (idx === -1) return;
        let next = null;
        if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') next = tabs[(idx + 1) % tabs.length];
        else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') next = tabs[(idx - 1 + tabs.length) % tabs.length];
        else if (ev.key === 'Home') next = tabs[0];
        else if (ev.key === 'End') next = tabs[tabs.length - 1];
        if (!next) return;
        ev.preventDefault();
        next.focus();
        setActiveView(next.getAttribute('data-nav-tab'), false, 'push');
    });
    // Scroll-aware header: lift it once content scrolls underneath.
    const siteHeaderEl = document.querySelector('.site-header');
    if (siteHeaderEl) {
        const onHeaderScroll = () => siteHeaderEl.classList.toggle('scrolled', window.scrollY > 4);
        window.addEventListener('scroll', onHeaderScroll, { passive: true });
        onHeaderScroll();
        // Keep --header-h pinned to the header's REAL height.  Debounced
        // window-resize alone can sample a mid-reflow transient (e.g. a row
        // briefly wrapping while crossing the 700px breakpoint) and then never
        // correct itself; observing the element re-fires once layout settles.
        if (window.ResizeObserver) {
            new ResizeObserver(() => syncHeaderHeight()).observe(siteHeaderEl);
        }
    }
    const searchRailEl = document.querySelector('.view-home .search-rail');
    if (searchRailEl && window.ResizeObserver) {
        new ResizeObserver(() => syncHeaderHeight()).observe(searchRailEl);
    }

    /* -----  Filter / search  --------------------------------------- */

    function applyFilters() {
        const role = filterState.role;
        const q = filterState.q.trim();
        let shown = 0;
        document.querySelectorAll('.tier-block').forEach(block => {
            let tierShown = 0;
            const champs = block.querySelectorAll(':scope > .tier-grid > .champ');
            champs.forEach(c => {
                const tags = (c.getAttribute('data-tags') || '').split(' ');
                const blob = c.getAttribute('data-search') || '';
                const matchRole = !role || tags.includes(role);
                const matchQ = !q || searchMatchesText(blob, q);
                // Keep the open detail's champ pinned even when it fails the
                // active role/search filter, so searching never closes the
                // panel you're reading.  (Ctrl+F focuses this search box; a
                // non-matching query used to hide the selected champ, which
                // closed its detail and looked like the page reset itself.)
                const isSelected = detailSelected
                    && c.getAttribute('data-cid') === detailSelected;
                const hide = !(matchRole && matchQ) && !isSelected;
                c.classList.toggle('hidden', hide);
                if (!hide) tierShown++;
            });
            // Update tier count number
            const tier = block.getAttribute('data-tier');
            const numEl = block.querySelector(`.tier-count-num[data-tier="${tier}"]`);
            if (numEl) numEl.textContent = tierShown;
            // Hide whole tier-block when empty
            block.classList.toggle('hidden', tierShown === 0);
            shown += tierShown;
        });
        const shownN = document.getElementById('shown-n');
        if (shownN) shownN.textContent = shown;
        const empty = document.getElementById('empty-state');
        if (empty) empty.classList.toggle('visible', shown === 0);

        // If the currently-selected champ got hidden, close its detail panel.
        if (detailSelected) {
            const sel = document.querySelector(`.champ[data-cid="${detailSelected}"].detail-selected`);
            if (!sel || sel.classList.contains('hidden')) {
                closeDetail();
            }
        }
        // NOTE: refreshSecondaryRoleBadges() is intentionally NOT called here.
        // The badges depend ONLY on filterState.role, not the query, so running
        // that full 173-card innerHTML walk on every keystroke was pure waste.
        // It is now invoked exactly where role changes (the chip handler).
        applySearchHighlights();
    }

    function setActiveChip(role) {
        document.querySelectorAll('.chip').forEach(chip => {
            chip.classList.toggle('active', chip.getAttribute('data-role') === role);
        });
    }

    // Role chip clicks (event delegation).  "All" chip (data-role="") already
    // unsets role filter — no dedicated reset button needed.
    //
    // Two-phase (INP): the badge sweep + full-grid filter was a ~170 ms task
    // when run inside the click.  Paint the pressed chip synchronously, yield,
    // THEN filter.  A token coalesces rapid chip taps into one trailing pass
    // (each pass reads filterState at run time, so the last click wins).
    let roleFilterToken = 0;
    document.addEventListener('click', (ev) => {
        const chip = ev.target.closest('.chip');
        if (!chip) return;
        filterState.role = chip.getAttribute('data-role') || '';
        setActiveChip(filterState.role);
        const token = ++roleFilterToken;
        yieldToMain().then(() => {
            if (token !== roleFilterToken) return;
            // Role is the only input to the secondary-role badges, so refresh
            // them here (the single role-change site), not on every keystroke.
            refreshSecondaryRoleBadges();
            applyFilters();
        });
        trackEvent('role_filter_click', { role: filterState.role || 'all' });
    });

    // Keyboard activation for cards.  Enter / Space on a `.champ` or `.aug`
    // triggers the same path a click would (they're role="button" /
    // tabindex="0").  Preventing default on Space stops the page from
    // scrolling.
    document.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') {
            if (augChampsId != null) {
                closeAugChamps();
                return;
            }
            if (detailSelected && isMobileViewport()) {
                closeDetail();
                return;
            }
            if (recModalOpen) {
                recModalOpen = false;
                renderSidePanel();
                return;
            }
            if (updatesOpen) {
                updatesOpen = false;
                renderUpdatesPanel();
                return;
            }
        }
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        const t = ev.target;
        if (!t || !t.classList) return;
        if (t.classList.contains('champ') || t.classList.contains('aug') || t.classList.contains('article-card')) {
            ev.preventDefault();
            t.click();
        }
    });

    // Augment tooltip viewport-clip protection: tooltips default to "above"
    // the card.  When the card sits near the top of the viewport, the
    // tooltip would clip — flip it below instead by toggling a class
    // computed from `getBoundingClientRect`.
    document.addEventListener('mouseover', (ev) => {
        const aug = ev.target.closest && ev.target.closest('.aug');
        if (!aug) return;
        const rect = aug.getBoundingClientRect();
        // Tooltip is ~ 110-140 px tall; flip when there's less than 160 px
        // of headroom above the card.
        aug.classList.toggle('flip-tip', rect.top < 160);
    }, { passive: true });

    // Rich item float-tip: show on hover/focus of any .has-item-tip host.
    document.addEventListener('mouseover', (ev) => {
        const host = ev.target.closest && ev.target.closest('.has-item-tip');
        if (!host) return;
        // Prefer the deepest host under the cursor (e.g. core icon inside cg-head).
        showItemFloatTip(host);
    }, { passive: true });
    document.addEventListener('mouseout', (ev) => {
        const host = ev.target.closest && ev.target.closest('.has-item-tip');
        if (!host) return;
        const to = ev.relatedTarget;
        if (to && host.contains(to)) return;
        // Moving between nested hosts (core icon -> head) should not flicker off.
        if (to && to.closest && to.closest('.has-item-tip')) return;
        scheduleHideItemFloatTip();
    }, { passive: true });
    document.addEventListener('focusin', (ev) => {
        const host = ev.target.closest && ev.target.closest('.has-item-tip');
        if (host) showItemFloatTip(host);
    });
    document.addEventListener('focusout', (ev) => {
        const host = ev.target.closest && ev.target.closest('.has-item-tip');
        if (!host) return;
        const to = ev.relatedTarget;
        if (to && host.contains(to)) return;
        if (to && to.closest && to.closest('.has-item-tip')) return;
        scheduleHideItemFloatTip();
    });
    window.addEventListener('scroll', () => hideItemFloatTip(), { passive: true, capture: true });
    window.addEventListener('resize', () => hideItemFloatTip(), { passive: true });

    document.addEventListener('mouseover', (ev) => {
        const fitChip = ev.target.closest && ev.target.closest('.fit-chip-wrap');
        if (fitChip) {
            // Item build chips now use the shared float tip; keep legacy
            // positioning only for non-item fit chips that still embed
            // .fit-chip-tooltip.
            if (!fitChip.classList.contains('has-item-tip')) {
                positionFitChipTooltip(fitChip);
            }
            return;
        }
        const badge = ev.target.closest && ev.target.closest('.alt-role-badge');
        if (badge) {
            positionSecondaryRoleTooltip(badge);
            return;
        }
        const champ = ev.target.closest && ev.target.closest('.champ.secondary-role-match');
        if (!champ) return;
        positionSecondaryRoleTooltip(champ.querySelector('.alt-role-badge'));
    }, { passive: true });

    document.addEventListener('focusin', (ev) => {
        const fitChip = ev.target.closest && ev.target.closest('.fit-chip-wrap');
        if (fitChip) {
            if (!fitChip.classList.contains('has-item-tip')) {
                positionFitChipTooltip(fitChip);
            }
            return;
        }
        const badge = ev.target.closest && ev.target.closest('.alt-role-badge');
        if (badge) {
            positionSecondaryRoleTooltip(badge);
            return;
        }
        const champ = ev.target.closest && ev.target.closest('.champ.secondary-role-match');
        if (!champ) return;
        positionSecondaryRoleTooltip(champ.querySelector('.alt-role-badge'));
    });

    // Live search.  Debounced: applyFilters loops every tier-block x champ, so
    // running it on each keystroke made typing the INP-heaviest text interaction.
    // A ~120 ms trailing debounce coalesces a burst of keystrokes into one filter
    // pass while still feeling live.  Escape stays immediate (below).
    const searchEl = document.getElementById('champ-search');
    if (searchEl) {
        let searchDebounceT = null;
        searchEl.addEventListener('input', () => {
            filterState.q = searchEl.value || '';
            clearTimeout(searchDebounceT);
            searchDebounceT = setTimeout(() => { searchDebounceT = null; applyFilters(); }, 120);
        });
        // Esc inside the search clears the filter and unfocuses, so the
        // typical "open, search, escape back to grid" flow works.  Immediate:
        // cancel any pending debounced pass and apply the cleared state now.
        searchEl.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape') {
                clearTimeout(searchDebounceT);
                searchDebounceT = null;
                searchEl.value = '';
                filterState.q = '';
                applyFilters();
                searchEl.blur();
            }
        });
    }

    // Ctrl+F / Cmd+F shortcut → focus our search input.
    //
    // Rationale: our search already understands zh-TW name + English alias +
    // role keywords (gua-Liang in one go).  Native browser find can also
    // discover champions thanks to the .sr-only English alias spans, but
    // the in-page search additionally filters out non-matches — usually
    // what the user wants.
    //
    // If the user is already inside the search box, fall through to the
    // browser's native find dialog (no preventDefault) so they retain that
    // escape hatch.
    document.addEventListener('keydown', (ev) => {
        const isFind = (ev.ctrlKey || ev.metaKey) && ev.key && ev.key.toLowerCase() === 'f';
        if (!isFind) return;
        // Only hijack find on the home view; on Column/Settings the search box is
        // hidden, so let the browser's native find work there.
        const homeView = document.getElementById('view-home');
        if (!homeView || !homeView.classList.contains('is-active')) return;
        const sEl = document.getElementById('champ-search');
        if (!sEl) return;
        if (document.activeElement === sEl) return;  // let browser take over on 2nd press
        ev.preventDefault();
        sEl.focus({ preventScroll: true });
        sEl.select();
    });
})().catch(err => {
    console.error(err);
    document.body.insertAdjacentHTML('afterbegin', `<div style="margin:16px;padding:12px 14px;border:1px solid #7f1d1d;background:#2a1216;color:#ffd7dc;border-radius:8px">資料載入失敗，請稍後再試。</div>`);
});