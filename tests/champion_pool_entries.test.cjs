const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('scripts/templates/site.js', 'utf8');
const start = source.indexOf('    function championPoolCategory(');
const end = source.indexOf('    function championPoolAugHtml(', start);
const context = vm.createContext({apoolWeight: w => w || 100});
vm.runInContext(source.slice(start, end), context);
const data = JSON.parse(fs.readFileSync('docs/api/augment-pools.json', 'utf8'));
const catalogue = JSON.parse(fs.readFileSync('docs/api/tier-list.json', 'utf8')).augs;
const pools = Object.fromEntries(data.pools.map(p => [p.id, p]));
let champions = 0;
for (const cid of Object.keys(data.champs)) {
    const rows = context.championPoolEntries(cid, data, catalogue);
    const expected = new Map();
    for (const [pid, w] of data.champs[cid]) {
        for (const id of pools[pid].augs) {
            const key = String(id);
            expected.set(key, Math.max(expected.get(key) || 0, w || 100));
        }
    }
    assert.equal(rows.length, expected.size);
    assert.equal(new Set(rows.map(r => r.id)).size, rows.length);
    rows.forEach((row, i) => {
        assert.equal(row.weight, expected.get(row.id));
        assert.ok(i === 0 || rows[i-1].weight >= row.weight);
        assert.equal(row.sources.length, new Set(row.sources.map(s => s.pool.id)).size);
        assert.ok(row.sources.every(s => s.weight <= row.weight));
    });
    champions++;
}
assert.equal(context.championPoolCategory(['new']), 'other');
assert.equal(context.championPoolCategory(['crit', 'amp']), 'amp');
assert.equal(context.championPoolCategory(['ad', 'gold']), 'gold');
assert.equal(context.championPoolEntries('missing',data,catalogue).length,0);
console.log(`Verified unique membership, max weight, ordering and source provenance for ${champions} champions.`);
