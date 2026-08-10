from aram_nn.classic_positions import (
    base_classic_item_id,
    infer_team_positions,
)


def participant(*, lane="", role="", spells=(), items=(), lane_cs=80, neutral=0):
    return {
        "lane": lane,
        "role": role,
        "spells": list(spells),
        "items": list(items),
        "stats": {
            "total_minions_killed": lane_cs,
            "neutral_minions_killed": neutral,
            "wards_placed": 1,
        },
    }


def test_jade_item_id_is_normalized():
    assert base_classic_item_id(771039) == 1039
    assert base_classic_item_id(773006) == 3006
    assert base_classic_item_id(3006) == 3006


def test_standard_team_gets_unique_confident_positions():
    team = [
        participant(lane="TOP", role="SOLO"),
        participant(lane="JUNGLE", spells=(711,), items=(771039,), neutral=80),
        participant(lane="MIDDLE", role="SOLO"),
        participant(lane="BOTTOM", role="CARRY", lane_cs=100),
        participant(lane="BOTTOM", role="SUPPORT", items=(772049,), lane_cs=20),
    ]
    inferred = infer_team_positions(team)
    assert [row.position for row in inferred] == [
        "TOP", "JUNGLE", "MIDDLE", "BOTTOM", "SUPPORT"
    ]
    assert len({row.position for row in inferred}) == 5
    assert sum(row.stat_eligible for row in inferred) == 5


def test_constraint_only_assignment_is_not_called_known():
    inferred = infer_team_positions([participant() for _ in range(5)])
    assert len(inferred) == 5
    assert len({row.position for row in inferred}) == 5
    assert not any(row.stat_eligible for row in inferred)


def test_four_known_leaves_a_derived_fifth():
    team = [
        participant(lane="TOP", role="SOLO"),
        participant(lane="JUNGLE", spells=(711,), neutral=80),
        participant(lane="MIDDLE", role="SOLO"),
        participant(lane="BOTTOM", role="CARRY", lane_cs=100),
        participant(lane_cs=20),
    ]
    inferred = infer_team_positions(team)
    assert inferred[-1].position == "SUPPORT"
    assert inferred[-1].confidence == "DERIVED"
