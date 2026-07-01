
    async function loadSitePayload(url) {
        const response = await fetch(url, { cache: 'no-cache' });
        if (!response.ok) {
            throw new Error(`payload ${response.status}: ${url}`);
        }
        return await response.json();
    }
    const DATA = __PAYLOAD__;
    // Empirical champion x team-archetype fit (decoupled artifact; absence -> heuristic fallback).
    let ARCHFIT = null;
    try {
        ARCHFIT = await loadSitePayload("api/champ-archetype-fit.json");
        if (ARCHFIT && ARCHFIT.champs && DATA.champs) {
            for (const cid in ARCHFIT.champs) {
                if (DATA.champs[cid]) DATA.champs[cid].archFit = ARCHFIT.champs[cid];
            }
        }
    } catch (e) { ARCHFIT = null; }
    // Empirical ability axes (scaling/snowball + empirical damage/tank/cc bars) merged into comp.
    try {
        const AXES = await loadSitePayload("api/champ-empirical-axes.json");
        if (AXES && AXES.champs && DATA.champs) {
            for (const cid in AXES.champs) {
                const c = DATA.champs[cid], a = AXES.champs[cid];
                if (c && c.comp && a) {
                    c.comp.scaling = a.scaling; c.comp.snowball = a.snowball;
                    c.comp.e_damage = a.e_damage; c.comp.e_tank = a.e_tank; c.comp.e_cc = a.e_cc;
                }
            }
        }
    } catch (e) {}
    const pct = x => (x * 100).toFixed(1) + '%';
    const signed = x => (x >= 0 ? '+' : '') + (x * 100).toFixed(1) + '%';
    const escHtml = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    // Capability dims we percentile-rank per champion (the building blocks of comp fit).
    const COMP_STAT_KEYS = ['front', 'engage', 'poke', 'magic', 'phys', 'sustain', 'cc', 'wave', 'damage', 'scaling', 'snowball', 'e_damage', 'e_tank', 'e_cc'];
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
    const ABILITY_BARS = [
        { key: 'e_damage', zh: '傷害', en: 'Damage' },
        { key: 'e_tank',   zh: '坦度', en: 'Tank' },
        { key: 'e_cc',     zh: '控場', en: 'CC' },
        { key: 'sustain', zh: '恢復', en: 'Sustain' },
        { key: 'scaling',  zh: '後期', en: 'Scaling' },
        { key: 'snowball', zh: '滾雪球', en: 'Snowball' },
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
    // axes: heuristic mode [{label, pct 0-1}]; signed mode (opts.signed) [{label, delta pp}].
    // Signed mode draws a dashed 0pp baseline ring; out=fits (blue), in=avoid (red); scale pp at full radius.
    function compRadarSvg(axes, ariaLabel, opts) {
        opts = opts || {};
        const signed = !!opts.signed, scale = opts.scale || 2;
        const cx = 190, cy = 158, R = 108, n = axes.length;
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
        const labels = axes.map((a, i) => {
            const [lx, ly] = at(i, R + 24);
            const anchor = Math.abs(lx - cx) < 1 ? 'middle' : (lx > cx ? 'start' : 'end');
            const valTxt = signed ? ((a.delta || 0) >= 0 ? '+' : '') + (a.delta || 0).toFixed(1) : String(Math.round((a.pct || 0) * 100));
            const valCol = signed ? ((a.delta || 0) >= 0 ? '#7fc8ff' : '#f0998a') : '#7fc8ff';
            return `<text x="${lx.toFixed(1)}" y="${(ly + 4).toFixed(1)}" font-size="13" text-anchor="${anchor}" fill="#c2c7ce">${escHtml(a.label)} <tspan fill="${valCol}">${valTxt}</tspan></text>`;
        }).join('');
        const fillCol = signed ? 'rgba(120,130,140,0.16)' : 'rgba(58,160,255,0.18)';
        const strokeCol = signed ? 'rgba(160,170,180,0.85)' : '#3aa0ff';
        return `<svg class="comp-radar" viewBox="0 0 380 320" width="100%" role="img" aria-label="${escHtml(ariaLabel)}">${grid}${baseline}${spokes}<polygon points="${dataPoly}" fill="${fillCol}" stroke="${strokeCol}" stroke-width="2"/>${dots}${labels}</svg>`;
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
    // "Comp fit" tab: a radar of how well this champion fits each of the 6 comp
    // archetypes (a champ can fit several), with the top fits called out as advice.
    function buildCompFit(info) {
        const copy = tr();
        const lang = currentLang === 'en' ? 'en' : 'zh';
        const comp = (info && info.comp) || {};
        const cap = compCapPct(comp);
        let radar, adviceHtml, metaText;
        const af = info && info.archFit;
        const signedPp = d => ((d >= 0 ? '+' : '') + d.toFixed(1) + 'pp');
        if (af && af.qualified && af.fit) {
            // Empirical: signed WR delta when the OTHER 4 teammates lean each archetype.
            const fits = COMP_FIT_DEFS.map(def => ({ def, name: def[lang].name, delta: Number((af.fit[def.key] || {}).delta || 0) }));
            radar = compRadarSvg(fits.map(f => ({ label: f.name, delta: f.delta })), copy.compFitTitle, { signed: true, scale: 2 });
            const best = [...fits].sort((a, b) => b.delta - a.delta).filter(f => f.delta >= COMP_FIT_EMP_POS).slice(0, 2);
            const avoid = [...fits].sort((a, b) => a.delta - b.delta).filter(f => f.delta <= COMP_FIT_EMP_NEG).slice(0, 1);
            const items = best.map(f =>
                `<div class="comp-advice-item"><span class="ca-tag">${escHtml(f.def[lang].name)} ${signedPp(f.delta)}</span><span class="ca-desc">${escHtml(f.def[lang].desc)}</span></div>`);
            avoid.forEach(f => items.push(
                `<div class="comp-advice-item"><span class="ca-tag" style="color:#f0998a;border-color:rgba(226,87,75,.45)">${escHtml(copy.compFitAvoid)}：${escHtml(f.def[lang].name)} ${signedPp(f.delta)}</span><span class="ca-desc">${escHtml(copy.compFitAvoidDesc)}</span></div>`));
            adviceHtml = items.length ? `<div class="comp-advice">${items.join('')}</div>` : `<div class="comp-fit-empty">${escHtml(copy.compFitFlexible)}</div>`;
            metaText = copy.compFitMetaEmp;
        } else {
            // Heuristic fallback (low sample): the champion's own ability blend, percentile-ranked.
            const stats = compFitStats();
            const fits = COMP_FIT_DEFS.map(def => {
                const raw = compFitRaw(def, cap);
                const arr = stats[def.key];
                const n = arr ? arr.length : 0;
                let lt = 0;
                while (lt < n && arr[lt] < raw) lt += 1;
                const pct = n < 2 ? 0 : Math.max(0, Math.min(1, lt / (n - 1)));
                return { def, name: def[lang].name, pct };
            });
            const advice = [...fits].sort((a, b) => b.pct - a.pct).filter(f => f.pct >= COMP_FIT_ADVICE_THRESHOLD).slice(0, 3).map(f => f.def[lang]);
            adviceHtml = advice.length
                ? `<div class="comp-advice">${advice.map(a => `<div class="comp-advice-item"><span class="ca-tag">${escHtml(a.name)}</span><span class="ca-desc">${escHtml(a.desc)}</span></div>`).join('')}</div>`
                : `<div class="comp-fit-empty">${escHtml(copy.compFitFlexible)}</div>`;
            radar = compRadarSvg(fits.map(f => ({ label: f.name, pct: f.pct })), copy.compFitTitle);
            metaText = copy.compFitMetaEst;
        }
        const abilities = ABILITY_BARS.map(a => {
            const val = Math.round((cap[a.key] || 0) * 100);
            return `<div class="ab-row"><span class="ab-label">${escHtml(a[lang])}</span><span class="ab-bar"><span style="width:${val}%"></span></span><span class="ab-val">${val}</span></div>`;
        }).join('');
        // Skill-scaling chip ("operation coefficient"): WR(high-skill lobbies) - WR(low-skill).
        const ss = info && info.skillScaling;
        let skillChip = '';
        if (ss && typeof ss.pp === 'number') {
            const strong = Math.abs(ss.z || 0) >= 2 && Math.abs(ss.pp) >= 2;
            const pos = ss.pp >= 0;
            const ppTxt = (pos ? '+' : '') + ss.pp.toFixed(1) + 'pp';
            const col = !strong ? '#9aa3ad' : (pos ? '#3aa0ff' : '#e2574b');
            const lbl = lang === 'en'
                ? (!strong ? 'skill-neutral' : pos ? 'rewards skill' : 'stomps low elo')
                : (!strong ? '中性' : pos ? '吃操作' : '低分強勢');
            const nameTxt = lang === 'en' ? 'Skill-scaling' : '操作係數';
            const titleTxt = lang === 'en'
                ? 'Win-rate in high-skill minus low-skill lobbies (top vs bottom 25% by lobby skill)'
                : '高分局勝率 − 低分局勝率（依對局水平前 25% vs 後 25%）';
            skillChip = `<span class="cf-skill" title="${escHtml(titleTxt)}">${escHtml(nameTxt)} <b style="color:${col}">${ppTxt}</b><span style="color:${col}">· ${escHtml(lbl)}</span></span>`;
        }
        return `
            <div class="detail-section">
                <div class="detail-section-head">
                    <h3>${escHtml(copy.compFitTitle)}</h3>
                    <span class="section-meta">${escHtml(metaText)}</span>
                    ${skillChip}
                </div>
                <div class="comp-fit-main">
                    <div class="comp-fit-radar">${radar}</div>
                    <div class="comp-fit-abilities">
                        <div class="cf-cap">${escHtml(copy.compAbilityCap)}</div>
                        ${abilities}
                    </div>
                </div>
                ${adviceHtml}
            </div>
        `;
    }
    const ROLE_LABELS = __ROLE_LABELS__;
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
    const HEADER_TITLE_ZH = __HEADER_TITLE_ZH__;
    const HEADER_TITLE_EN = __HEADER_TITLE_EN__;
    const SHORT_PATCH_ZH = __SHORT_PATCH_ZH__;
    const DATE_STR_ZH = __DATE_STR_ZH__;
    const BUILD_DATE = __BUILD_DATE__;
    const PATCH_LABEL = __PATCH_LABEL__;
    const TOTAL_GAMES = __TOTAL_GAMES__;
    const LANG_KEY = 'aram-mayhem-site-lang';
    const THEME_KEY = 'aram-mayhem-site-theme';
    const VIEWS = ['home', 'augments', 'changes', 'column', 'settings'];
    // Column articles.  Bilingual; `body_*` is trusted HTML, everything else is
    // escaped at render time.  Add new entries here — newest first.
    const ARTICLES = [
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
            subtitle: () => `${SHORT_PATCH_ZH}`,
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
            sideSub: '依歷史搭配排序，並修正傷害比例與陣容缺口。<br>推薦度越高越適合；可信度是資料穩定度摘要。',
            closeRecs: '關閉推薦組合',
            openRecs: n => `看推薦組合 (${n})`,
            langToggleLabel: 'EN',
            langToggleTitle: 'Switch to English',
            langToggleAria: '切換語言',
            removePick: name => `移除 ${name}`,
            pickEmpty: '尚未選擇',
            maxOnly: n => `最多只能選 ${n} 隻英雄。`,
            pickNoteEmpty: n => `最多選 ${n} 隻；先看推薦度，再看原因與樣本。`,
            pickNotePartial: want => `目前這組選角的完整資料較少，先用已知搭配排序。`,
            pickNoteReady: (want, minGames) => `已選 ${want}/${MAX_TEAM_PICKS} 隻；pair 門檻 >= ${minGames} 場。`,
            panelEmpty: '先開啟「選擇你的隊友」，再從英雄列表點 1~4 隻英雄。系統會排出最適合補進來的英雄。',
            panelNoData: '這組英雄目前沒有足夠的 pair 資料。',
            detailEmpty: '這個英雄目前沒有可顯示的資料。',
            detailClose: '關閉詳細資訊',
            pairSectionTitle: '推薦搭檔',
            pairSectionMeta: '適配度為主，勝率為輔',
            setSectionTitle: '增幅裝置系列相性',
            setSectionMeta: '保守分數；負值代表相對較好，但未達正訊號',
            itemSectionTitle: '最強前兩件出裝',
            itemSectionMeta: '不含鞋子，左到右為第 1 到第 3 推薦',
            itemClusterSectionTitle: '',
            itemClusterSectionMeta: '依出裝順序分群：核心＝最先做的兩件（從提早結束的場次推回）；「搭配裝備」列出與核心常一起出、且選取率或勝率達標的所有裝備（各附勝率／選取率）；收尾為其餘常見後續',
            augTypeSectionTitle: '推薦增幅裝置傾向',
            augTypeSectionMeta: '細分類優先；分數扣掉同角色／傷害型英雄的平均偏好',
            relativeBest: '相對最佳',
            best: '最佳',
            worst: '最差',
            bestAugments: '最佳增幅裝置',
            worstAugments: '最差增幅裝置',
            augmentStrengthMeta: '強度綜合參考勝率與選取率',
            augmentStrengthTip: '排序以勝率提升的保守估計為主，並搭配選取率判斷樣本穩定度；低選取率的高勝率會更保守看待。卡片上的選用率數字依熱門程度上色：灰→藍→黃→橙→紅，越紅越熱門。',
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
            itemClusterCardTitle: (name, wr, pick, lift, games, confirm, lane) => `${name}〔${lane}〕· 勝率 ${wr}（${lift}）· 選取率 ${pick} · 該選擇 ${games} 場 · 完整六件確認 ${confirm} 場`,
            itemClusterLanes: { popular: { label: '熱門' }, winrate: { label: '高勝率' }, combined: { label: '綜合' } },
            itemClusterPick: (pick) => `${pick} 選取`,
            itemClusterGames: (games) => `${games} 場`,
            coreBuildShare: (pick) => `${pick} 選取率`,
            coreBuildWr: (wr) => `勝率 ${wr}`,
            coreBuildThird: '搭配裝備',
            coreBuildTail: '常見後續',
            coreBuildTailTip: '這套常一起出、但選取率與勝率都未達上方「搭配裝備」門檻的裝備',
            coreBuildHeadTitle: (name, wr, lift, pick, games) => `${name} · 勝率 ${wr}（${lift}）· 選取率 ${pick} · ${games} 場`,
            expected: value => ` · 預期 ${value}`,
            recRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · 推薦度 ${fit} · 搭配 ${pairFit} · 陣容 ${comp} · ${confidence}`,
            leastFitLabel: '最不適配',
            leastFitRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · 最不適配 ${fit} · 搭配 ${pairFit} · 陣容 ${comp} · ${confidence}`,
            champCardTitle: (name, wr, games, raw) => `${name} · WR ${wr} · games ${games} · raw ${raw}`,
            champCardAria: (name, alias, tier, wr) => `${name} ${alias}，tier ${tier}，勝率 ${wr}`,
            secondaryRoleBadgeTitle: (style, pick, lift) => `${style} 選取率 ${pick}，勝率${lift}`,
            secondaryRoleBadgePick: pick => `選取率 ${pick}`,
            secondaryRoleBadgeLift: lift => `勝率${lift}`,
            augPickLabel: pick => `選用率 ${pick}`,
            augHotBadge: '熱門',
            augFilterAll: '全部',
            augFilterAllTip: '清除分類，顯示全部增幅',
            augFilterNewTip: patch => `${patch} 新推出的增幅`,
            augFilterEmpty: '此分類沒有符合的增幅',
            overviewWrLabel: '綜合勝率',
            overviewGames: games => `${games} 場`,
            compFitTitle: '適配陣型',
            compFitMeta: '左：適配陣型；右：英雄能力（皆為全英雄百分位）',
            compFitMetaEmp: '左：實測適配（隊友走該型時的勝率差 pp，正=圍繞他組／負=該避免）；右：能力百分位',
            compFitMetaEst: '左：適配陣型（出賽量不足，暫以能力推估）；右：能力百分位',
            compFitAvoid: '避免',
            compFitAvoidDesc: '隊友角色重複，發揮變差',
            compAbilityCap: '英雄能力',
            compFitFlexible: '綜合型，沒有特別突出的適配陣型，可彈性搭配各種陣容',
        },
        en: {
            htmlLang: 'en',
            subtitle: () => `${SHORT_PATCH_ZH}`,
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
            sideSub: 'Ranked by teammate fit, then adjusted for damage mix and team gaps.<br>Higher fit is better; confidence summarizes data stability.',
            closeRecs: 'Close recommendations',
            openRecs: n => `Open recommendations (${n})`,
            langToggleLabel: '中',
            langToggleTitle: '切換成中文',
            langToggleAria: 'Switch language',
            removePick: name => `Remove ${name}`,
            pickEmpty: 'Empty',
            maxOnly: n => `You can only pick up to ${n} champions.`,
            pickNoteEmpty: n => `Pick up to ${n}; read fit first, then reason and sample size.`,
            pickNotePartial: want => `This selected group has less complete data, so the list uses known teammate fits first.`,
            pickNoteReady: (want, minGames) => `${want}/${MAX_TEAM_PICKS} picked; pair threshold >= ${minGames} games.`,
            panelEmpty: 'Turn on teammate mode, then click 1-4 champions in the grid. The site will rank the best additions.',
            panelNoData: 'This combination does not have enough pair data yet.',
            detailEmpty: 'No detail data is available for this champion yet.',
            detailClose: 'Close details',
            pairSectionTitle: 'Recommended Pairings',
            pairSectionMeta: 'Fit first, win rate second',
            setSectionTitle: 'Augment Sets',
            setSectionMeta: 'Conservative score; negative can still be relative-best',
            itemSectionTitle: 'Best First Two Items',
            itemSectionMeta: 'boots excluded; left to right is #1 to #3',
            itemClusterSectionTitle: '',
            itemClusterSectionMeta: 'grouped by build order: core = the first 2 items built (inferred from games that ended early); the "other items" row lists every item commonly paired with the core that clears a pick-rate or win-rate bar (each with its win/pick rate); finish = other common follow-ups',
            augTypeSectionTitle: 'Recommended Augment Tendencies',
            augTypeSectionMeta: 'Fine-grained first; scores are adjusted against similar role/damage-profile champions.',
            relativeBest: 'Relative Best',
            best: 'Best',
            worst: 'Worst',
            bestAugments: 'Best Augments',
            worstAugments: 'Worst Augments',
            augmentStrengthMeta: 'Strength considers both win rate and pick rate',
            augmentStrengthTip: 'Ranking is led by conservative win-rate lift, with pick rate used as a stability signal; low-pick high-win results are treated more carefully. The pick-rate figure on each card is colour-coded by popularity on a heat ramp: grey -> blue -> yellow -> orange -> red, redder = hotter.',
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
            itemClusterCardTitle: (name, wr, pick, lift, games, confirm, lane) => `${name} [${lane}] · WR ${wr} (${lift}) · pick ${pick} · ${games} core games · ${confirm} full-build confirmations`,
            itemClusterLanes: { popular: { label: 'Popular' }, winrate: { label: 'High WR' }, combined: { label: 'Balanced' } },
            itemClusterPick: (pick) => `${pick} pick`,
            itemClusterGames: (games) => `${games}g`,
            coreBuildShare: (pick) => `${pick} pick`,
            coreBuildWr: (wr) => `${wr} WR`,
            coreBuildThird: 'Other items',
            coreBuildTail: 'Also common',
            coreBuildTailTip: 'Built with this core but below the pick-rate / win-rate bar of the items above',
            coreBuildHeadTitle: (name, wr, lift, pick, games) => `${name} · WR ${wr} (${lift}) · pick ${pick} · ${games} games`,
            expected: value => ` · expected ${value}`,
            recRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · fit ${fit} · pair ${pairFit} · comp ${comp} · ${confidence}`,
            leastFitLabel: 'Least fit',
            leastFitRowTitle: (name, fit, pairFit, comp, confidence) => `${name} · least fit ${fit} · pair ${pairFit} · comp ${comp} · ${confidence}`,
            champCardTitle: (name, wr, games, raw) => `${name} · WR ${wr} · games ${games} · raw ${raw}`,
            champCardAria: (name, alias, tier, wr) => `${name} ${alias}, tier ${tier}, win rate ${wr}`,
            secondaryRoleBadgeTitle: (style, pick, lift) => `${style} pick ${pick}, WR ${lift}`,
            secondaryRoleBadgePick: pick => `pick ${pick}`,
            secondaryRoleBadgeLift: lift => `WR ${lift}`,
            augPickLabel: pick => `pick ${pick}`,
            augHotBadge: 'Hot',
            augFilterAll: 'All',
            augFilterAllTip: 'Clear categories — show every augment',
            augFilterNewTip: patch => `Augments introduced in ${patch}`,
            augFilterEmpty: 'No augments match this category',
            overviewWrLabel: 'Overall WR',
            overviewGames: games => `${games} games`,
            compFitTitle: 'Comp fit',
            compFitMeta: 'left: comp fit; right: abilities (both percentile across all champions)',
            compFitMetaEmp: 'left: measured fit (WR delta when teammates lean each comp; + build around him / − avoid); right: ability percentiles',
            compFitMetaEst: 'left: comp fit (low sample — estimated from abilities); right: ability percentiles',
            compFitAvoid: 'Avoid',
            compFitAvoidDesc: 'redundant teammates — underperforms',
            compAbilityCap: 'Abilities',
            compFitFlexible: 'Flexible — no standout comp fit, slots into many comps',
        }
    };
    let currentLang = 'zh';
    let updatesOpen = false;
    let activeUpdateTab = 'heroes';
    let filterState = { role: '', q: '' };

    function tr() {
        return COPY[currentLang] || COPY.zh;
    }

    function roleLabel(role) {
        const labels = ROLE_LABELS[currentLang] || ROLE_LABELS.zh;
        return labels[role] || role || '';
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
        const searchEl = document.getElementById('champ-search');
        if (!searchEl) return;
        const copy = tr();
        searchEl.placeholder = searchPlaceholderFor(copy);
        searchEl.setAttribute('aria-label', copy.searchAria);
    }

    function champName(info) {
        if (!info) return '';
        return currentLang === 'en' ? (info.name_en || info.alias || info.name || '') : (info.name_zh || info.name || info.alias || '');
    }

    function augName(aug) {
        if (!aug) return '';
        return currentLang === 'en' ? (aug.name_en || aug.name || '') : (aug.name_zh || aug.name || '');
    }

    function augDesc(aug) {
        if (!aug) return '';
        if (currentLang === 'en') return aug.desc_en || aug.desc || '';
        return aug.desc_zh || aug.desc || '';
    }

    function augSetName(aug) {
        if (!aug) return '';
        if (currentLang === 'en') return aug.set_en || aug.set || '';
        return aug.set_zh || aug.set || aug.set_en || '';
    }

    function setEntryName(entry) {
        if (!entry) return '';
        if (currentLang === 'en') return entry.name_en || entry.name || '';
        return entry.name_zh || entry.name || entry.name_en || '';
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
            entry?.set,
            entry?.set_zh,
            entry?.set_en,
            entry?.slug,
        ];
        (entry?.items || []).forEach(item => {
            parts.push(item.name, item.name_zh, item.name_en, item.id);
        });
        return parts.filter(Boolean).join(' ');
    }

    function currentSearchQuery() {
        const searchEl = document.getElementById('champ-search');
        return searchEl ? searchEl.value : filterState.q;
    }

    function applySearchHighlights(root = document) {
        const q = currentSearchQuery();
        root.querySelectorAll('[data-match-text]').forEach(card => {
            card.classList.toggle('search-hit', searchMatchesText(card.getAttribute('data-match-text') || '', q));
        });
    }

    function buildAugCard(entry, kind, opts) {
        const aug = DATA.augs[entry.id];
        const name = aug ? augName(aug) : '#' + entry.id;
        const icon = aug && aug.icon ? aug.icon : '';
        const rarity = aug ? (aug.rarity || '') : '';
        const desc = augDesc(aug);
        const setName = augSetName(aug);
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
        const titleAttr = copy.augTitle(name, setName, pct(entry.wr), entry.g, desc);
        const tooltip = `
            <div class="aug-tip">
                <div class="aug-tip-name">${escHtml(name)}</div>
                ${setName ? `<div class="aug-tip-set">${copy.augSetLabel}: ${escHtml(setName)}</div>` : ''}
                ${desc ? `<div class="aug-tip-desc">${escHtml(desc)}</div>` : ''}
                <div class="aug-tip-stat">${copy.augTipStat(pct(entry.wr), signed(entry.lift), entry.g)}</div>
            </div>
        `;
        // Augment card carries its own ARIA semantics so screen readers and
        // keyboard users get the same info hover tooltip shows.
        // The 增幅榜 board's pick is "average appearances per game" (counts
        // multiplicity), a different scale than the champion-relative pick the
        // per-champion cards carry, so the board uses its own 熱門 cut + heat ramp.
        const onBoard = !!(opts && opts.board);
        const pickRate = Number(entry.pick || 0);
        const pickPct = pct(pickRate);
        const isHot = pickRate >= (onBoard ? AUG_BOARD_HOT_PICK : AUG_HOT_PICK);
        const hotBadge = isHot ? `<span class="aug-hot-badge">${copy.augHotBadge}</span>` : '';
        const augTierCls = onBoard ? augBoardPickTier(pickRate) : pickTier(pickRate);
        const cats = (aug && Array.isArray(aug.cats)) ? aug.cats.join(' ') : '';
        // rawWr is the unsmoothed win rate; fall back to the headline for older
        // payloads built before rawWr was emitted.
        const rawWrVal = (entry.rawWr != null ? entry.rawWr : entry.wr);
        const rawLine = (currentLang === 'en' ? 'raw ' : '原始 ') + pct(rawWrVal) + ' · n=' + entry.g;
        const ariaLabel = copy.augAria(name, pct(entry.wr), signed(entry.lift), entry.g, desc)
            + ' · ' + rawLine;
        return `
            <div class="aug ${kind} rarity-${rarity}"
                 tabindex="0"
                 data-match-text="${escHtml(matchText)}"
                 data-cats="${escHtml(cats)}"
                 aria-label="${escHtml(ariaLabel)}"
                 title="${escHtml(titleAttr)}">
                ${hotBadge}
                ${icon ? `<img loading="lazy" src="${icon}" alt="">` : '<div style="width:48px;height:48px;margin:0 auto 4px;background:#2a3142;border-radius:6px"></div>'}
                <div class="aname">${escHtml(name)}</div>
                <div class="awr">${pct(entry.wr)}</div>
                <div class="araw">${escHtml(rawLine)}</div>
                <div class="alift pick-${augTierCls}">${copy.augPickLabel(pickPct)}</div>
                ${tooltip}
            </div>
        `;
    }

    // Champion-relative pick rate (0-1) at or above this flags an augment as 熱門.
    const AUG_HOT_PICK = 0.20;
    // 增幅榜 board pick = average appearances per game (counts multiplicity), so it
    // runs on its own scale: the most-taken augment sits near ~0.75/game, the
    // median near ~0.14, and a value can exceed 1.0.  Give the board its own 熱門
    // cut + heat ramp so the colour still means "popular" relative to the field.
    const AUG_BOARD_HOT_PICK = 0.40;
    function augBoardPickTier(pick) {
        if (pick >= 0.40) return 5;
        if (pick >= 0.28) return 4;
        if (pick >= 0.18) return 3;
        if (pick >= 0.10) return 2;
        return 1;
    }
    // Bucket a pick rate (0-1) into a popularity tier (1=rare .. 5=very hot) for
    // the cool->hot colour ramp shared by augment cards AND every item surface
    // (clusters / core-build options / first-two & single items / boots / core
    // rush share).  Absolute cuts (2/5/10/20%) so the colour means the same
    // thing everywhere; tier 5 (>=20%) lines up with the augment 熱門 badge.
    function pickTier(pick) {
        if (pick >= 0.20) return 5;
        if (pick >= 0.10) return 4;
        if (pick >= 0.05) return 3;
        if (pick >= 0.02) return 2;
        return 1;
    }
    const RARITIES = [
        { key: 'kPrismatic', css: 'prismatic' },
        { key: 'kGold',      css: 'gold' },
        { key: 'kSilver',    css: 'silver' },
    ];
    const MATE_LIST_LIMIT_DESKTOP = 8;
    const MATE_LIST_LIMIT_MOBILE = 6;

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
        return currentLang === 'en' ? (lbl.en || lbl.zh || cat) : (lbl.zh || lbl.en || cat);
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
        return `<div class="aug-cat-bar" role="group" aria-label="${escHtml(currentLang === 'en' ? 'Augment categories' : '增幅分類')}">${chips.join('')}</div>`;
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
        if (!rar) return currentLang === 'en' ? 'All' : '全部';
        const l = AUG_RARITY_LABELS[rar];
        return l ? (currentLang === 'en' ? l.en : l.zh) : rar;
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
        const catChips = [`<button type="button" class="aug-cat-chip aug-cat-all${allCat ? ' is-active' : ''}" data-tcat="" aria-pressed="${allCat}">${escHtml(currentLang === 'en' ? 'All' : '全部')}</button>`]
            .concat(order.map(cat => {
                const on = augTierCats.has(cat);
                return `<button type="button" class="aug-cat-chip cat-${cat}${on ? ' is-active' : ''}" data-tcat="${cat}" aria-pressed="${on}">${escHtml(augCatLabel(cat))}</button>`;
            })).join('');
        const rarLbl = currentLang === 'en' ? 'Rarity' : '稀有度';
        const catLbl = currentLang === 'en' ? 'Category' : '分類';
        host.innerHTML =
            `<div class="aug-cat-bar aug-rarity-bar" role="group" aria-label="${escHtml(rarLbl)}">${rarChips}</div>`
            + `<div class="aug-cat-bar" role="group" aria-label="${escHtml(catLbl)}">${catChips}</div>`;
    }
    function renderAugmentTier() {
        const host = document.getElementById('aug-tier-host');
        if (!host) return;
        buildAugTierFilters();
        const tierMeta = (DATA && DATA.tiers) || {};
        const order = (tierMeta.order && tierMeta.order.length) ? tierMeta.order : ['OP','T1','T2','T3','T4','T5'];
        const colors = tierMeta.colors || {};
        const entries = computeAugTiers(augTierEntries());
        if (!entries.length) {
            host.innerHTML = `<div class="empty-state visible">${escHtml(currentLang === 'en' ? 'Augment win-rate data is not available yet.' : '尚無增幅勝率資料。')}</div>`;
            return;
        }
        const byTier = {};
        entries.forEach(e => { (byTier[e.tier] = byTier[e.tier] || []).push(e); });
        const unit = currentLang === 'en' ? '' : '個';
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

    function buildRarityRow(items, kind, r) {
        const copy = tr();
        const cards = (items || []).map(e => {
            const cardKind = kind === 'ranked'
                ? (Number(e.lift || 0) >= 0 ? 'good' : 'bad')
                : kind;
            return buildAugCard(e, cardKind);
        }).join('');
        const body = cards
            ? `<div class="aug-list">${cards}</div>`
            : `<div class="aug-list empty-list">${copy.insufficient}</div>`;
        return `
            <div class="rarity-row">
                <div class="rlabel ${r.css}">${copy.rarityLabels[r.key]}</div>
                ${body}
            </div>
        `;
    }

    function renderDetail(cid) {
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
        const augmentRankTitle = currentLang === 'en' ? 'Augment Ranking' : '增幅裝置排行';
        const singleItemTitle = currentLang === 'en' ? 'Single Item Strength' : '單件裝備強度';
        const singleItemMeta = currentLang === 'en'
            ? 'counts any final-slot item; strongest first, swipe for more'
            : '六格中出過就計入；由強到弱，右滑看更多';
        const singleItemBadTitle = currentLang === 'en' ? 'Common Traps' : '常見但不推薦';
        const singleItemBadMeta = currentLang === 'en'
            ? 'high-pick negative-lift items; commonly built, but they drag this champion below baseline'
            : '高出場但負 lift；很多人出，但相對該英雄 baseline 會拉低勝率';
        const topRows = RARITIES.map(r => buildRarityRow(top[r.key], 'ranked', r)).join('');
        const pairs = info.pairs || [];
        const mateLimit = isMobileViewport() ? MATE_LIST_LIMIT_MOBILE : MATE_LIST_LIMIT_DESKTOP;
        const mateTop = pairs.slice(0, mateLimit);
        const mateBot = [...pairs].slice(-mateLimit).reverse();
        const buildMateCard = (entry, kind) => {
            const mate = DATA.champs[String(entry.id)];
            const name = mate ? champName(mate) : ('#' + entry.id);
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
            const name = setEntryName(entry);
            const score = kind === 'bad' ? (entry.badScore ?? entry.res) : (entry.score ?? entry.res);
            const titleAttr = copy.setTitle(name, signed(entry.res), signed(entry.lift), signed(entry.avg), pct(entry.wr), entry.g);
            const pairItems = Array.isArray(entry.items) ? entry.items : [];
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
                return `
                    <span class="fit-chip-wrap" tabindex="0" aria-label="${escHtml(itemTitle)}">
                        <span class="fit-chip ${kind} item-build-chip">
                            ${itemIcons}<span class="fit-chip-label">${escHtml(name)}</span>
                        </span>
                        <span class="fit-chip-tooltip" role="tooltip">
                            <span class="fit-tip-name">${escHtml(name)}</span>
                            <span class="fit-tip-pick">${escHtml(copy.secondaryRoleBadgePick(pct(entry.pick || 0)))}</span>
                            <span class="fit-tip-lift ${liftClass}">${escHtml(copy.secondaryRoleBadgeLift(signed(liftValue)))}</span>
                        </span>
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
            const name = setEntryName(entry);
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
                const pickHeat = 'pick-' + pickTier(pickVal);
                const clusterTitle = copy.itemClusterCardTitle
                    ? copy.itemClusterCardTitle(name, pct(entry.wr || 0), pct(pickVal), signed(liftValue), games, confirm, laneInfo ? laneInfo.label : lane)
                    : titleForItemCard(name, pct(entry.wr || 0), pct(pickVal), signed(liftValue), games);
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
                    <div class="item-build-card item-cluster-card lane-${lane}" tabindex="0" data-match-text="${escHtml(matchText)}" title="${escHtml(clusterTitle)}" aria-label="${escHtml(clusterTitle)}">
                        <div class="item-cluster-top">${laneBadge}<span class="cluster-games">${escHtml(gamesText)}</span></div>
                        <div class="item-build-icons">${icons}</div>
                        <div class="item-cluster-stats">
                            <span class="item-build-wr ${wrSign}">${pct(entry.wr || 0)}</span>
                            <span class="cluster-pick ${pickHeat}">${escHtml(pickText)}</span>
                        </div>
                        <div class="item-build-name"><span>${escHtml(name)}</span></div>
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
            const icons = pairItems.slice(0, iconLimit).map(item => (
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
            const wrClass = options.singleItem && !options.bootItem && liftValue < -0.0005
                ? 'item-build-wr is-bad'
                : 'item-build-wr';
            return `
                <div class="${cardClass}" tabindex="0" data-match-text="${escHtml(matchText)}" title="${escHtml(titleAttr)}" aria-label="${escHtml(titleAttr)}">
                    <div class="item-build-icons">${paddedIcons}</div>
                    <div class="${wrClass}">${pct(entry.wr || 0)}</div>
                    <div class="item-build-pick pick-${pickTier(Number(entry.pick || 0))}">${pct(entry.pick || 0)}</div>
                    <div class="item-build-name"><span>${escHtml(name)}</span></div>
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
            return `<div class="${carouselClass}">${rows.map(entry => buildItemCard(entry, options)).join('')}</div>`;
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
        const selectCommonTrapRows = (payload, maxRows = 4) => {
            const sourceRows = (payload && (payload.popularBad || payload.bot)) || [];
            const badRows = sourceRows
                .filter(entry => Number(entry.lift ?? 0) <= -0.01);
            if (!badRows.length) return [];
            return [...badRows]
                .sort((a, b) => (
                    Number(b.pick ?? b.pick_rate ?? 0) - Number(a.pick ?? a.pick_rate ?? 0)
                    || Number(b.g ?? b.games ?? 0) - Number(a.g ?? a.games ?? 0)
                    || Number(a.lift ?? 0) - Number(b.lift ?? 0)
                    || String(a.name_en || '').localeCompare(String(b.name_en || ''))
                ))
                .slice(0, maxRows);
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
        const buildAffinitySection = (title, meta, payload, options = {}) => {
            if (options.itemCarousel) {
                const rows = (payload && payload.top) || [];
                if (!rows.length) return '';
                const itemMeta = currentLang === 'en'
                    ? 'boots excluded; strongest first, swipe for more'
                    : '不含鞋子；勝率分數由高到低，右滑看更多';
                const displayMeta = (options.singleItem || options.itemCluster) && meta ? meta : itemMeta;
                const metaHtml = `<span class="section-meta">${displayMeta}</span>`;
                return `
                    <div class="detail-section">
                        <div class="detail-section-head">
                            <h3>${title}</h3>
                            ${metaHtml}
                        </div>
                        ${buildItemCarousel(rows, {
                            singleItem: Boolean(options.singleItem),
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
                const nm = escHtml((item && (item.name_zh || item.name)) || '');
                return (item && item.icon)
                    ? `<img class="${cls}" src="${escHtml(item.icon)}" alt="${nm}" title="${nm}" loading="lazy">`
                    : `<span class="${cls}"></span>`;
            };
            const blocks = groups.map(grp => {
                const core = Array.isArray(grp.core) ? grp.core : [];
                const coreIcons = core.map(it => iconImg(it, 'cg-core-icon')).join('<span class="cg-arrow">▸</span>');
                const options = (Array.isArray(grp.options) ? grp.options : []).map(o => {
                    const lift = Number(o.lift || 0);
                    const wrSign = lift > 0.005 ? 'is-good' : (lift < -0.005 ? 'is-bad' : 'is-even');
                    const pickVal = Number(o.pick || 0);
                    const pickHeat = 'pick-' + pickTier(pickVal);
                    const laneInfo = laneLabels[o.lane];
                    const badge = laneInfo ? `<span class="lane-badge lane-${o.lane}">${escHtml(laneInfo.label)}</span>` : '';
                    const tip = copy.itemClusterCardTitle
                        ? copy.itemClusterCardTitle(o.name_zh || o.name || '', pct(o.wr || 0), pct(pickVal), signed(lift), o.g || 0, o.exactGames || 0, laneInfo ? laneInfo.label : (o.lane || ''))
                        : (o.name_zh || o.name || '');
                    return `
                        <div class="cg-option" tabindex="0" title="${escHtml(tip)}" aria-label="${escHtml(tip)}">
                            ${iconImg(o, 'cg-option-icon')}
                            <span class="cg-option-wr ${wrSign}">${pct(o.wr || 0)}</span>
                            <span class="cg-option-pick ${pickHeat}">${pct(pickVal)}</span>
                            ${badge}
                        </div>`;
                }).join('');
                const tail = Array.isArray(grp.tail) ? grp.tail : [];
                const tailTip = copy.coreBuildTailTip ? ` title="${escHtml(copy.coreBuildTailTip)}"` : '';
                const tailBlock = tail.length
                    ? `<div class="cg-tail"${tailTip}><span class="cg-tail-label">${copy.coreBuildTail || '收尾'}</span>${tail.map(it => iconImg(it, 'cg-tail-icon')).join('')}</div>`
                    : '';
                const share = copy.coreBuildShare ? copy.coreBuildShare(pct(grp.pick || 0)) : pct(grp.pick || 0);
                const coreLift = Number(grp.lift || 0);
                const coreWrSign = coreLift > 0.005 ? 'is-good' : (coreLift < -0.005 ? 'is-bad' : 'is-even');
                const wrText = copy.coreBuildWr ? copy.coreBuildWr(pct(grp.wr || 0)) : pct(grp.wr || 0);
                const wrHtml = grp.wr ? `<span class="cg-core-wr ${coreWrSign}">${escHtml(wrText)}</span>` : '';
                const headTitle = copy.coreBuildHeadTitle
                    ? copy.coreBuildHeadTitle(grp.name_zh || grp.name || '', pct(grp.wr || 0), signed(coreLift), pct(grp.pick || 0), grp.g || 0)
                    : '';
                return `
                    <div class="core-group">
                        <div class="cg-head"${headTitle ? ` title="${escHtml(headTitle)}"` : ''}>
                            <div class="cg-core">${coreIcons}</div>
                            <div class="cg-core-meta">
                                ${wrHtml}
                                <span class="cg-core-share pick-${pickTier(Number(grp.pick || 0))}">${escHtml(share)}</span>
                            </div>
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
                const name = setEntryName(entry);
                const pairItems = Array.isArray(entry.items) ? entry.items : [];
                const icon = pairItems[0] && pairItems[0].icon;
                const wr = Number(entry.wr || 0);
                const liftValue = Number(entry.lift ?? entry.res ?? 0);
                const wrSign = liftValue > 0.005 ? 'is-good' : (liftValue < -0.005 ? 'is-bad' : 'is-even');
                const pickVal = Number(entry.pick || 0);
                const tip = tipFn(name, pct(wr), pct(pickVal), signed(liftValue), entry.g || 0);
                return `
                    <div class="boot-rail-row${idx === 0 ? ' is-top' : ''}" tabindex="0" data-match-text="${escHtml(entrySearchText(entry))}" title="${escHtml(tip)}" aria-label="${escHtml(tip)}">
                        ${icon ? `<img class="boot-rail-icon" src="${escHtml(icon)}" alt="" loading="lazy">` : '<span class="boot-rail-icon"></span>'}
                        <span class="boot-rail-name">${escHtml(name)}</span>
                        <span class="boot-rail-wr ${wrSign}">${pct(wr)}</span>
                        <span class="boot-rail-pick pick-${pickTier(pickVal)}">${pct(pickVal)}</span>
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
            const goodSection = buildAffinitySection(title, meta, payload, { itemCarousel: true, singleItem: true });
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
        const buildDetailTabSet = (scope, tabs, extraClass = '') => {
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
            return `
                <div class="detail-tabset ${extraClass}">
                    ${inputs}
                    <div class="detail-tab-list" role="tablist">${labels}</div>
                    <div class="detail-tab-panels">${panels}</div>
                </div>
            `;
        };
        const mainTabLabels = currentLang === 'en'
            ? { overview: 'Overview', items: 'Items', augments: 'Augments', compfit: 'Comp fit' }
            : { overview: '概覽', items: '出裝', augments: '增幅裝置', compfit: '適配陣型' };
        const bootItemTitle = currentLang === 'en' ? 'Recommended Boots' : '推薦鞋子';
        const bootItemMeta = currentLang === 'en' ? 'WR · pick' : '勝率 · 選取率';
        const spellRailTitle = currentLang === 'en' ? 'Summoner Spells' : '召喚師技能';
        // Mayhem players carry two spells, so pick rates sum to ~200%.
        const spellRailMeta = currentLang === 'en' ? 'WR · pick · 2 per player' : '勝率 · 選取率 · 每人 2 個';
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
                    ${buildBootRail(spellRailTitle, spellRailMeta, spellInfo, { limit: 5, extraClass: 'spell-rail-section' })}
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
        const compFitTabContent = buildCompFit(info);
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
        const detailTabs = buildDetailTabSet('main', [
            { key: 'overview', label: mainTabLabels.overview, content: overviewTabContent },
            { key: 'items', label: mainTabLabels.items, content: itemTabContent },
            { key: 'augments', label: mainTabLabels.augments, content: augmentTabContent },
            { key: 'compfit', label: mainTabLabels.compfit, content: compFitTabContent },
        ], 'detail-main-tabs');
        return `
            <button class="detail-close" type="button" title="${escHtml(copy.detailClose)}" aria-label="${escHtml(copy.detailClose)}">&times;</button>
            <div class="detail-head">
                ${info.image ? `<img class="detail-avatar" loading="lazy" src="${info.image}" alt="">` : ''}
                <span class="cname" id="detail-title-${cid}">${escHtml(champName(info))}</span>
                ${buildDetailRoleTags(info)}
            </div>
            ${detailTabs}
        `;
    }

    const REC_LIST_LIMIT = 12;
    const MAX_TEAM_PICKS = 4;
    let detailSelected = null;
    let recommendMode = false;
    let recModalOpen = false;
    let teamPicks = [];
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
            return currentLang === 'en' ? 'High confidence' : '可信度高';
        }
        if (row.coverageRatio >= 0.5 && row.minGames >= 40 && signal >= 0.6) {
            return currentLang === 'en' ? 'Medium confidence' : '可信度中';
        }
        return currentLang === 'en' ? 'Early signal' : '樣本偏早';
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
                return currentLang === 'en' ? `adds frontline ${signed(value)}` : `補前排 ${signed(value)}`;
            }
            if (row.beforePokeGroup === 'poke lack' && row.afterPokeGroup === 'poke ok') {
                return currentLang === 'en' ? `adds poke ${signed(value)}` : `補Poke ${signed(value)}`;
            }
            if (row.beforeWaveGroup === 'wave lack' && row.afterWaveGroup === 'wave ok') {
                return currentLang === 'en' ? `adds waveclear ${signed(value)}` : `補清兵 ${signed(value)}`;
            }
            if (row.beforeEngageGroup === 'engage lack' && row.afterEngageGroup === 'engage ok') {
                return currentLang === 'en' ? `adds engage ${signed(value)}` : `補開戰 ${signed(value)}`;
            }
            if (row.beforeAllLacksGroup !== row.afterAllLacksGroup) {
                return currentLang === 'en' ? `rounds team ${signed(value)}` : `補陣容 ${signed(value)}`;
            }
        }
        if (abs < 0.001) return currentLang === 'en' ? 'team neutral' : '陣容中性';
        if (value > 0) return currentLang === 'en' ? `team +${(value * 100).toFixed(1)}%` : `陣容加分 ${signed(value)}`;
        return currentLang === 'en' ? `team ${(value * 100).toFixed(1)}%` : `陣容扣分 ${signed(value)}`;
    }

    function recMetaHtml(row, name) {
        const copy = tr();
        const scoreClass = row.leastFit ? 'fit-worst' : recScoreClass(row.fitScore);
        const scoreLabel = row.leastFit
            ? copy.leastFitLabel
            : (currentLang === 'en' ? 'Fit' : '推薦度');
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
        const isMobile = window.matchMedia('(max-width: 700px)').matches;
        if (!showPanel || !isMobile) recModalOpen = false;
        shell.classList.toggle('with-side-panel', showPanel && !isMobile);
        document.body.classList.toggle('rec-modal-open', showPanel && isMobile && recModalOpen);
        panel.classList.toggle('is-modal-open', showPanel && isMobile && recModalOpen);
        panel.classList.toggle('is-hidden', !showPanel || (isMobile && !recModalOpen));
        if (fab) {
            fab.classList.toggle('is-hidden', !(showPanel && isMobile && !recModalOpen));
            fab.textContent = copy.openRecs(teamPicks.length);
        }
        if (!showPanel) return;

        const chips = [];
        teamPicks.forEach((cid, idx) => {
            const info = DATA.champs[cid];
            const name = info ? champName(info) : ('#' + cid);
            const image = info && info.image ? info.image : '';
            chips.push(
                `<button class="pick-chip" type="button" data-remove-cid="${cid}" title="${escHtml(copy.removePick(name))}">` +
                `<span class="ord">${idx + 1}</span>` +
                (image ? `<img loading="lazy" src="${image}" alt="">` : '') +
                `<span>${escHtml(name)}</span></button>`
            );
        });
        for (let i = teamPicks.length; i < MAX_TEAM_PICKS; i += 1) {
            chips.push(`<div class="pick-chip empty"><span class="ord">${i + 1}</span>${copy.pickEmpty}</div>`);
        }
        slots.innerHTML = chips.join('');

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
            const name = info ? champName(info) : ('#' + row.id);
            const image = info && info.image ? info.image : '';
            const confidence = confidenceLabel(row);
            const meta = recMetaHtml(row, name);
            const title = row.leastFit
                ? copy.leastFitRowTitle(name, signed(row.fitScore), signed(row.pairFitScore), signed(row.compositionContribution), confidence)
                : copy.recRowTitle(name, signed(row.fitScore), signed(row.pairFitScore), signed(row.compositionContribution), confidence);
            return `
                <button class="rec-row${row.leastFit ? ' least-fit' : ''}" type="button" data-cid="${row.id}" title="${escHtml(title)}">
                    <span class="rec-rank">${idx + 1}</span>
                    ${image ? `<img loading="lazy" src="${image}" alt="">` : '<div style="width:40px;height:40px;border-radius:8px;background:#2a3142"></div>'}
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
            const name = champName(info);
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
        return {
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
    }

    function fmtInt(n) {
        return Number(n || 0).toLocaleString(currentLang === 'en' ? 'en-US' : 'zh-TW');
    }

    function localizedEntityName(entity) {
        if (!entity) return '';
        return currentLang === 'en'
            ? (entity.name_en || entity.alias || entity.name || entity.id || '')
            : (entity.name_zh || entity.name || entity.name_en || entity.alias || entity.id || '');
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
        return currentLang === 'en'
            ? (a[base + '_en'] || a[base + '_zh'] || '')
            : (a[base + '_zh'] || a[base + '_en'] || '');
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
        if (_scatterRO) { _scatterRO.disconnect(); _scatterRO = null; }
        const host = document.getElementById('column-host');
        if (!host) return;
        const head = currentLang === 'en' ? 'Column' : '專欄';
        const sub = currentLang === 'en'
            ? 'The thinking behind the data, and how to play it.'
            : '資料背後的思考與玩法解析。';
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
        const back = currentLang === 'en' ? 'Back to Column' : '返回專欄';
        host.innerHTML = `<div class="article-reader">`
            + `<button class="article-back" data-article-back type="button">← ${escHtml(back)}</button>`
            + `<div class="article-hero${articleField(a, 'cover_image') ? ' article-hero--img' : ''}">${articleCoverMedia(a, escHtml(a.id) + '-hero', 'xMinYMax slice')}</div>`
            + `<h1>${escHtml(articleField(a, 'title'))}</h1>`
            + `<div class="article-meta">${escHtml(articleField(a, 'kicker'))} · ${escHtml(a.date)}</div>`
            + `<div class="article-body">${articleField(a, 'body')}</div></div>`;
        if (document.getElementById('scatter-host')) renderScalingSnowballChart();
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
            rows.push({ name: champName(info), img: info.image || '', wr: Number(info.wr) || 0, g: Number(info.g) || 0, sx, sy });
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
        const q = en ? { tr: 'Snowball + late', tl: 'Late game', br: 'Snowball', bl: 'Neither' }
                     : { tr: '滾雪球強 · 後期強', tl: '後期強', br: '滾雪球強', bl: '兩者皆弱' };
        const xTitle = en ? 'Snowball  →' : '滾雪球能力  →';
        const yTitle = en ? 'Late game  →' : '後期能力  →';
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
                : `${r.name}  ·  後期 ${signed(r.sy)}  ·  滾雪球 ${r.sx.toFixed(2)}  ·  勝率 ${pct(r.wr)}  ·  ${r.g}場`;
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
        const active = bar && bar.querySelector('.nav-tab.active');
        if (!bar || !active) return;
        bar.style.setProperty('--ind-x', active.offsetLeft + 'px');
        bar.style.setProperty('--ind-w', active.offsetWidth + 'px');
    }
    // Slide the segmented control's fill behind its active option.  Snaps by
    // default; pass animate=true (theme toggle) for the slide.  Skips while the
    // control is hidden / not yet laid out (offsetWidth 0) so a hidden or
    // prematurely-measured seg never collapses the thumb to width 0 -- it keeps
    // its last good geometry until the seg is genuinely visible.
    function moveSegThumb(seg, animate) {
        seg = seg || document.getElementById('theme-seg');
        if (!seg) return;
        const active = seg.querySelector('button.active') || seg.querySelector('button');
        const thumb = seg.querySelector('.seg-thumb');
        if (!active || !thumb || !active.offsetWidth) return;
        if (!animate) thumb.style.transition = 'none';
        thumb.style.left = (active.offsetLeft - seg.clientLeft) + 'px';
        thumb.style.width = active.offsetWidth + 'px';
        if (!animate) { void thumb.offsetWidth; thumb.style.transition = ''; }
    }
    function setActiveView(name, instant) {
        if (!VIEWS.includes(name)) name = 'home';
        const apply = () => {
            document.querySelectorAll('.nav-tab[data-nav-tab]').forEach(t => {
                const on = t.getAttribute('data-nav-tab') === name;
                t.classList.toggle('active', on);
                t.setAttribute('aria-selected', on ? 'true' : 'false');
                t.tabIndex = on ? 0 : -1;  // roving tabindex (WAI-ARIA tabs)
            });
            // Mirror the active highlight onto the mobile drawer items (plain
            // buttons, not a tablist — so no aria-selected / roving tabindex).
            document.querySelectorAll('.nav-drawer-item[data-nav-tab]').forEach(t => {
                t.classList.toggle('active', t.getAttribute('data-nav-tab') === name);
            });
            document.querySelectorAll('.view[data-view]').forEach(v => {
                v.classList.toggle('is-active', v.getAttribute('data-view') === name);
            });
            if (name === 'column') {
                columnArticle ? renderArticle(columnArticle) : renderColumnList();
            }
            if (name === 'augments') {
                renderAugmentTier();
            }
            if (name === 'changes') {
                renderUpdatesPanel();
            }
            if (('#' + name) !== location.hash) {
                try { history.replaceState(null, '', '#' + name); } catch { location.hash = name; }
            }
            window.scrollTo(0, 0);
            moveTabIndicator();
            moveSegThumb();  // settings seg is measurable only while its view is visible
        };
        // Cross-fade the panel via the View Transitions API; root is pinned so the
        // header and the scrollTo above don't animate.  Skip on first paint
        // (instant) and for reduced-motion / unsupported browsers.
        const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (instant || reduce || !document.startViewTransition) { apply(); return; }
        // Flag the VT so .vt-running suppresses the indicator/thumb CSS tweens
        // while the snapshots are captured; clear it once the VT settles.
        const root = document.documentElement;
        root.classList.add('vt-running');
        document.startViewTransition(apply).finished.finally(() => {
            root.classList.remove('vt-running');
            moveSegThumb();  // seg is laid out now if we landed on settings; snap the thumb
        });
    }
    function applyTheme(theme) {
        const t = theme === 'light' ? 'light' : 'dark';
        const root = document.documentElement;
        // Kill transitions for one tick so var()-driven backgrounds snap to the
        // new palette instead of sticking mid-transition (see .no-theme-transition).
        root.classList.add('no-theme-transition');
        root.setAttribute('data-theme', t);
        setTimeout(() => root.classList.remove('no-theme-transition'), 60);
        document.querySelectorAll('#theme-seg [data-theme-choice]').forEach(b => {
            const on = b.getAttribute('data-theme-choice') === t;
            b.classList.toggle('active', on);
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
        moveSegThumb(undefined, true);  // animate the fill across on theme toggle
        try { localStorage.setItem(THEME_KEY, t); } catch {}
    }

    function applyLanguage(nextLang) {
        currentLang = nextLang === 'en' ? 'en' : 'zh';
        const copy = tr();
        document.documentElement.lang = copy.htmlLang;
        try { localStorage.setItem(LANG_KEY, currentLang); } catch {}

        const titleEl = document.getElementById('site-title');
        if (titleEl) titleEl.textContent = currentLang === 'en' ? HEADER_TITLE_EN : HEADER_TITLE_ZH;
        const subtitleEl = document.getElementById('site-subtitle');
        if (subtitleEl) subtitleEl.innerHTML = copy.subtitle();
        updateSearchPlaceholder();
        const shownUnit = document.getElementById('shown-unit');
        if (shownUnit) shownUnit.textContent = copy.shownUnit;
        document.querySelectorAll('.tier-count-unit').forEach(el => {
            el.textContent = copy.tierUnit;
        });
        document.querySelectorAll('.chip').forEach(chip => {
            chip.textContent = currentLang === 'en'
                ? (chip.getAttribute('data-label-en') || chip.textContent || '')
                : (chip.getAttribute('data-label-zh') || chip.textContent || '');
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
        const toggle = document.getElementById('lang-toggle');
        const toggleLabel = document.getElementById('lang-toggle-label');
        if (toggle) {
            toggle.title = copy.langToggleTitle;
            toggle.setAttribute('aria-label', copy.langToggleAria);
        }
        if (toggleLabel) toggleLabel.textContent = copy.langToggleLabel;

        // Static chrome strings (nav tabs, settings, augment placeholder) carry
        // their own zh/en text via data-i18n-* attributes.
        document.querySelectorAll('[data-i18n-zh]').forEach(el => {
            const val = currentLang === 'en'
                ? el.getAttribute('data-i18n-en')
                : el.getAttribute('data-i18n-zh');
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

        moveTabIndicator();
        moveSegThumb();
        updateChampCardCopy();
        refreshSecondaryRoleBadges();
        renderUpdatesPanel();
        setRecommendMode(recommendMode);
        renderSidePanel();
        if (detailSelected) {
            const champ = document.querySelector(`.champ[data-cid="${detailSelected}"].detail-selected`);
            if (champ) openDetailForChamp(champ, true);
        }
    }

    function setRecommendMode(next) {
        recommendMode = Boolean(next);
        if (!recommendMode) recModalOpen = false;
        const btn = document.getElementById('recommend-mode');
        if (!btn) return;
        btn.classList.toggle('active', recommendMode);
        btn.setAttribute('aria-pressed', recommendMode ? 'true' : 'false');
        btn.textContent = recommendMode ? tr().recModeOn : tr().recModeOff;
    }

    function syncDetailModalState() {
        document.body.classList.toggle('detail-modal-open', Boolean(detailSelected) && isMobileViewport());
    }

    function closeDetail() {
        document.querySelectorAll('.detail-host').forEach(h => h.innerHTML = '');
        document.querySelectorAll('.champ.detail-selected').forEach(el => el.classList.remove('detail-selected'));
        detailSelected = null;
        syncDetailModalState();
    }

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

        const dialogAttrs = isMobileViewport()
            ? ` role="dialog" aria-modal="true" aria-labelledby="detail-title-${cid}"`
            : '';
        host.innerHTML = `<div class="detail"${dialogAttrs}>${renderDetail(cid)}</div>`;
        applySearchHighlights(host);
        applyAugCatFilter(host);
        champ.classList.add('detail-selected');
        detailSelected = cid;
        syncDetailModalState();
        if (isMobileViewport()) {
            host.querySelector('.detail-close')?.focus({ preventScroll: true });
        }
        if (!force) {
            trackEvent('champion_detail_open', {
                champion_id: cid,
                champion_name: champ.getAttribute('data-name-en') || '',
                tier: champ.getAttribute('data-tier') || '',
            });
        }
    }

    function openDetailByCid(cid) {
        // The detail host lives inside the home tier grid; callers can fire from
        // the Settings changelog or a recommend row, so always surface the home
        // view first or the panel would open in a hidden view (invisible).
        setActiveView('home');
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

    // ---- Mobile nav drawer (側欄) open/close ----
    const navBurger = document.getElementById('nav-burger');
    const navDrawer = document.getElementById('nav-drawer');
    const navDrawerBackdrop = document.getElementById('nav-drawer-backdrop');
    function setNavDrawer(open) {
        if (!navDrawer) return;
        navDrawer.classList.toggle('open', open);
        navDrawer.setAttribute('aria-hidden', open ? 'false' : 'true');
        if (navDrawerBackdrop) navDrawerBackdrop.classList.toggle('open', open);
        if (navBurger) navBurger.setAttribute('aria-expanded', open ? 'true' : 'false');
        document.body.classList.toggle('nav-drawer-open', open);
    }

    document.addEventListener('click', (ev) => {
        const ghStar = ev.target.closest('.gh-star');
        if (ghStar) {
            trackEvent('github_star_click', { location: ghStar.closest('.nav-drawer') ? 'drawer' : 'header' });
            return;
        }
        if (ev.target.closest('#nav-burger')) {
            setNavDrawer(!(navDrawer && navDrawer.classList.contains('open')));
            return;
        }
        if (ev.target.closest('#nav-drawer-close') || ev.target.closest('#nav-drawer-backdrop')) {
            setNavDrawer(false);
            return;
        }
        const navTab = ev.target.closest('[data-nav-tab]');
        if (navTab) {
            const view = navTab.getAttribute('data-nav-tab');
            setActiveView(view);
            setNavDrawer(false);  // navigating from the drawer dismisses it
            trackEvent('view_change', { view });
            return;
        }
        const themeBtn = ev.target.closest('[data-theme-choice]');
        if (themeBtn) {
            const theme = themeBtn.getAttribute('data-theme-choice');
            applyTheme(theme);
            trackEvent('theme_toggle', { theme });
            return;
        }
        const articleCard = ev.target.closest('[data-article]');
        if (articleCard) {
            const id = articleCard.getAttribute('data-article');
            renderArticle(id);
            window.scrollTo(0, 0);
            trackEvent('article_open', { id });
            return;
        }
        if (ev.target.closest('[data-article-back]')) {
            renderColumnList();
            window.scrollTo(0, 0);
            return;
        }
        const langBtn = ev.target.closest('[data-lang-toggle]');
        if (langBtn) {
            const nextLang = currentLang === 'en' ? 'zh' : 'en';
            applyLanguage(nextLang);
            trackEvent('language_toggle', { language: nextLang });
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
        const modeBtn = ev.target.closest('#recommend-mode');
        if (modeBtn) {
            const nextMode = !recommendMode;
            setRecommendMode(nextMode);
            pickNotice = '';
            renderSidePanel();
            trackEvent('recommend_mode_toggle', { enabled: nextMode });
            return;
        }
        const removeBtn = ev.target.closest('[data-remove-cid]');
        if (removeBtn) {
            const removedCid = removeBtn.getAttribute('data-remove-cid');
            teamPicks = teamPicks.filter(cid => cid !== removeBtn.getAttribute('data-remove-cid'));
            pickNotice = '';
            syncPickDecorations();
            renderSidePanel();
            trackEvent('team_pick_remove', { champion_id: removedCid, picks: teamPicks.length });
            return;
        }
        const recRow = ev.target.closest('.rec-row');
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
        const cid = champ.getAttribute('data-cid');
        if (recommendMode) {
            toggleTeamPick(cid);
            trackEvent('team_pick_toggle', { champion_id: cid, picks: teamPicks.length });
            return;
        }
        openDetailForChamp(champ);
    });

    // When viewport width changes, the row containing the selected champ
    // shifts — re-anchor the detail host so it stays directly under that
    // champ on the new layout.
    let resizeT = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeT);
        resizeT = setTimeout(() => {
            if (!isMobileViewport()) setNavDrawer(false);  // never strand the drawer on desktop
            updateSearchPlaceholder();
            renderSidePanel();
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

    function rehydrateItems() {
        // Restore the item identities (name + icon) that the build stripped into
        // DATA.itemLut to shrink the payload (see _dedupe_item_objects).  Every
        // embedded item was tagged "ic":1 with its id + per-row stats kept; we put
        // name / name_zh / name_en / icon back in place so all downstream render
        // code sees the original object shape.  Backward-compatible: a payload with
        // no itemLut (full item objects) is left untouched.  Must run before
        // enrichSearchIndexes() (which reads item names) and any renderDetail().
        const lut = DATA.itemLut;
        if (!lut) return;
        const ver = DATA.ddv || '';
        function visit(node) {
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
                    node.icon = ver
                        ? ('https://ddragon.leagueoflegends.com/cdn/' + ver + '/img/item/' + node.id + '.png')
                        : '';
                }
                for (const k in node) visit(node[k]);
            }
        }
        Object.values(DATA.champs || {}).forEach(visit);
    }

    function enrichSearchIndexes() {
        document.querySelectorAll('.champ[data-cid]').forEach(champ => {
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
        });
    }

    try {
        const savedLang = localStorage.getItem(LANG_KEY);
        if (savedLang === 'en' || savedLang === 'zh') currentLang = savedLang;
    } catch {}

    rehydrateItems();
    enrichSearchIndexes();
    setRecommendMode(false);
    syncPickDecorations();
    renderSidePanel();
    applyLanguage(currentLang);

    // ---- Chrome init: theme, tab routing ----
    try {
        const savedTheme = localStorage.getItem(THEME_KEY);
        applyTheme(savedTheme === 'light' ? 'light' : 'dark');
    } catch { applyTheme('dark'); }
    (function initView() {
        const start = (location.hash || '').replace('#', '');
        setActiveView(VIEWS.includes(start) ? start : 'home', true);  // instant: no View Transition on first paint
    })();
    // Position the sliding indicator/thumb now (base CSS has transition:none so
    // they snap), commit that layout with a reflow, THEN enable the transitions
    // so only later tab/theme changes animate -- never a grow-from-0 on load.
    // Synchronous (not requestAnimationFrame) so it still runs when the tab is
    // backgrounded / not painting (rAF callbacks are parked there).
    moveTabIndicator();
    moveSegThumb();
    void document.documentElement.offsetWidth;  // reflow: commit initial geometry
    document.documentElement.classList.add('ui-ready');
    // Web fonts can resize tab/segment labels after load -- re-anchor once settled.
    window.addEventListener('load', () => { moveTabIndicator(); moveSegThumb(); });
    if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(() => { moveTabIndicator(); moveSegThumb(); });
    }
    window.addEventListener('hashchange', () => {
        const v = (location.hash || '').replace('#', '');
        setActiveView(VIEWS.includes(v) ? v : 'home');
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
        setActiveView(next.getAttribute('data-nav-tab'));
    });
    // Scroll-aware header: lift it once content scrolls underneath.
    const siteHeaderEl = document.querySelector('.site-header');
    if (siteHeaderEl) {
        const onHeaderScroll = () => siteHeaderEl.classList.toggle('scrolled', window.scrollY > 4);
        window.addEventListener('scroll', onHeaderScroll, { passive: true });
        onHeaderScroll();
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
        refreshSecondaryRoleBadges();
        applySearchHighlights();
    }

    function setActiveChip(role) {
        document.querySelectorAll('.chip').forEach(chip => {
            chip.classList.toggle('active', chip.getAttribute('data-role') === role);
        });
    }

    // Role chip clicks (event delegation).  "All" chip (data-role="") already
    // unsets role filter — no dedicated reset button needed.
    document.addEventListener('click', (ev) => {
        const chip = ev.target.closest('.chip');
        if (!chip) return;
        filterState.role = chip.getAttribute('data-role') || '';
        setActiveChip(filterState.role);
        applyFilters();
        trackEvent('role_filter_click', { role: filterState.role || 'all' });
    });

    // Keyboard activation for cards.  Enter / Space on a `.champ` or `.aug`
    // triggers the same path a click would (they're role="button" /
    // tabindex="0").  Preventing default on Space stops the page from
    // scrolling.
    document.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') {
            if (navDrawer && navDrawer.classList.contains('open')) {
                setNavDrawer(false);
                if (navBurger) navBurger.focus();
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

    document.addEventListener('mouseover', (ev) => {
        const fitChip = ev.target.closest && ev.target.closest('.fit-chip-wrap');
        if (fitChip) {
            positionFitChipTooltip(fitChip);
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
            positionFitChipTooltip(fitChip);
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

    // Live search.
    const searchEl = document.getElementById('champ-search');
    if (searchEl) {
        searchEl.addEventListener('input', () => {
            filterState.q = searchEl.value || '';
            applyFilters();
        });
        // Esc inside the search clears the filter and unfocuses, so the
        // typical "open, search, escape back to grid" flow works.
        searchEl.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape') {
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
    