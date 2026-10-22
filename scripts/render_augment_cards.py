"""Render 4:5 (1080x1350) social cards for Mayhem augments.

Each card: augment icon + name, one-line hook, short description, win rate,
pick rate, and the 5 best-fit champions (top-5 by lcb, shown by lift).
Data comes from the live arammeta.com API, not the local DB, so the numbers
match what the site shows. Template: scripts/templates/augment_card.html.

Edit CARDS (augment id, icon file, hook, description) and run:

    python scripts/render_augment_cards.py --out <dir>

Descriptions are hand-written; put a manual <br> where the CJK line should
break, or Chrome splits words mid-phrase.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import urllib.request

SITE = "https://arammeta.com"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
TEMPLATE = pathlib.Path(__file__).parent / "templates" / "augment_card.html"

# (augment id, icon file in /assets/icons/, hook line, description)
CARDS = [
    (3078, "theboomandthebass_gold_tint1", "技能接普攻，打完爆發再拉開",
     "施放技能後，下一次普攻更快、附加魔法傷害，<br>並獲得逐漸衰減的跑速。"),
    (3163, "enchantedbuttery_gold_tint1", "奶得越多，打得越痛",
     "治療或護盾隊友時收集蝴蝶；<br>集滿後傷害敵方英雄，一次放出全部蝴蝶造成魔法傷害。"),
    (3131, "stillhere_gold_tint1", "越殘越兇，血線換攻速與吸血",
     "依已損失生命，獲得攻速與普攻吸血。"),
    (3049, "armoredimpact_gold_tint1", "技能後一拳，範圍物理爆發",
     "施放技能後，下一次普攻對周圍敵軍造成額外物理傷害。"),
    (3086, "bees_gold_tint1", "技能命中就放蜂，緩速＋持續傷害",
     "技能命中敵方英雄時，定期丟出蜂巢：<br>緩速並持續造成魔法傷害，附帶命中效果。"),
]


def fetch(url: str, dest: pathlib.Path) -> None:
    if not dest.exists():
        req = urllib.request.Request(url, headers={"User-Agent": "arammeta-cards"})
        dest.write_bytes(urllib.request.urlopen(req, timeout=30).read())


def fetch_json(path: str):
    req = urllib.request.Request(SITE + path, headers={"User-Agent": "arammeta-cards"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def top_champs(stats: dict, champs: dict, aid: int, n: int = 5):
    rows = [(e["lcb"], cid, e)
            for cid, r in stats.items() for e in r.get("kGold", []) if e["id"] == aid]
    rows.sort(key=lambda x: -x[0])
    top = [(champs[c], e) for _, c, e in rows[:n]]
    return sorted(top, key=lambda x: -x[1]["lift"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=pathlib.Path)
    args = ap.parse_args()
    out = args.out.resolve()
    img = out / "img"
    img.mkdir(parents=True, exist_ok=True)

    tl = fetch_json("/api/tier-list.json")
    stats = fetch_json("/api/champions/augment-stats.json")
    patch = tl["patch_prefix"]
    ddragon = json.load(urllib.request.urlopen(
        "https://ddragon.leagueoflegends.com/api/versions.json", timeout=30))[0]
    tpl = TEMPLATE.read_text(encoding="utf-8")

    for i, (aid, icon, hook, desc) in enumerate(CARDS, 1):
        a = tl["augs"][str(aid)]
        champs = top_champs(stats, tl["champs"], aid)
        fetch(f"{SITE}/assets/icons/{icon}.png", img / f"{icon}.png")
        for c, _ in champs:
            fetch(f"https://ddragon.leagueoflegends.com/cdn/{ddragon}/img/champion/{c['alias']}.png",
                  img / f"{c['alias']}.png")
        splash = champs[0][0]["alias"] + "_splash.jpg"
        fetch(f"https://ddragon.leagueoflegends.com/cdn/img/champion/splash/{champs[0][0]['alias']}_0.jpg",
              img / splash)

        champ_html = "".join(
            f'''<div class="ch">
  <div class="rank">{k}</div>
  <img src="img/{c["alias"]}.png">
  <div class="cn">{c["name_zh"]}</div>
  <div class="lift">+{e["lift"]*100:.1f}%</div>
  <div class="cwr">勝率 {e["wr"]*100:.1f}%</div>
</div>''' for k, (c, e) in enumerate(champs, 1))
        html = (tpl.replace("{{SPLASH}}", f"img/{splash}")
                   .replace("{{ICON}}", f"img/{icon}.png")
                   .replace("{{NAME}}", a["name_zh"])
                   .replace("{{NFS}}", "80px" if len(a["name_zh"]) > 5 else "88px")
                   .replace("{{EN}}", a["name_en"])
                   .replace("{{TAG}}", hook)
                   .replace("{{DESC}}", desc)
                   .replace("{{WR}}", f"{a['wr']*100:.1f}")
                   .replace("{{PICK}}", f"{a['pick']*100:.1f}")
                   .replace("{{PATCH}}", patch)
                   .replace("{{CHAMPS}}", champ_html)
                   .replace("{{IDX}}", str(i))
                   .replace("{{TOTAL}}", str(len(CARDS))))
        page = out / f"card{i}.html"
        page.write_text(html, encoding="utf-8")
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=1", "--window-size=1080,1350",
                        "--virtual-time-budget=8000", f"--screenshot={out / f'card{i}.png'}",
                        page.as_uri()], check=True, capture_output=True)
        print(i, a["name_zh"], [c["name_zh"] for c, _ in champs])


if __name__ == "__main__":
    main()
