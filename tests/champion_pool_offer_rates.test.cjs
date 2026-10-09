const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('scripts/templates/site.js', 'utf8');
const start = source.indexOf('    const APOOL_GRANT_RATIO =');
const end = source.indexOf('    function championPoolAugHtml(', start);
const ctx = vm.createContext({apoolAug: id => ({rarity: rarities.get(String(id)) || ''})});
const rarities = new Map();
vm.runInContext(source.match(/const AUGMENT_LADDER = \{[\s\S]*?\n    \};/)[0], ctx);
vm.runInContext(source.match(/const AUG_RARITY_OF_CODE = .*;/)[0], ctx);
vm.runInContext(source.match(/const AUG_DRAFT_OFFER = \d+;/)[0], ctx);
vm.runInContext(source.slice(start, end), ctx);
const originalLadder = vm.runInContext('({...AUGMENT_LADDER})', ctx);
function ladder(value) {
    ctx.ladder = value;
    vm.runInContext('Object.keys(AUGMENT_LADDER).forEach(k => delete AUGMENT_LADDER[k]); Object.assign(AUGMENT_LADDER, ladder);', ctx);
}
function entries(n, rarity, weight = 100, offset = 0) {
    return Array.from({length: n}, (_, i) => {
        const id = String(offset + i);
        rarities.set(id, rarity);
        return {id, weight};
    });
}
function close(actual, expected) {
    assert.ok(Math.abs(actual - expected) < 1e-11, `${actual} != ${expected}`);
}

// Independent reference: enumerate ordered draws of individual cards, rather
// than grouped counts. Stop when the target is seen, so it counts only once.
function reference(weights, draws, target = 0) {
    function miss(available, left) {
        if (!left) return 1;
        const total = available.reduce((sum, id) => sum + weights[id], 0);
        return available.reduce((sum, id) => id === target ? sum : sum
            + weights[id] / total * miss(available.filter(x => x !== id), left - 1), 0);
    }
    return 1 - miss(weights.map((_, i) => i), Math.min(draws, weights.length));
}
const weights = [0.8, 0.8, 1, 1, 1.32, 1.58, 1.76, 1.76];
const grouped = ctx.apoolExposureByDrawCount([0.8, 1, 1.32, 1.58, 1.76], [2, 2, 1, 1, 2], [0, 3, 6, 12]);
for (const n of [0, 3, 6, 12]) {
    [0, 2, 4, 5, 6].forEach((target, i) => close(grouped.get(n)[i], reference(weights, n, target)));
}

// Cross-round exclusion must give 12/18, not independent six-card attempts.
ladder({PPGG: 1});
let rows = entries(18, 'kPrismatic');
ctx.championPoolOfferRates(rows, {}).forEach(rate => close(rate, 12 / 18));
ladder({PPPP: 1});
ctx.championPoolOfferRates(rows, {}).forEach(rate => close(rate, 1));

// Unequal sequence frequencies, zero-rarity ladders, and full pool exhaustion.
ladder({GGGG: 1, PGGG: 2, PPGG: 3, PPPP: 4});
ctx.championPoolOfferRates(rows, {}).forEach(rate => close(rate, (2 / 3 + 2 + 4) / 10));
rows = entries(3, 'kPrismatic');
ctx.championPoolOfferRates(rows, {}).forEach(rate => close(rate, 0.9));

// Same weight, separate rarity pools: each rarity has its own denominator.
ladder({SPGG: 1});
rows = [...entries(12, 'kSilver', 200), ...entries(24, 'kPrismatic', 200, 100)];
let rates = ctx.championPoolOfferRates(rows, {});
close(rates.get('0'), 0.5);
close(rates.get('100'), 0.25);
assert.equal(rates.size, rows.length);
rates = ctx.championPoolOfferRates(rows, {observed: {dead: [{id: 0}]}});
assert.equal(rates.has('0'), false);
close(rates.get('1'), 6 / 11);
assert.equal(ctx.championPoolOfferRates([], {}).size, 0);
rows = entries(4, 'unknown');
ctx.championPoolOfferRates(rows, {}).forEach(rate => close(rate, 0));

// Real payload regression, including rarity lookup and server exclusions.
ladder(originalLadder);
const pool = JSON.parse(fs.readFileSync('docs/api/augment-pools.json', 'utf8'));
const catalogue = JSON.parse(fs.readFileSync('docs/api/tier-list.json', 'utf8')).augs;
Object.entries(pool.augs).forEach(([id, aug]) => rarities.set(id, catalogue[id]?.rarity || aug.rarity));
const membershipSource = source.slice(source.indexOf('    function championPoolCategory('), start);
const built = fs.readFileSync('docs/assets/site.js', 'utf8');
ctx.AUGMENT_TAXONOMY = JSON.parse(built.match(/const AUGMENT_TAXONOMY = (.*);/)[1]);
ctx.apoolWeight = weight => weight || 100;
vm.runInContext(membershipSource, ctx);
rows = ctx.championPoolEntries('63', pool, catalogue);
const before = performance.now();
rates = ctx.championPoolOfferRates(rows, pool);
const coldMs = performance.now() - before;
const cachedBefore = performance.now();
const cached = ctx.championPoolOfferRates(rows, pool);
assert.deepEqual([...cached], [...rates]);
rates.forEach(rate => assert.ok(Number.isFinite(rate) && rate >= 0 && rate <= 1));
// All three Brand pools have at least 24 cards: exactly 24 distinct cards are
// seen in every four-round ladder, so inclusion probabilities must sum to 24.
close([...rates.values()].reduce((sum, rate) => sum + rate, 0), 24);
assert.ok(rates.get('1045') > 0.16957);
const summary = {brandInfernalConduit: rates.get('1045'), coldMs, cachedMs: performance.now() - cachedBefore};
console.log('Verified exact exposure, cross-round exclusion, ladder weighting, rarity boundaries, exhaustion, dead cards, and cache stability.');
console.log(JSON.stringify(summary));
