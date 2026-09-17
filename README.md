# ppfarmer

Initialy made for farming pps, you can actually discover good maps through the app. Filter popular maps to play based on similar ranked players **just above you**

ppfarmer works through the osu! API. Fetch players top 100 scores and let you filter the result.

In order to respect the API limitation the process of crawling can take a while depending on how much player data you want/need.

If you care about it, know this was made using **Claude Opus 5**.

This app is not recommended if you are above 5 digits. At rank #100000 you will only get about 71% of your desired players. Falling at ~11% at rank 1000000

## Install

### Release

Download `ppfarmer.exe` from the
[latest release](../../releases/latest) and run it. That is all.

### From Source code

```bash
pip install -r requirements.txt
python -m ppfarmer
```

Optionally copy `.env.example` to `.env` and fill in your credentials there;
otherwise the app asks for them in the browser.

## First launch

The app starts a local server, opens your browser, and asks for two things:

**An osu! API client.** Create one at
<https://osu.ppy.sh/home/account/edit> (the **OAuth** section, *New OAuth
Application*), leave the callback URL empty, then paste the client ID and
secret. They are checked against osu! immediately, and stored locally.

**Your profile URL**, such as `https://osu.ppy.sh/users/4721909`. Everything is
ranked relative to your global rank, so this is the one piece of identity the
app needs.

Both are remembered, so this happens once. A *change profile* link in the
header is available for whatever reason (Don't multiaccount kids).

Data lives in `%LOCALAPPDATA%\ppfarmer` if you download from release.

### The interface

Two tabs.

**Maps** — sort, support threshold, star range, played-length range, score
years, game version, mod combinations, search, and hiding maps you have already
played. Everything is a control with an immediate result.

**Crawl** — run a crawl without touching a terminal: players to analyse,
scores fetched per player, early stop. The request-cost estimate updates as you
tune it, a bar tracks progress, and Cancel keeps whatever was already fetched.
One crawl at a time.

The server reads your profile and top 100 once at startup (two API requests),
sends the whole dataset in one go, and all filtering then happens in the
browser. That is what makes the controls instant: no round trip once the page
has loaded.

Three controls do go back to the server, because they change the aggregation
itself: top depth, game version and the year window. Results are cached per
combination.

The server only listens on `127.0.0.1` and exposes nothing private: everything
it serves comes from the local cache and the public API.

### Filters are remembered

Every control on the Maps tab is saved in the browser as you touch it, so a
refresh — or stopping and restarting the local server — brings back the view
you had set up rather than the defaults.

They are kept in the browser's own storage, not in the database: they are a
per-browser convenience, and nothing about them belongs in the crawl data. The
three controls that reach the server are restored *before* the first request,
so the page never draws a full year range or a top 50 for a moment before
correcting itself.

If the remembered filters leave the list empty, the empty state offers **Reset
all filters**, which clears them and reloads.

## How it works, and why

### The rank 10,000 wall

`GET /rankings/osu/global` is capped at 10,000 results
([`RankingController::MAX_RESULTS`](https://github.com/ppy/osu-web/blob/master/app/Http/Controllers/RankingController.php),
50 per page via [`Model::PER_PAGE`](https://github.com/ppy/osu-web/blob/master/app/Models/Model.php)).
There is no way to ask "who sits at rank 71,000". Issue
[ppy/osu-web#7374](https://github.com/ppy/osu-web/issues/7374) has been asking
for that endpoint for years and is still open.

### Going around it through country rankings

The same endpoint filtered by country (`?country=XX`) is capped at 10,000 too —
but **per country**, and every entry carries the player's real `global_rank`:
[`UserStatistics::globalRank()`](https://github.com/ppy/osu-web/blob/master/app/Models/UserStatistics/Model.php)
returns `rank_score_index`, a precomputed column with no depth limit.

A country exposes global rank *G* within its first 10,000 entries as long as it
holds less than 10,000/*G* of ranked players. For *G* = 72,000 the threshold is
14.2%. Sweeping the countries below it reconstructs the band.

Since pages are sorted by pp descending, hence by global rank ascending, the
starting page is found by **binary search** (~8 requests per country) instead of
walking everything.

The countries swept are the **50 on the first page** of
[osu.ppy.sh/rankings/osu/country](https://osu.ppy.sh/rankings/osu/country), in
the site's order — by total performance, not by player count. A single request
gets them, and they cover 2,749,407 active players out of 2,940,299, or 93%.
Fixed on purpose: making it an option only made results less comparable between
crawls.

### Measured coverage

Over 236 countries and 2,940,299 declared active players:

| Country | Share | Deepest rank reached | Players found |
| --- | --- | --- | --- |
| US | 15.40% | #51,256 | **0** |
| FR | 3.68% | #147,034 | 65 |
| DE | 3.50% | #100,918 | 90 |
| PL | 3.52% | #116,402 | 82 |

The US crosses the threshold: its 10,000th player already sits at global rank
51,256, well above the band. **US players at that level are structurally out of
reach**, about 15% of the population. That is the known limit of the method, and
the tool reports it rather than hiding it. Every other country goes far deeper
than needed.

### Counting players, not ranks

Because US players are unreachable, a band of ranks only yields a fraction of
its headcount: on real data, **10,000 ranks gave 7,642 players**, or 76.4%.
Asking for a number of ranks gave an unpredictable sample size.

`--players N` targets a headcount. The sweep covers a wider window — about +45%,
calibrated on a deliberately conservative 70% harvest rate — then the band
tightens onto the **N players closest to you** as the database knows them. Only
those get their top fetched, so the cost matches what was asked.

Tightening happens after writing to the database, not on the players discovered
in that run alone: otherwise a narrow crawl ignored players a previous crawl had
already found, and fetched more tops than the target.

### The band is remembered

The crawl stores the **bounds** of the band it covered, not its spread: your
rank moves, and recomputing from a spread would shift the window towards ranks
with no data. Without an explicit setting, the app reuses those bounds —
otherwise reopening it fell back to a default spread of 1000 and showed only a
fraction of what had been fetched: 781 players instead of 7,630 after a
10,000-rank crawl.

### Stable or lazer

The **CL (Classic)** mod marks a score submitted from stable. It is stored but
**kept out of the grouping**: the same play is the same play, and splitting each
map into two samples would gain nothing.

The data stays reachable. Each row shows the split — `15 st / 5 lz` — and
`--scoring` restricts to one version, with the same selector on the page. The
support threshold applies **after** that filter, so restricting to one version
can empty a row that only held up thanks to the other.

On a fresh sample of 1250 scores, **91.4% carry CL**. Lazer is still a minority,
so restricting to `lazer` gives thin samples.

### One row per mod combination

A map played in NM and in HR yields **two rows**: same map, two ways of farming
it, each with its own headcount, typical pp and mean. Merging them forced a
"dominant" combination and discarded the rest, which could show an HR ceiling
next to a row labelled NM.

On *[Shiawase!!]*, at depth 100:

| Mods | Players | Median | Mean |
| --- | --- | --- | --- |
| NM | 1259 | 238.3 | 234.7 |
| HD | 259 | 251.5 | 249.0 |
| HDHR | 73 | 260.9 | 263.7 |
| HR | 65 | 251.3 | 254.0 |

The support threshold applies **per combination**: a map carried by 300 players
in NM and 6 in HR gives one solid row and one anecdotal row, not an average.

The dominant combination depends on the depth counted. At top 10 this map has no
NM score at all — at 238pp they do not make a top 10 — only modded ones. At top
100, NM dominates.

**NC is treated as DT** and DC as HT: same mods bar the sound, identical pp, and
separating them would split samples for nothing. NC is 4.2% of scores against
24.3% for DT.

`--mods` keeps only the combinations asked for, and the page has one checkbox
each. The six that shape farming are `NM`, `HD`, `HR`, `DT`, `HDHR` and `HDDT`;
the `other` token keeps everything else. Spelling is free: `hddt`, `HDDT` and
`DTHD` all mean the same thing.

### Popularity is not profitability

Your total pp is a weighted sum: your 100 best scores sorted by pp descending,
weighted by `0.95^i`. A score below your 100th therefore earns **nothing at
all**.

A map can be very popular in your band and still be useless to you. From the
tests: a map present in 62% of players' tops but whose typical score is 120pp
earns +0.0pp to a player whose 100th score is 151pp — while a map present in
only 7% of tops, at 316pp, earns +76pp.

That is why each row shows its **typical pp** and the exact **gain** you would
get by matching it, computed by reinserting the value into your own weighted
curve. `--sort gain` sorts by that.

What the typical pp measures exactly, because the nuance matters:

- the **median** of the scores, not the mean — a lone outstanding score must not
  drag the value up. Both are shown side by side, with the sample size in
  brackets;
- only players **in the band**, not osu! at large;
- only those who have the map **in their top N** — on *[Shiawase!!]*, 1664 of
  the 3803 players in the panel, or 43.8%;
- only within that **mod combination**.

Two caveats:

**The support threshold.** Sorting by gain without a filter surfaces lone
outliers — a map present in a single player's top shows a median equal to that
one score, which predicts nothing. The symptom was blatant on real data: the
seven top rows by gain each had a single player.

A fixed threshold solves nothing, since 5 players out of 20 is a strong signal
while 5 out of 1544 is noise. The default is **3% of the panel, minimum 5** (46
players for a panel of 1544). The effect is clear: at 5, the gain ranking is
dominated by rows with 0% support; at the adaptive threshold it converges on
maps carried by 7 to 10% of players, with gains tightening to +28 to +33pp
instead of an illusory +45 to +65pp.

**Selection bias.** A map only appears in a player's top 10 if they did well on
it. The typical pp shown is therefore what players who *succeeded* got, not the
expectation of an average attempt. Read it as a reachable value, not a promise.

### Maps you have already played

They are **shown and flagged `PLAYED`**, not hidden: an existing score can be
improved, and that is often the best lead. The detail line gives your pp, your
accuracy, your rank, your mods, whether you FC'd, and the margin between you and
the band's typical score.

The gain calculation accounts for it. osu! keeps only **the best score per
map**: replaying does not add to your top, it replaces the old score. Treating a
replay as an addition would badly overstate the gain, all the more so when the
existing score is good. `pp_gain(..., replacing=...)` removes the old score
before inserting the new one, and returns zero if the target is not better.

On the real dataset, 20 of the 49 rows retained were already in the top 100 and
17 of those were improvable — hiding them meant hiding the best opportunity in
the list (a 226pp score with no FC on a map where the band gets 298).

### Length

Each row shows its length, corrected by the speed mods: DT and NC speed up by
50%, HT and DC slow down by 25%. A three-minute map farmed with DT only costs
two minutes, and that is the length that counts.

`--length-min` and `--length-max` filter on it. Both accept several spellings —
`90`, `90s`, `1m30`, `1m`, `1:30` — because writing a four-minute map's length
in raw seconds is not natural. The filter applies to the **played** length, not
the one shown on the website: a 2m30 map farmed with DT passes a 2-minute
filter.

`--sort efficiency` ranks by pp per minute, and that often contradicts
popularity: the most common map in the band returns 72pp/min where the third
most common returns 169 for an equivalent pp. The figure only shows under that
sort.

### Filtering by year

Every score carries its date, so the ranking can be restricted to a window of
years: `--year-min` / `--year-max`, or two sliders on the page. Each row shows
the **median year** of its scores, and its tooltip gives the full breakdown.

The filter applies **before** grouping, so it changes headcounts, medians and
which mod combinations survive — which is what lets you watch the meta/popularity move.

Since the support threshold applies after the filter, a narrow window can empty
rows that only held up on the whole.

### Score age

A score older than **10 years** is not kept (`MAX_SCORE_AGE_YEARS` in
`store.py`). It is not really relevent as "popular".

Be aware that a map made and ranked more than 10 years ago can still be shown.

The dropped score is not replaced and positions are not renumbered. Promoting
the next one would pull into the "top 10" something that never was there; it is
more honest to count a player with nine retained scores.

Measured on 1250 scores from 25 players in the band: **none exceeds 10 years**,
the oldest being 9.9. So the filter removes nothing today, it is a guard rail. A
lower threshold would bite immediately: 26% of scores are over 5 years old, 3.9%
over 8.

### Request budget

The [ppy/osu-api wiki](https://osu.ppy.sh/docs/index.html#terms-of-use) asks to stay under 60 requests a minute.

| Step | Requests |
| --- | --- |
| Country list | 1 |
| Discovery (50 countries) | ~450 |
| Tops of the players kept | one each |

A single request per player covers their whole top: the includes on
`/users/{id}/scores/best` are
[`['beatmap', 'beatmapset', 'user']`](https://github.com/ppy/osu-web/blob/master/app/Transformers/ScoreTransformer.php),
so map metadata arrives with the score.

Everything is cached in `ppfarmer.db` (SQLite). Re-runs only refetch tops
older than 30 days, and players already up to date are reported rather than
silently skipped.

For your own API safety this is not adjustable. To be honest I don't even know if this is a good use of the API. Time will tell.

## Neighbouring projects

[**Tillerinobot**](https://github.com/Tillerino/Tillerinobot) (Java, IRC) is the
historical reference. Its *gamma* engine reasons explicitly about pp and mods,
and it also builds on players' **top 10** — which supports that choice of depth.

[**pupsbot**](https://github.com/Pupariaa/pupsbot) (Node.js, Redis + MySQL)
groups players by pp range rather than by rank, and hides maps already in the
top 200. Its *target pp* idea directly inspired the gain calculation above,
except that we compute the exact delta on the weighted curve instead of
approximating it with tuned constants.

Its player pool, however, did not transfer: the Redis sorted set `scores_by_pp`
fills **organically**, recording the top 100 of every player who messages the
bot. That assumes thousands of users. For a personal tool starting from zero the
pool would hold exactly one player — hence the country sweep.
