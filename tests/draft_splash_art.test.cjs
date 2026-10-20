const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');

const source = readFileSync(join(__dirname, '../scripts/templates/site.js'), 'utf8');
const functions = source.slice(
    source.indexOf('    function draftScaleFandomHdUrl('),
    source.indexOf('    function draftSlotCropFor('),
);
const info = { alias: 'Amumu', id: 32 };
const currentRiotArt = 'https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/'
    + 'global/default/assets/characters/amumu/skins/base/images/amumu_splash_uncentered_0.jpg';

function artUrl(entry) {
    const resolve = new Function('DRAFT_SPLASH_HD', 'window',
        `${functions}\nreturn draftSlotArtUrl;`)(
        entry ? { byCid: { 32: entry } } : null,
        { location: { origin: 'https://arammeta.com' } },
    );
    return resolve(info, 32);
}

test('archived splash maps fall back to current Riot art', () => {
    for (const [key, value] of [
        ['url', 'https://example.com/Amumu_OriginalSkin_old.jpg'],
        ['remoteUrl', 'https://example.com/Amumu_OriginalSkin_old_HD.jpg'],
        ['wikiTitle', 'File:Amumu OriginalSkin old.jpg'],
    ]) {
        assert.equal(artUrl({ url: 'assets/draft-splash-hd/32.jpg', [key]: value }), currentRiotArt);
    }
});

test('current HD and local art remain preferred', () => {
    const url = 'https://static.wikia.nocookie.net/leagueoflegends/images/Amumu_OriginalSkin_HD.jpg/revision/latest?cb=1';
    assert.equal(artUrl({ url }), url.replace('/latest?', '/latest/scale-to-width-down/1600?'));
    assert.equal(artUrl({ url: 'assets/draft-splash-hd/32.jpg' }),
        'https://arammeta.com/assets/draft-splash-hd/32.jpg');
    assert.equal(artUrl(null), currentRiotArt);
});
