"""Logic tests, with no call to the osu! API.

Run with: python tests/test_logic.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ppfarmer.api import PAGE_SIZE
from ppfarmer.crawler import band_for, reachable_share, window_for_players
from ppfarmer.discovery import CountryScanner
from ppfarmer.ppcalc import median_pp, pp_gain, weighted_total
from ppfarmer.rank import parse_duration, rank_beatmaps, speed_multiplier
from ppfarmer.store import (MAX_SCORE_AGE_YEARS, Store, canonical_mods,
                            difficulty_mods, is_full_combo, mods_allowed,
                            normalize_mods, parse_mod_filter, score_age_years,
                            split_mods, user_score_index)


class FakeClient:
    """Simulated country ranking: the player at national rank n has global
    rank ~ n/share. Lets the binary search be tested without network."""

    def __init__(self, share, total_players):
        self.share, self.total = share, total_players
        self.calls = 0

    def rankings_page(self, mode, country, page):
        self.calls += 1
        start = (page - 1) * PAGE_SIZE + 1
        out = []
        for i in range(start, start + PAGE_SIZE):
            if i > self.total:
                break
            out.append({
                "global_rank": int(i / self.share),
                "pp": 10000 - i,
                "user": {"id": i, "username": f"p{i}", "country_code": country},
            })
        return out


def test_discovery():
    fake = FakeClient(share=0.05, total_players=10000)
    band = CountryScanner(fake, "XX").collect(70000, 72000)
    ranks = [p["global_rank"] for p in band]
    assert ranks, "the band must not be empty"
    assert all(70000 <= r <= 72000 for r in ranks), f"ranks outside the band: {ranks[:5]}"
    assert ranks == sorted(ranks), "ranks must be ascending"
    assert fake.calls < 20, f"too many requests: {fake.calls}"
    print(f"[1] binary search: {len(band)} players in {fake.calls} requests "
          f"(ranks {ranks[0]}..{ranks[-1]})")

    shallow = FakeClient(share=0.50, total_players=10000)
    assert CountryScanner(shallow, "BIG").collect(70000, 72000) == [], \
        "a country too populous to reach the band must return an empty list"
    print(f"[2] out-of-reach country skipped ({shallow.calls} requests)")


def test_mods():
    assert normalize_mods(None) == "NM"
    assert normalize_mods(["HD", "DT"]) == "DTHD"
    assert normalize_mods([{"acronym": "DT"}, {"acronym": "HD"}]) == "DTHD"

    # CL (Classic) marks a stable-scoring score and must survive: the same
    # SS is 254pp with it, 268pp without. Conflating it with a lazer score
    # inflated the ceiling reported.
    assert normalize_mods([{"acronym": "CL"}]) == "CL"
    assert normalize_mods([{"acronym": "CL"}, {"acronym": "HD"}]) == "CLHD"

    assert split_mods("CLDTHD") == ["CL", "DT", "HD"]
    assert split_mods("NM") == []
    # CL leaves the grouping: the same play is the same play, whichever
    # version submitted it.
    assert difficulty_mods("CL") == "NM"
    assert difficulty_mods("CLDTHD") == "DTHD"
    assert difficulty_mods("NM") == "NM"

    # "HD" then "TD" gives "HDTD": a substring test would see a DT there.
    assert speed_multiplier("HDTD") == 1.0, "false DoubleTime detected"
    assert speed_multiplier("CLDT") == 1.5
    assert speed_multiplier("CL") == 1.0
    print("[3] mods: CL kept in storage, left out of grouping, "
          "false DT rejected")


def _score(bid, mods, pp=100.0):
    return {"beatmap": {"id": bid, "version": f"v{bid}", "difficulty_rating": 5.0,
                        "bpm": 180, "total_length": 100, "status": "ranked"},
            "beatmapset": {"id": bid, "artist": "A", "title": f"map{bid}"},
            "pp": pp, "mods": mods, "accuracy": 0.98, "rank": "S"}


def test_aggregation():
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": 1, "username": "a", "country": "FR", "global_rank": 70500, "pp": 3000},
            {"user_id": 2, "username": "b", "country": "FR", "global_rank": 71500, "pp": 2900},
            {"user_id": 3, "username": "c", "country": "US", "global_rank": 99999, "pp": 1000},
        ])
        # Player 1 has map 10 twice in NM: it counts once in that group.
        # Player 2 has it in DT: that is another row.
        store.save_scores(1, [_score(10, []), _score(10, []), _score(20, ["DT"])])
        store.save_scores(2, [_score(10, []), _score(30, [])])
        # Player 3 is outside the band: their scores must never count.
        store.save_scores(3, [_score(99, []), _score(99, []), _score(10, [])])

        maps, panel = rank_beatmaps(store, 70000, 72000, top_n=10, limit=10, min_players=1)
        par_cle = {(m["beatmap_id"], m["mods"]): m for m in maps}
        assert panel == 2, f"panel should be 2, is {panel}"
        assert par_cle[(10, "NM")]["players"] == 2, (
            f"map10 NM = {par_cle[(10, 'NM')]['players']} players instead of 2")
        assert 99 not in {m["beatmap_id"] for m in maps},             "a map from an out-of-band player must not appear"
        print(f"[4] aggregation: map10 NM = {par_cle[(10, 'NM')]['players']} players "
              f"(per-group dedupe OK), panel={panel}")

        maps2, _ = rank_beatmaps(store, 70000, 72000, top_n=1, limit=10, min_players=1)
        assert {m["beatmap_id"] for m in maps2} == {10}
        print("[5] top_n respects the top depth")

        maps3, _ = rank_beatmaps(store, 70000, 72000, top_n=10, exclude={10},
                                 limit=10, min_players=1)
        assert 10 not in {m["beatmap_id"] for m in maps3}
        print("[6] maps already in your top are excluded")


def test_one_row_per_mod_combo():
    """A map played with several combinations yields several rows.

    Each carries its own headcount, typical pp and mean: the same map in NM
    and in HR is not farmed the same way.
    """
    db = os.path.join(tempfile.mkdtemp(), "combo.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 31)
        ])
        for i in range(1, 31):
            if i <= 20:
                store.save_scores(i, [_score(1, [{"acronym": "CL"}], pp=240.0)])
            else:
                store.save_scores(i, [_score(1, [{"acronym": "CL"},
                                                {"acronym": "HR"}], pp=310.0)])

        maps, _ = rank_beatmaps(store, 70000, 72000, min_players=5)
        by_mods = {m["mods"]: m for m in maps}
        assert set(by_mods) == {"NM", "HR"}, (
            f"two rows expected, got {sorted(by_mods)}")
        assert by_mods["NM"]["players"] == 20
        assert by_mods["HR"]["players"] == 10
        assert by_mods["NM"]["typical_pp"] == 240.0
        assert by_mods["HR"]["typical_pp"] == 310.0
        assert by_mods["NM"]["beatmap_id"] == by_mods["HR"]["beatmap_id"]
        assert by_mods["NM"]["row_id"] != by_mods["HR"]["row_id"]

        # The threshold applies per combination, not to the whole map.
        strictes, _ = rank_beatmaps(store, 70000, 72000, min_players=15)
        assert {m["mods"] for m in strictes} == {"NM"},             "only the sufficiently carried combination must remain"
        print(f"[25] one row per combination: NM {by_mods['NM']['players']} players "
              f"at {by_mods['NM']['typical_pp']:.0f}pp, HR {by_mods['HR']['players']} "
              f"at {by_mods['HR']['typical_pp']:.0f}pp")


def test_ppcalc():
    user = [100.0] * 100
    expected = 100 * (1 - 0.95 ** 100) / 0.05  # closed geometric sum
    assert abs(weighted_total(user) - expected) < 1e-6
    print(f"[7] weighted total = {weighted_total(user):.1f}pp (checked against the formula)")

    # A score below the 100th earns nothing: that is the whole point.
    assert pp_gain(user, 50.0) == 0.0, "a score too low must earn nothing"
    assert pp_gain(user, 100.0) < 1.0
    gains = [pp_gain(user, p) for p in [100, 150, 200, 250, 300]]
    assert gains == sorted(gains), f"gain is not monotonic: {gains}"
    print(f"[8] monotonic gains {[round(g, 1) for g in gains]}, "
          f"score too low -> +0pp")

    assert pp_gain([200.0, 150.0], 180.0) > 100, "with few scores, a good one earns a lot"
    assert median_pp([10, 20, 30]) == 20
    assert median_pp([None, None]) is None
    assert pp_gain(user, None) == 0.0
    print("[9] median and missing values handled")


def test_popularity_vs_gain():
    """A very popular but too weak map must earn nothing."""
    db = os.path.join(tempfile.mkdtemp(), "g.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 11)
        ])
        for i in range(1, 11):
            scores = [_score(1, [], pp=120.0)]          # popular but weak
            if i <= 3:
                scores.append(_score(2, ["HR"], pp=320.0))  # rare but strong
            store.save_scores(i, scores)

        my_pps = [260 - i * 1.1 for i in range(100)]   # 100th score ~= 151pp
        by_pop, _ = rank_beatmaps(store, 70000, 72000, user_pps=my_pps,
                                  sort="players", min_players=1)
        by_gain, _ = rank_beatmaps(store, 70000, 72000, user_pps=my_pps,
                                   sort="gain", min_players=1)

        assert by_pop[0]["beatmap_id"] == 1, "the most popular must come first by popularity"
        assert by_pop[0]["gain"] == 0.0, "but it must earn no pp at all"
        assert by_gain[0]["beatmap_id"] == 2, "sorting by gain must surface the profitable map"
        assert by_gain[0]["gain"] > 50
        print(f"[10] popular map (10 players) -> +{by_pop[0]['gain']:.0f}pp | "
              f"rare map (3 players) -> +{by_gain[0]['gain']:.0f}pp")


def test_min_players():
    """The support threshold drops maps seen by too few players.

    On real data, sorting by gain without a threshold surfaces lone outliers:
    a map present in a single player's top shows a median equal to that one
    score, which predicts nothing.
    """
    db = os.path.join(tempfile.mkdtemp(), "m.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 11)
        ])
        for i in range(1, 11):
            scores = [_score(1, [], pp=200.0)]              # 10 players
            if i == 1:
                scores.append(_score(2, ["HR"], pp=900.0))  # lone outlier
            store.save_scores(i, scores)

        my_pps = [260 - i * 1.1 for i in range(100)]
        loose, _ = rank_beatmaps(store, 70000, 72000, user_pps=my_pps,
                                 sort="gain", min_players=1)
        assert loose[0]["beatmap_id"] == 2, "without a threshold, the lone outlier wins"

        strict, _ = rank_beatmaps(store, 70000, 72000, user_pps=my_pps,
                                  sort="gain", min_players=5)
        ids = {m["beatmap_id"] for m in strict}
        assert ids == {1}, f"with a threshold, only the shared map remains, got {ids}"
        print("[11] support threshold: lone outlier (1 player) dropped, "
              "shared map (10 players) kept")


def test_gain_on_replayed_map():
    """On a map already played, the gain is a replacement, not an addition.

    osu! only keeps the best score per map. Treating a replay as an addition
    would overstate the gain, all the more so when the old score is good.
    """
    user = [300.0 - i for i in range(100)]      # 300 down to 201pp
    existing = 250.0                             # score already on the map
    target = 290.0

    addition = pp_gain(user, target)
    replacement = pp_gain(user, target, replacing=existing)
    assert replacement < addition, (
        f"the replacement ({replacement:.1f}) must earn less than "
        f"the addition ({addition:.1f})")

    # Replaying worse than your current score earns nothing.
    assert pp_gain(user, 240.0, replacing=existing) == 0.0
    assert pp_gain(user, existing, replacing=existing) == 0.0
    print(f"[12] map already played: naive addition +{addition:.1f}pp vs real "
          f"replacement +{replacement:.1f}pp; replaying worse -> +0pp")


def test_played_flags():
    """Maps in the player's top are flagged and carry their score."""
    db = os.path.join(tempfile.mkdtemp(), "p.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 11)
        ])
        for i in range(1, 11):
            store.save_scores(i, [_score(1, [], pp=300.0), _score(2, [], pp=300.0)])

        my_top = [{"beatmap": {"id": 1}, "pp": 250.0, "accuracy": 0.9712,
                   "legacy_perfect": True, "rank": "S", "mods": ["DT"]}]
        idx = user_score_index(my_top)
        assert idx[1]["fc"] is True, "legacy_perfect must be read as an FC"
        assert idx[1]["mods"] == "DT"

        # Non-flat top: on a flat top, inserting a score ejects one of the
        # same value as the replaced one, and both computations coincide.
        my_pps = [300.0 - i for i in range(100)]   # 300 down to 201pp, contains 250
        maps, _ = rank_beatmaps(store, 70000, 72000, user_pps=my_pps,
                                min_players=1, user_scores=idx)
        by_id = {m["beatmap_id"]: m for m in maps}
        assert by_id[1]["played"] is True and by_id[2]["played"] is False
        assert by_id[1]["my_accuracy"] == 0.9712
        assert by_id[1]["my_fc"] is True
        # The played map earns less than the unknown one, at equal typical pp.
        assert by_id[1]["gain"] < by_id[2]["gain"]
        print(f"[13] played map flagged (97.12%, FC) and gain reduced "
              f"(+{by_id[1]['gain']:.1f} vs +{by_id[2]['gain']:.1f}pp)")


def test_duration_and_efficiency():
    """The real length depends on the speed mods, and the yield follows."""
    assert speed_multiplier("NM") == 1.0
    assert speed_multiplier("DT") == 1.5
    assert speed_multiplier("DTHD") == 1.5
    assert speed_multiplier("NC") == 1.5
    assert speed_multiplier("HT") == 0.75

    db = os.path.join(tempfile.mkdtemp(), "d.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 11)
        ])
        for i in range(1, 11):
            # Same pp, but one is long in nomod and the other short in DT.
            long_map = _score(1, [], pp=300.0)
            long_map["beatmap"]["total_length"] = 240   # 4m00
            short_map = _score(2, ["DT"], pp=300.0)
            short_map["beatmap"]["total_length"] = 90   # 1m30 -> 1m00 with DT
            store.save_scores(i, [long_map, short_map])

        maps, _ = rank_beatmaps(store, 70000, 72000, min_players=1,
                                sort="efficiency")
        by_id = {m["beatmap_id"]: m for m in maps}
        assert by_id[1]["played_length"] == 240, "nomod: length unchanged"
        assert by_id[2]["played_length"] == 60, "DT: 90s must become 60s"
        assert abs(by_id[1]["pp_per_min"] - 75.0) < 0.1     # 300pp / 4min
        assert abs(by_id[2]["pp_per_min"] - 300.0) < 0.1    # 300pp / 1min
        assert maps[0]["beatmap_id"] == 2, "the most efficient must come first"
        print(f"[14] length corrected by mods: 4m00 -> {by_id[1]['pp_per_min']:.0f}pp/min "
              f"vs 1m30 in DT -> 1m00 -> {by_id[2]['pp_per_min']:.0f}pp/min")


def test_duration_filter():
    """The length filter applies to time actually played, mods included."""
    for text, expected in [("90", 90), ("90s", 90), ("1m30", 90), ("1m", 60),
                           ("1:30", 90), ("2m05", 125), (None, None), (240, 240)]:
        assert parse_duration(text) == expected, f"{text!r} parsed wrong"
    for bad in ("abc", "1h", "m"):
        try:
            parse_duration(bad)
            raise AssertionError(f"{bad!r} should have been rejected")
        except ValueError:
            pass

    db = os.path.join(tempfile.mkdtemp(), "l.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 11)
        ])
        for i in range(1, 11):
            short_map = _score(1, [], pp=300.0)
            short_map["beatmap"]["total_length"] = 100      # 1m40 nomod
            long_map = _score(2, [], pp=300.0)
            long_map["beatmap"]["total_length"] = 300       # 5m00 nomod
            # 150s in DT only cost 100s of play: it must pass a two-minute
            # filter, unlike its displayed length.
            fast_map = _score(3, ["DT"], pp=300.0)
            fast_map["beatmap"]["total_length"] = 150
            store.save_scores(i, [short_map, long_map, fast_map])

        short_rows, _ = rank_beatmaps(store, 70000, 72000, min_players=1,
                                      max_length=parse_duration("2m"))
        ids = {m["beatmap_id"] for m in short_rows}
        assert ids == {1, 3}, f"expected maps 1 and 3, got {ids}"

        long_rows, _ = rank_beatmaps(store, 70000, 72000, min_players=1,
                                     min_length=parse_duration("4m"))
        assert {m["beatmap_id"] for m in long_rows} == {2}

        window, _ = rank_beatmaps(store, 70000, 72000, min_players=1,
                                  min_length=90, max_length=110)
        assert {m["beatmap_id"] for m in window} == {1, 3}
        print("[15] length filter: 2m30 in DT (=1m40 played) passes a 2m "
              "filter, the 5m map is dropped")


def test_median_vs_mean():
    """The median resists an extreme score, the mean does not."""
    db = os.path.join(tempfile.mkdtemp(), "x.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 11)
        ])
        for i in range(1, 11):
            # Map 1: spread-out scores, one player reaching 400.
            store.save_scores(i, [_score(1, [], pp=400.0 if i == 1 else 250.0),
                                  _score(2, [], pp=260.0)])

        maps, _ = rank_beatmaps(store, 70000, 72000, min_players=1)
        by_id = {m["beatmap_id"]: m for m in maps}
        assert by_id[1]["typical_pp"] == 250.0, "the median must not follow the outlier"
        assert abs(by_id[1]["mean_pp"] - 265.0) < 0.1, (
            f"the mean does follow it: {by_id[1]['mean_pp']}")
        assert by_id[2]["typical_pp"] == 260.0
        print(f"[16] median {by_id[1]['typical_pp']:.0f}pp against mean "
              f"{by_id[1]['mean_pp']:.0f}pp: the outlier only moves the latter")


def test_band_above_only():
    """The band only keeps ranks better than the player's own."""
    low, high = band_for(71322, 1000)
    assert (low, high) == (70322, 71321), f"got {(low, high)}"
    assert high < 71322, "the player must not be inside their own band"
    assert high - low + 1 == 1000, "the band must span exactly 1000 ranks"

    # Near the top, the lower bound clamps to 1 without going negative.
    assert band_for(300, 1000) == (1, 299)
    assert band_for(1, 1000) == (1, 1), "rank 1 must not produce a zero bound"
    print(f"[17] band above only: #{low}-#{high} for a player at #71322")


def test_score_age_filter():
    """Scores older than MAX_SCORE_AGE_YEARS are not kept."""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    recent = (now - dt.timedelta(days=200)).isoformat().replace("+00:00", "Z")
    old = (now - dt.timedelta(days=int(365.25 * 12))).isoformat().replace("+00:00", "Z")
    edge = (now - dt.timedelta(days=int(365.25 * 9))).isoformat().replace("+00:00", "Z")

    assert abs(score_age_years(recent) - 0.55) < 0.05
    assert score_age_years(old) > MAX_SCORE_AGE_YEARS
    assert score_age_years(None) is None
    assert score_age_years("not a date") is None, "an unreadable date must not break"

    db = os.path.join(tempfile.mkdtemp(), "a.db")
    with Store(db) as store:
        store.upsert_players([{"user_id": 1, "username": "a", "country": "FR",
                               "global_rank": 71000, "pp": 3000}])
        s1 = _score(1, [], pp=300.0); s1["ended_at"] = recent
        s2 = _score(2, [], pp=300.0); s2["ended_at"] = old
        s3 = _score(3, [], pp=300.0); s3["ended_at"] = edge
        s4 = _score(4, [], pp=300.0)                 # no date: kept
        store.save_scores(1, [s1, s2, s3, s4])

        rows = store.conn.execute(
            "SELECT position, beatmap_id FROM scores WHERE user_id=1 ORDER BY position"
        ).fetchall()
        kept = {r["beatmap_id"] for r in rows}
        assert kept == {1, 3, 4}, f"map 2 (12 years) should have been dropped, got {kept}"
        # The position of a kept score must not have been renumbered.
        pos = {r["beatmap_id"]: r["position"] for r in rows}
        assert pos[3] == 3, f"map 3 must stay at position 3, not {pos[3]}"
        assert pos[4] == 4, "no promotion must fill the hole"
        print(f"[18] 12-year-old score dropped, positions preserved "
              f"(map3 stays at {pos[3]}, map4 at {pos[4]})")


def test_median_age():
    """The median score age tells a fresh map from a relic."""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    def years_ago(years):
        return (now - dt.timedelta(days=int(365.25 * years))).isoformat().replace("+00:00", "Z")

    db = os.path.join(tempfile.mkdtemp(), "ma.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 6)
        ])
        for i in range(1, 6):
            fresh = _score(1, [], pp=300.0); fresh["ended_at"] = years_ago(1)
            relic = _score(2, [], pp=300.0); relic["ended_at"] = years_ago(8)
            store.save_scores(i, [fresh, relic])

        maps, _ = rank_beatmaps(store, 70000, 72000, min_players=1)
        by_id = {m["beatmap_id"]: m for m in maps}
        assert abs(by_id[1]["median_age"] - 1) < 0.1, by_id[1]["median_age"]
        assert abs(by_id[2]["median_age"] - 8) < 0.1, by_id[2]["median_age"]
        print(f"[19] median age: fresh map {by_id[1]['median_age']:.1f}y, "
              f"relic {by_id[2]['median_age']:.1f}y")


def test_pp_grouped_by_mods():
    """Each combination has its own figures.

    Real case: on [Shiawase!!], nomod scores top out around 254pp while HR
    ones reach 344pp. Mixing them reported an HR maximum next to a row
    labelled NM.
    """
    db = os.path.join(tempfile.mkdtemp(), "mg.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 21)
        ])
        for i in range(1, 21):
            if i <= 16:                                   # 80% stable nomod
                sc = _score(1, [{"acronym": "CL"}], pp=250.0 + (i % 3))
            else:                                         # 20% HR, far higher
                sc = _score(1, [{"acronym": "CL"}, {"acronym": "HR"}], pp=340.0)
            store.save_scores(i, [sc])

        maps, _ = rank_beatmaps(store, 70000, 72000, min_players=1)
        by_mods = {m["mods"]: m for m in maps}
        assert set(by_mods) == {"NM", "HR"}, (
            f"each combination must have its row, got {sorted(by_mods)}")
        assert by_mods["NM"]["typical_pp"] <= 253, (
            f"NM pp must not borrow from HR, "
            f"got {by_mods['NM']['typical_pp']}")
        assert by_mods["HR"]["typical_pp"] == 340.0
        assert by_mods["NM"]["players"] == 16 and by_mods["HR"]["players"] == 4
        print(f"[20] each combination has its own figures: "
              f"{by_mods['NM']['typical_pp']:.0f}pp in NM, "
              f"{by_mods['HR']['typical_pp']:.0f}pp in HR")


def test_scoring_version_grouping():
    """The same play counts once, whether it came from stable or lazer."""
    db = os.path.join(tempfile.mkdtemp(), "sv.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 21)
        ])
        for i in range(1, 21):
            if i <= 18:                                   # stable: 254pp
                sc = _score(1, [{"acronym": "CL"}], pp=254.0)
            else:                                         # lazer: 268pp
                sc = _score(1, [], pp=268.0)
            store.save_scores(i, [sc])

        # All 20 scores form a single NM group: the sample stays whole.
        lazer, _ = rank_beatmaps(store, 70000, 72000, min_players=1)
        assert lazer[0]["mods"] == "NM", "CL must not split the group"
        assert lazer[0]["players"] == 20, (
            f"all 20 players must count together, got {lazer[0]['players']}")
        print(f"[21] single group of {lazer[0]['players']} players: "
              f"CL does not split the sample")


def test_mod_filter():
    """Filter by combination, with NC folded into DT and a catch-all."""
    # NC is DT with a different sound: same pp, so same group.
    assert difficulty_mods("CLHDNC") == "DTHD", "NC must become DT"
    assert difficulty_mods("CLNC") == "DT"
    assert difficulty_mods("CLDC") == "HT"

    # Free spelling is normalised: the mod order need not be known.
    assert canonical_mods("hddt") == "DTHD"
    assert canonical_mods("DTHD") == "DTHD"
    assert canonical_mods("HDDT") == "DTHD"

    wanted = parse_mod_filter("HD,HR,DT,HDHR,HDDT")
    assert wanted == {"HD", "HR", "DT", "HDHR", "DTHD"}
    assert mods_allowed("HD", wanted) and mods_allowed("DTHD", wanted)
    assert not mods_allowed("NM", wanted), "NM is not in the requested list"
    assert not mods_allowed("DTHDHR", wanted), "an exotic combination is dropped"
    assert mods_allowed("NM", None), "with no filter, everything passes"

    other = parse_mod_filter("other")
    assert mods_allowed("DTHDHR", other), "the catch-all takes the exotic ones"
    assert not mods_allowed("HD", other), "but not the common combinations"

    db = os.path.join(tempfile.mkdtemp(), "mf.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 6)
        ])
        for i in range(1, 6):
            store.save_scores(i, [
                _score(1, [{"acronym": "CL"}], pp=300.0),                       # NM
                _score(2, [{"acronym": "CL"}, {"acronym": "HD"}], pp=300.0),    # HD
                _score(3, [{"acronym": "CL"}, {"acronym": "NC"}], pp=300.0),    # NC -> DT
                _score(4, [{"acronym": "CL"}, {"acronym": "SD"}], pp=300.0),    # exotic
            ])

        maps, _ = rank_beatmaps(store, 70000, 72000, min_players=1,
                                mods_filter=parse_mod_filter("HD,DT"))
        ids = {m["beatmap_id"] for m in maps}
        assert ids == {2, 3}, f"expected maps HD and NC(=DT), got {ids}"
        by_id = {m["beatmap_id"]: m["mods"] for m in maps}
        assert by_id[3] == "DT", f"NC must show as DT, got {by_id[3]}"

        exotic, _ = rank_beatmaps(store, 70000, 72000, min_players=1,
                                  mods_filter=parse_mod_filter("other"))
        assert {m["beatmap_id"] for m in exotic} == {4}
        print("[24] mod filter: NC grouped with DT, catch-all isolates the "
              "rare combinations")


def test_page_javascript_parses():
    """The page script must parse.

    A syntax error does not stop the page from being served: it just leaves
    "Loading..." on screen forever, with nothing in the server logs. This test
    catches that before the user does.
    """
    import pathlib
    import re
    import shutil
    import subprocess
    import tempfile as tf

    page = (pathlib.Path(__file__).resolve().parent.parent
            / "ppfarmer" / "static" / "index.html")
    src = page.read_text(encoding="utf-8")

    scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
    assert scripts, "the page must contain a script"

    # Every id queried must exist in the markup, otherwise $(...) returns
    # null and rendering breaks on first access.
    declared = set(re.findall(r'id="([^"]+)"', src))
    used = set(re.findall(r'\$\("([^"]+)"\)', src))
    # Created on the fly by render() rather than declared in the markup.
    missing = used - declared - {"goCrawl", "changeProfile", "resetFilters"}
    assert not missing, f"ids absent from the HTML: {missing}"

    node = shutil.which("node")
    if node is None:
        print("[28] node missing: only ids were checked")
        return
    path = os.path.join(tf.mkdtemp(), "page.js")
    pathlib.Path(path).write_text(scripts[0], encoding="utf-8")
    res = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert res.returncode == 0, f"invalid JavaScript:{chr(10)}{res.stderr}"
    print(f"[28] page script valid ({len(scripts[0])} characters) "
          f"and {len(used)} ids resolved")


def test_scoring_filter():
    """The game version can be filtered, and the split stays visible without one.

    CL (Classic) marks a score submitted from stable. It is kept out of the
    grouping so samples are not split, but the data is preserved: it can be
    read or restricted to.
    """
    db = os.path.join(tempfile.mkdtemp(), "sc.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 21)
        ])
        for i in range(1, 21):
            if i <= 15:                                   # stable
                store.save_scores(i, [_score(1, [{"acronym": "CL"}], pp=254.0)])
            else:                                         # lazer
                store.save_scores(i, [_score(1, [], pp=268.0)])

        both, _ = rank_beatmaps(store, 70000, 72000, min_players=1)
        assert len(both) == 1, "both versions stay a single NM group"
        m = both[0]
        assert m["players"] == 20
        assert m["stable_players"] == 15 and m["lazer_players"] == 5, (
            f"expected a 15/5 split, got "
            f"{m['stable_players']}/{m['lazer_players']}")

        st, _ = rank_beatmaps(store, 70000, 72000, min_players=1, scoring="stable")
        assert st[0]["players"] == 15 and st[0]["typical_pp"] == 254.0
        assert st[0]["lazer_players"] == 0

        lz, _ = rank_beatmaps(store, 70000, 72000, min_players=1, scoring="lazer")
        assert lz[0]["players"] == 5 and lz[0]["typical_pp"] == 268.0
        assert lz[0]["stable_players"] == 0

        # The threshold applies after the filter: restricting can empty a row.
        empty, _ = rank_beatmaps(store, 70000, 72000, min_players=10, scoring="lazer")
        assert empty == [], "five lazer players do not pass a threshold of ten"
        print(f"[29] game version: {m['stable_players']} stable at "
              f"{st[0]['typical_pp']:.0f}pp / {m['lazer_players']} lazer at "
              f"{lz[0]['typical_pp']:.0f}pp, grouped together by default")


def test_crawl_reports_skipped():
    """A re-run crawl must report how many players it skipped.

    Without that count, re-running a crawl right after the previous one looks
    like it did nothing: cache freshness skips everyone in silence.
    """
    from ppfarmer.crawler import crawl

    class FakeCrawlClient:
        """No network: discovery returns the already-known players."""
        def __init__(self, players):
            self.players = players
            self.tops = 0
        def country_stats(self, mode="osu", pages=1):
            return [{"code": "XX", "active_users": 1000}]
        def rankings_page(self, mode, country, page):
            if page != 1:
                return []
            return [{"global_rank": p["global_rank"], "pp": 3000,
                     "user": {"id": p["user_id"], "username": p["username"],
                              "country_code": "XX"}}
                    for p in self.players]
        def user_best(self, uid, mode="osu", limit=50):
            self.tops += 1
            return [_score(1, [{"acronym": "CL"}], pp=250.0)]

    db = os.path.join(tempfile.mkdtemp(), "sk.db")
    players_seed = [{"user_id": i, "username": f"p{i}", "country": "XX",
                "global_rank": 70000 + i, "pp": 3000} for i in range(1, 11)]
    with Store(db) as store:
        client = FakeCrawlClient(players_seed)

        first = crawl(client, store, 70000, 72000, top_n=10, max_age_days=14)
        assert first["crawled"] == 10, f"the first crawl fetches everything: {first}"
        assert first["skipped"] == 0

        # Immediately after, everything is fresh: nothing to redo.
        again = crawl(client, store, 70000, 72000, top_n=10, max_age_days=14)
        assert again["crawled"] == 0, "nothing must be refetched"
        assert again["skipped"] == 10, (
            f"all ten players must be reported as skipped, got "
            f"{again['skipped']}")

        # max_age = 0 forces a full refetch.
        forced = crawl(client, store, 70000, 72000, top_n=10, max_age_days=0)
        assert forced["crawled"] == 10 and forced["skipped"] == 0
        print(f"[30] crawl re-run: {again['crawled']} fetched, "
              f"{again['skipped']} skipped; with max_age=0, "
              f"{forced['crawled']} refetched")


def test_band_is_remembered():
    """The covered band survives closing the application.

    We store the bounds, not the spread: the player's rank moves, and
    recomputing from a spread would shift the window towards ranks with no
    data.
    """
    from ppfarmer.crawler import crawl

    class FakeCrawlClient:
        def country_stats(self, mode="osu", pages=1):
            return [{"code": "XX", "active_users": 1000}]
        def rankings_page(self, mode, country, page):
            if page != 1:
                return []
            return [{"global_rank": 65000 + i, "pp": 3000,
                     "user": {"id": i, "username": f"p{i}", "country_code": "XX"}}
                    for i in range(1, 6)]
        def user_best(self, uid, mode="osu", limit=50):
            return [_score(1, [{"acronym": "CL"}], pp=250.0)]

    db = os.path.join(tempfile.mkdtemp(), "sp.db")
    with Store(db) as store:
        assert store.last_band() is None, "with no crawl, nothing is stored"
        low, high = band_for(71345, 10000)
        crawl(FakeCrawlClient(), store, low, high, top_n=10, max_age_days=0)

    with Store(db) as store:
        assert store.last_band() == (low, high), (
            f"expected band {(low, high)}, got {store.last_band()}")
        print(f"[31] band remembered: #{low:,}-#{high:,} recovered "
              f"after reopening")


def test_target_players():
    """Targeting a headcount tightens the band onto the closest players.

    Asking for 1000 ranks only yields about 700 players, US ones being out of
    reach. Targeting a headcount is therefore more predictable.
    """
    from ppfarmer.crawler import crawl

    class FakeCrawlClient:
        """Returns 300 players spread over the requested band."""
        def __init__(self):
            self.tops = 0
        def country_stats(self, mode="osu", pages=1):
            return [{"code": "XX", "active_users": 1000}]
        def rankings_page(self, mode, country, page):
            if page != 1:
                return []
            return [{"global_rank": 70000 + i, "pp": 3000,
                     "user": {"id": i, "username": f"p{i}", "country_code": "XX"}}
                    for i in range(1, 301)]
        def user_best(self, uid, mode="osu", limit=50):
            self.tops += 1
            return [_score(1, [{"acronym": "CL"}], pp=250.0)]

    # The window aimed at is wider than the target, to absorb the losses.
    assert window_for_players(1000) > 1000
    assert window_for_players(100) >= 100

    db = os.path.join(tempfile.mkdtemp(), "tp.db")
    client = FakeCrawlClient()
    with Store(db) as store:
        report = crawl(client, store, 70000, 70400, top_n=10,
                       max_age_days=0, target_players=50)
        # Discovery sees everything, but only the 50 closest are fetched:
        # that is the cost the user asked for.
        assert report["discovered"] == 300
        assert report["crawled"] == 50, (
            f"50 tops expected, got {report['crawled']}")
        assert client.tops == 50, f"50 requests expected, got {client.tops}"

        # These are the closest to the player, so the highest rank numbers.
        low, high = report["band"]
        assert high == 70300, f"closest should be #70300, got {high}"
        assert low == 70251, f"furthest should be #70251, got {low}"
        assert store.last_band() == (low, high)

        # The analysed panel must match the target, not the discovery.
        _, panel = rank_beatmaps(store, low, high, min_players=1)
        assert panel == 50, f"panel expected 50, got {panel}"
        print(f"[33] target of 50 players: {report['discovered']} discovered, "
              f"band tightened to #{low:,}-#{high:,}, "
              f"{report['crawled']} tops fetched, panel {panel}")


def test_year_filter():
    """Scores can be filtered by year, and each row exposes its breakdown."""
    db = os.path.join(tempfile.mkdtemp(), "an.db")
    with Store(db) as store:
        store.upsert_players([
            {"user_id": i, "username": f"p{i}", "country": "FR",
             "global_rank": 71000, "pp": 3000} for i in range(1, 13)
        ])
        # Four players per year, over three years.
        for i in range(1, 13):
            year = 2022 + (i - 1) // 4
            sc = _score(1, [{"acronym": "CL"}], pp=200.0 + year - 2022)
            sc["ended_at"] = f"{year}-06-15T12:00:00Z"
            store.save_scores(i, [sc])

        every, _ = rank_beatmaps(store, 70000, 72000, min_players=1)
        m = every[0]
        assert m["players"] == 12
        assert m["years"] == {2022: 4, 2023: 4, 2024: 4}, m["years"]
        assert m["median_year"] == 2023, m["median_year"]

        recent, _ = rank_beatmaps(store, 70000, 72000, min_players=1, year_min=2024)
        assert recent[0]["players"] == 4
        assert recent[0]["years"] == {2024: 4}
        assert recent[0]["median_year"] == 2024

        older, _ = rank_beatmaps(store, 70000, 72000, min_players=1, year_max=2022)
        assert older[0]["players"] == 4 and older[0]["median_year"] == 2022

        window, _ = rank_beatmaps(store, 70000, 72000, min_players=1,
                                  year_min=2023, year_max=2023)
        assert window[0]["players"] == 4 and window[0]["years"] == {2023: 4}

        # The threshold applies after the filter: restricting can empty the row.
        empty, _ = rank_beatmaps(store, 70000, 72000, min_players=10, year_min=2024)
        assert empty == [], "four players do not pass a threshold of ten"
        print(f"[32] year filter: {m['years']} overall, "
              f"median {m['median_year']}; 2024 alone -> "
              f"{recent[0]['players']} players")


def test_page_styles_are_scoped():
    """Layout rules must not leak out of the map list.

    The map rows are styled through `ol`/`li`. Left unscoped, those rules also
    hit the ordered list inside the setup dialog, turning each step into a
    three-column card with its words torn apart. Anything that lays out a
    list has to name its container.
    """
    import pathlib
    import re

    page = (pathlib.Path(__file__).resolve().parent.parent
            / "ppfarmer" / "static" / "index.html")
    css = re.search(r"<style>(.*?)</style>", page.read_text(encoding="utf-8"), re.S)
    assert css, "the page must carry a stylesheet"
    rules = css.group(1)

    # A selector made only of bare element names, at the start of a rule.
    bare = re.findall(r"^\s*((?:ol|li|ul|p|input|button|select|a)\s*(?:,\s*\w+\s*)*)\{",
                      rules, re.M)
    offenders = [b.strip() for b in bare
                 if re.fullmatch(r"(ol|li|ul)(\s*,\s*(ol|li|ul))*", b.strip())]
    assert not offenders, (
        f"unscoped list selectors would leak into the dialog: {offenders}")

    # The password field has to be themed like the others, or it renders as a
    # bright white box in a dark dialog.
    assert "input[type=password]" in rules, "the password input is not themed"

    # The dialog steps must stay a real numbered list.
    assert "list-style: decimal" in rules, "the setup steps lost their numbering"
    print("[34] page styles scoped: no bare list selector, password themed, "
          "steps numbered")


def test_reachability_by_depth():
    """A country drops out of the sweep once the band is deeper than its cap.

    Each country ranking stops at 10,000 entries, so a country holding a share
    s of ranked players only exposes global rank G while s <= 10000/G. Nobody
    is missing above rank 30,000; deeper down the largest countries fall away
    one by one, and the window has to widen to still reach the headcount.
    """
    # Shares close to the measured ones: US 16.5%, RU 12.6%, then smaller.
    countries = [
        {"code": "US", "active_users": 1650},
        {"code": "RU", "active_users": 1260},
        {"code": "PL", "active_users": 380},
        {"code": "FR", "active_users": 390},
        {"code": "KR", "active_users": 145},
    ]
    # The long tail has to be many small countries, not one big entry: a
    # single 60% block would itself breach the cap and skew the whole share.
    countries += [{"code": f"C{i}", "active_users": 250} for i in range(25)]

    # Shallow: the cap is far away, everyone reaches.
    assert reachable_share(countries, 8000) == 1.0

    # Around rank 71,000 the threshold is 14.1%, so only the US falls out.
    at_71k = reachable_share(countries, 71000)
    assert 0.80 < at_71k < 0.86, at_71k

    # Past 100,000 the threshold is 10% and Russia goes too.
    at_150k = reachable_share(countries, 150000)
    assert at_150k < at_71k, "depth must not increase coverage"
    assert 0.66 < at_150k < 0.74, at_150k

    # The window follows: same target, wider sweep when less is reachable.
    shallow = window_for_players(1000, 8000, countries)
    deep = window_for_players(1000, 150000, countries)
    assert deep > shallow, (
        f"a deeper band needs a wider window, got {deep} vs {shallow}")

    # Without the country list it falls back to the fixed rate, which must
    # still be usable rather than absurd.
    blind = window_for_players(1000)
    assert 1000 < blind < 2000, blind
    print(f"[35] reachability: {at_71k*100:.0f}% at rank 71k, "
          f"{at_150k*100:.0f}% at 150k; window {shallow:,} -> {deep:,} ranks")


def test_release_workflow_conditions():
    """Workflow `if:` conditions must be single-line expressions.

    Written as a `|` block, the value keeps its newlines. The expression
    parser rejects that, the step is skipped, and the run still reports
    success: a release silently never happens. That is exactly how v1.1 was
    missed.
    """
    import pathlib

    try:
        import yaml
    except ImportError:
        print("[36] pyyaml missing, workflow not checked")
        return

    path = (pathlib.Path(__file__).resolve().parent.parent
            / ".github" / "workflows" / "release.yml")
    if not path.exists():
        print("[36] no workflow to check")
        return

    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = spec["jobs"]["windows"]["steps"]
    multiline = [s.get("name", s.get("uses", "?")) for s in steps
                 if isinstance(s.get("if"), str) and chr(10) in s["if"]]
    assert not multiline, (
        f"these conditions span several lines and will never match: {multiline}")

    # The release has to be gated on the version step, or a run with nothing
    # new to publish would try to reuse an existing tag and fail.
    publish = [s for s in steps if s.get("name") == "Publish the release"]
    assert publish, "the workflow no longer publishes anything"
    assert "steps.version.outputs.release" in publish[0]["if"], (
        "the release is not gated on the computed version")

    # Triggering on a branch is what makes it automatic; tags alone meant
    # nothing was ever published without a manual tag.
    triggers = spec.get(True) or spec.get("on")
    assert "push" in triggers and "branches" in triggers["push"], (
        "the workflow no longer runs on pushes to a branch")
    print("[36] release workflow: single-line conditions, gated on the "
          "computed version, triggered by branch pushes")


def test_filters_persist():
    """Filters have to survive a reload and a restart of the local server.

    Three of them (top count, scoring, year bounds) decide what the server is
    asked for, not just what is displayed, so restoring them after the first
    fetch would show the previous filter's data until something else moved.
    Restoring has to happen before the page loads anything.
    """
    import pathlib
    import re

    page = (pathlib.Path(__file__).resolve().parent.parent
            / "ppfarmer" / "static" / "index.html")
    text = page.read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", text, re.S).group(1)

    listed = re.search(r"const FILTER_IDS = \[(.*?)\];", script, re.S)
    assert listed, "the page no longer declares the filters it remembers"
    remembered = set(re.findall(r'"([^"]+)"', listed.group(1)))

    # A remembered id that no longer exists would throw on restore and leave
    # the page blank, since restoreFilters runs before anything is drawn.
    present = set(re.findall(r'id="([^"]+)"', text))
    assert not remembered - present, (
        f"remembered filters with no control: {sorted(remembered - present)}")

    # The filters that reach the server are the ones worth checking by name:
    # forgetting one is invisible until a crawl returns the wrong slice.
    for wanted in ("topn", "scoring", "yearmin", "yearmax"):
        assert wanted in remembered, f"{wanted} decides the fetch but is not saved"

    # Every accessor guarded: a browser set to block site data throws from
    # localStorage instead of returning null, which would kill the script.
    for name in ("saveFilters", "restoreFilters", "resetFilters"):
        body = re.search(name + r"\(\) \{(.*?)\n\}", script, re.S)
        assert body, f"{name} is gone"
        assert "catch" in body.group(1), f"{name} does not guard against storage throwing"

    # Order at the bottom of the page: restore, then load.
    restore = script.rindex("restoreFilters();")
    bootstrap = script.rindex('load(+$("topn").value);')
    assert restore < bootstrap, (
        "the page fetches before restoring: the first draw would use the "
        "default filters, not the saved ones")

    # Restored controls must be marked, or load() overwrites them with the
    # default it computes from the panel size.
    assert 'dataset.touched = "1"' in script, (
        "restored controls are not marked, load() will overwrite them")

    print(f"[37] filters persist: {len(remembered)} controls remembered, "
          "restored before the first fetch, storage access guarded")


if __name__ == "__main__":
    test_discovery()
    test_mods()
    test_aggregation()
    test_one_row_per_mod_combo()
    test_ppcalc()
    test_popularity_vs_gain()
    test_min_players()
    test_gain_on_replayed_map()
    test_played_flags()
    test_duration_and_efficiency()
    test_duration_filter()
    test_median_vs_mean()
    test_band_above_only()
    test_score_age_filter()
    test_median_age()
    test_pp_grouped_by_mods()
    test_scoring_version_grouping()
    test_mod_filter()
    test_page_javascript_parses()
    test_page_styles_are_scoped()
    test_release_workflow_conditions()
    test_scoring_filter()
    test_crawl_reports_skipped()
    test_band_is_remembered()
    test_target_players()
    test_reachability_by_depth()
    test_year_filter()
    test_filters_persist()
    print("\nAll tests pass.")
