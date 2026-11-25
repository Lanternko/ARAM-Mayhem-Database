"""Visible per-route explanation for the app's tab routes.

Every tab route ships the same app body, so without this block each URL's
first response reads identically.  The block states, in the route's own
language, what that page measures and how to read it.  All routes' text is in
the markup; only the active route's section is visible, and ``site.js`` just
toggles ``hidden`` on view changes (language switches reuse ``data-i18n-*``).
"""
from __future__ import annotations

import html
import re

# key → language → (heading, paragraphs)
ROUTE_INTROS: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    "home": {
        "zh": ("關於這份英雄榜", (
            "這份英雄榜用 ARAM 大亂鬥（Mayhem）的實戰勝率，把英雄分成 OP 到 T5 六級。勝率有做貝氏修正，場次少的英雄會被拉回平均，才不會打沒幾場就衝上榜首。",
            "點任何一隻英雄，你就能看它的增幅、出裝和隊友搭配。分級門檻是固定勝率（OP 55%、T1 52%、T2 50%、T3 48%、T4 46%），改版後每級人數都會跟著變。",
        )),
        "zh-CN": ("关于这份英雄榜", (
            "这份英雄榜按大乱斗（Mayhem）实战胜率，把英雄分成 OP 到 T5 六级。胜率做了贝叶斯修正，场次少的英雄会被拉回平均，才不会没打几场就冲上榜首。",
            "点任意英雄，你就能看它的海克斯、出装和队友搭配。分级门槛是固定胜率（OP 55%、T1 52%、T2 50%、T3 48%、T4 46%），改版后每级人数都会跟着变。",
        )),
        "en": ("About this tier list", (
            "This tier list ranks every champion into six tiers, from OP to T5, using real ARAM Mayhem win rates. The numbers use Bayesian shrinkage, which pulls low-game champions back toward the average so a few lucky games will not put them at the top.",
            "Click any champion to check its augments, builds, and teammate stats. Tier cutoffs stay fixed (OP 55%, T1 52%, T2 50%, T3 48%, T4 46%), so the number of champions in each tier shifts every patch.",
        )),
    },
    "champions": {
        "zh": ("關於英雄列表", (
            "這頁把所有英雄在 ARAM 大亂鬥的勝率、場次和核心出裝排成一張表。你可以按職業篩選，也能直接搜中英文名字。",
            "想比同職業英雄很方便，一眼就知道誰勝率高、誰場次多。場次越多數字越穩，場次少的話當參考就好。點進英雄頁還能看完整增幅排名、出裝順序和文字分析。",
        )),
        "zh-CN": ("关于英雄列表", (
            "这页把所有英雄在大乱斗的胜率、场次和核心出装排成一张表。你可以按职业筛选，也能直接搜中英文名。",
            "想比同职业英雄很方便，一眼能看出谁胜率高、谁场次多。场次越多数据越稳，场次少的当参考就好。点进英雄页还能看完整海克斯排名、出装顺序和文字分析。",
        )),
        "en": ("About the champion table", (
            "This table shows every champion's ARAM Mayhem win rate, game count, and core items. You can filter by role or search by name.",
            "Use it to compare champions in the same role and see who wins more or gets picked more. More games mean steadier numbers, so treat low-game win rates as rough guides. Open any champion page for full augment rankings, build orders, and a written breakdown.",
        )),
    },
    "augments": {
        "zh": ("關於增幅勝率", (
            "這頁看的是每個增幅在 ARAM 大亂鬥的整體表現，包含拿到它的勝率、常不常被選，還有照稀有度分開的排名。",
            "增幅強不強很看英雄。整體勝率再高，給不搭的英雄拿還是會掉勝率。這頁幫你先看哪些增幅普遍好用，想挑特定英雄的增幅，去英雄頁看它對這隻英雄的勝率增減比較準。",
            "這裡只算當前版本的對局，場次太少的增幅不會上榜。",
        )),
        "zh-CN": ("关于海克斯胜率", (
            "这页看的是每个海克斯在大乱斗的整体表现，包括拿到它的胜率、选得多不多，还有按稀有度分开的排名。",
            "海克斯强不强很看英雄。整体胜率再高，给不搭的英雄拿照样会掉胜率。这页帮你先看哪些海克斯普遍好用，想挑具体英雄的海克斯，去英雄页看它对这英雄的胜率增减更准。",
            "这里只统计当前版本的对局，场次太少的海克斯不上榜。",
        )),
        "en": ("About augment win rates", (
            "This page shows how each augment does across ARAM Mayhem. You can check its win rate, how often players pick it, and its rank within each rarity.",
            "An augment's value depends a lot on your champion. Even a high win-rate pick can drag down the wrong kit. Use this list to spot generally strong augments, then open your champion's page to see how much each one actually lifts or drops its win rate.",
            "Only games from the current patch count here. Augments with too few games are left off the list.",
        )),
    },
    "pools": {
        "zh": ("關於增幅池", (
            "大亂鬥的三選一不會從全部增幅隨機抽。每隻英雄都有對應的幾個增幅池，池子的權重會決定哪些增幅比較常刷給你。",
            "這頁整理每個池有哪些增幅、權重多高，還有各英雄會從哪幾個池抽。知道自己的英雄抽得到什麼，你才好決定要不要為某個增幅重骰。池子定義來自遊戲檔案，實際出現率還會受伺服器規則影響，這裡列的是有機會刷到的增幅。",
        )),
        "zh-CN": ("关于海克斯池", (
            "大乱斗的三选一不会从全部海克斯里随机抽。每个英雄都有对应的几个海克斯池，池子的权重会决定哪些海克斯更容易刷给你。",
            "这页整理每个池有哪些海克斯、权重多高，还有各英雄会从哪些池抽。知道自己的英雄能抽到什么，你才好决定要不要为了某个海克斯重掷。池子定义来自游戏文件，实际出现率还会受服务器规则影响，这里列的是有机会刷到的海克斯。",
        )),
        "en": ("About augment pools", (
            "Your three augment choices in Mayhem do not come from the full list. Every champion pulls from a few augment pools, and each pool's weight controls what you are more likely to see.",
            "This page shows what is in each pool, how heavy its weight is, and which pools your champion uses. Knowing what you can roll helps you decide if a reroll is worth it. Pool data comes from the game files, while server rules also affect real offer rates, so treat this as what can show up.",
        )),
    },
    "draft": {
        "zh": ("關於選角分析", (
            "你把雙方各五隻英雄輸進去，就能預估兩邊勝率，也能看兩隊在傷害、坦度、控場、回復和清線的強弱。",
            "這組數字是拿實戰對局訓練的統計模型算的，看的是這套陣容過去平均表現如何。模型不知道玩家的熟練度，也不知道之後會選到什麼增幅，當成陣容的起跑點就好，別當成這場的勝負預測。",
            "看的時候重點放在差距和缺口。兩隊只差幾個百分點基本就是五五波，要是隊伍明顯缺哪一項，開打後靠增幅和出裝優先補齊。",
        )),
        "zh-CN": ("关于选角分析", (
            "你把双方各五个英雄输进去，就能预估两边胜率，也能看两队在伤害、坦度、控制、回复和清线的强弱。",
            "这组数据是拿实战对局训练的统计模型算的，看的是这套阵容过去平均表现如何。模型不知道玩家的熟练度，也不知道后面会选到什么海克斯，当成阵容的起跑点就好，别当成这把的胜负预测。",
            "看的时候重点放在差距和短板。两队只差几个百分点基本就是五五开，要是队伍明显缺哪一项，开打后靠海克斯和出装优先补上。",
        )),
        "en": ("About draft analysis", (
            "Put in all ten champions to estimate each team's win rate. You also see where both sides are strong or weak in damage, durability, crowd control, sustain, and wave clear.",
            "A statistical model trained on real matches makes this call based on how similar comps usually perform. It cannot see player skill or the augments you pick later, so treat it as a starting point for the draft instead of a final prediction.",
            "Focus on the gap and what your comp is missing. A gap of a few points is basically even. If your team clearly lacks something, use your augments and items to cover it.",
        )),
    },
    "game": {
        "zh": ("關於 Meta Pick", (
            "Meta Pick 是用本站資料做的小遊戲。每回合從隨機給的英雄裡挑一隻，五回合湊齊五人隊伍，最後由選角模型幫你的陣容打分數。",
            "玩起來就像平常打大亂鬥，能挑的英雄有限，你得在單隻強度跟團隊搭配之間拿捏。想拿高分靠的常是傷害類型、前排和控場都齊全，不用硬湊五隻最強英雄。成績可以自己選要不要上傳排行榜，暱稱別填真名。",
        )),
        "zh-CN": ("关于 Meta Pick", (
            "Meta Pick 是用本站数据做的小游戏。每回合从随机给的英雄里挑一个，五回合凑齐五人队伍，最后由选角模型给你的阵容打分。",
            "玩起来就像平时打大乱斗，能挑的英雄有限，你得在单个强度和团队搭配之间拿捏。想拿高分靠的常是伤害类型、前排和控制都齐全，不用硬凑五个最强英雄。成绩可以自己选要不要上传排行榜，昵称别填真名。",
        )),
        "en": ("About Meta Pick", (
            "Meta Pick is a mini-game built with this site's data. Pick one champion from a random set over five rounds to build a five-man team, and the draft model scores your comp.",
            "It feels like a real ARAM lobby where your choices are limited and you have to balance strong solo picks with a working comp. High scores usually come from mixed damage, a front line, and crowd control rather than five top-tier champions. You can choose to upload your score to the leaderboard, just do not use your real name as a nickname.",
        )),
    },
    "changes": {
        "zh": ("關於版本變動", (
            "這頁比對當前版本和上一版，看哪些英雄、增幅和裝備勝率變最多，也整理了官方公告的增幅改動。",
            "勝率跳動有時是真的平衡改動，有時只是正常起伏。每筆都附了兩個版本的場次讓你對照，場次少時漲跌看起來很誇張，其實不太準。剛改版前幾天對局還不多，等資料累積多了，數字還會慢慢穩下來。",
        )),
        "zh-CN": ("关于版本变动", (
            "这页对比当前版本和上一版，看哪些英雄、海克斯和装备胜率变最多，也整理了官方公告的海克斯改动。",
            "胜率跳动有时是真的平衡改动，有时只是正常起伏。每条都附了两个版本的场次让你对照，场次少时涨跌看着很夸张，其实不太准。刚改版前几天对局还不多，等数据积累多了，数字还会慢慢稳下来。",
        )),
        "en": ("About patch changes", (
            "This page compares the current patch with the last one. See which champions, augments, and items moved most in win rate, plus the official augment changes.",
            "A win-rate jump can come from a real balance change or just normal noise. Every row lists game counts for both patches so you can tell the difference. Big swings on low games look wild, though they are not very reliable. Match volume is thin right after a patch drops, so numbers settle as more games come in.",
        )),
    },
}

ROUTE_INTRO_RE = re.compile(
    r"<!-- arammeta:route-intro -->.*?<!-- /arammeta:route-intro -->", flags=re.S,
)


def route_intro_key(canonical_path: str) -> str:
    """Route-intro key for a canonical path; '' when the route has none."""
    parts = [part for part in canonical_path.strip("/").split("/") if part]
    if parts and parts[0] in {"en", "zh-cn"}:
        parts = parts[1:]
    if not parts:
        return "home"
    if parts == ["augments", "pools"]:
        return "pools"
    return parts[0] if len(parts) == 1 and parts[0] in ROUTE_INTROS else ""


def route_intro_html(*, active: str = "home", html_lang: str = "zh-Hant") -> str:
    """All route intros; only ``active`` is visible, text in ``html_lang``."""
    esc = html.escape
    shown = {"en": "en", "zh-Hans": "zh-CN"}.get(html_lang, "zh")

    def node(tag: str, key: str, index: int) -> str:
        def text(lang: str) -> str:
            heading, paragraphs = ROUTE_INTROS[key][lang]
            return heading if index < 0 else paragraphs[index]
        return (
            f"<{tag} data-i18n-zh='{esc(text('zh'), quote=True)}' "
            f"data-i18n-zh-cn='{esc(text('zh-CN'), quote=True)}' "
            f"data-i18n-en='{esc(text('en'), quote=True)}'>{esc(text(shown))}</{tag}>"
        )

    sections = []
    for key, copy in ROUTE_INTROS.items():
        # Collapsed by default: the copy stays in the markup for crawlers but
        # only the heading line takes space under the page.
        body = (
            "<details><summary>" + node("h2", key, -1) + "</summary>"
            + "".join(node("p", key, index) for index in range(len(copy["zh"][1])))
            + "</details>"
        )
        hidden = "" if key == active else " hidden"
        sections.append(f"<section data-route-intro='{key}'{hidden}>{body}</section>")
    return (
        "<!-- arammeta:route-intro --><aside class='route-intro' id='route-intro'>"
        + "".join(sections)
        + "</aside><!-- /arammeta:route-intro -->"
    )


def localize_route_intro(html_src: str, *, canonical_path: str, html_lang: str) -> str:
    """Re-render the shell's intro block for one route and language."""
    return ROUTE_INTRO_RE.sub(
        lambda _match: route_intro_html(
            active=route_intro_key(canonical_path), html_lang=html_lang,
        ),
        html_src, count=1,
    )
