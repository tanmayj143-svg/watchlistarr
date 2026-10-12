# Scoring reference

Every candidate movie gets a score. The score is the **sum of every rule that
matched**. Highest score wins, and that movie is what gets requested.

Two rules are not scorers: **limits** (see below) either pass or block.

## Anatomy of a rule

| Field | Meaning |
|---|---|
| `kind` | what attribute to test |
| `op` | how to compare |
| `value` | the operand |
| `points` | what to add (may be negative) |
| `limit` | if true, a match **blocks** instead of scoring |
| `weight` | optional multiplier on the points |

## Attribute kinds

| `kind` | Compares against | Example value |
|---|---|---|
| `genre` | genres list | `["Sci-Fi", "Horror"]` |
| `country` | countries list | `["China", "Hong Kong"]` |
| `language` | languages list | `["Mandarin"]` |
| `decade` | release decade | `[1960]` or `"1960s"` |
| `year` | release year | `1994` |
| `rating` | IMDb rating | `7.5` |
| `popularity` | provider popularity | `10` |
| `runtime` | minutes | `140` |
| `source_position` | position in the source list | `1` (top of list) |
| `quota` | requests already made in a window | **limit** |
| `capacity` | free disk GB / used % / library count | **limit** |
| `list_size` | how many fit in the cadence | **limit** |

## Operators

| `op` | Meaning |
|---|---|
| `in` | is any of |
| `nin` | is none of |
| `contains` | partial text match |
| `gte` / `lte` | ≥ / ≤ |
| `gt` / `lt` | > / < |
| `eq` / `neq` | = / ≠ |
| `between` | within two bounds |

## Limits (hard stops)

A limit that matches **rejects the movie**, regardless of score. Use these for
"only this many per period" and "not when the disk is full".

### Quota — "X per Y time"

```json
{"id":"cap","kind":"quota","window":"month","threshold":4,
 "comparator":"gte","limit":true,"points":0}
```

`window`: `day` · `week` · `month` · `quarter` · `year`

Once 4 requests exist in the window, every candidate is blocked until it rolls
over. That is what makes "Sci-Fi this week" mean *at most* two per week.

### Capacity — "X GB free" / "Y % used" / "N in library"

```json
{"id":"disk","kind":"capacity","unit":"free_gb","comparator":"lt",
 "threshold":100,"limit":true,"points":0}
```

| `unit` | Measures |
|---|---|
| `free_gb` | free space on Radarr's library volumes |
| `used_gb` | used space |
| `percent` | percent used |
| `movie_count` | movies already in the library |

Requires Radarr. Without it, capacity rules report *capacity unknown* and do not
gate anything — requests continue rather than stalling.

## Worked examples

### "1960s Chinese cinema this month" (built-in preset)

```json
[
 {"id":"cap","kind":"quota","window":"month","threshold":4,
  "comparator":"gte","limit":true,"points":0},
 {"id":"dec","kind":"decade","op":"in","value":[1960],"points":40},
 {"id":"ctry","kind":"country","op":"in","value":["China","Hong Kong","Taiwan"],"points":30},
 {"id":"lang","kind":"language","op":"in","value":["Mandarin","Cantonese"],"points":20},
 {"id":"rating","kind":"rating","op":"gte","value":7.5,"points":10},
 {"id":"new","kind":"decade","op":"nin","value":[1960],"points":-15}
]
```

A 1965 Chinese Mandarin film scores **100** (40+30+20+10).
The same film from 2020 scores **45** (30+20+10−15).
That 55-point gap is the prioritisation working.

### "Sci-Fi this week"

```json
[
 {"id":"cap","kind":"quota","window":"week","threshold":2,
  "comparator":"gte","limit":true,"points":0},
 {"id":"sf","kind":"genre","op":"in","value":["Sci-Fi"],"points":40},
 {"id":"hi","kind":"rating","op":"gte","value":7.0,"points":15},
 {"id":"year","kind":"year","op":"gte","value":2010,"points":10},
 {"id":"nonsf","kind":"genre","op":"nin","value":["Sci-Fi"],"points":-25}
]
```

At 0 requests this week a good recent Sci-Fi film scores 65; after 2 requests
the quota blocks everything until next week.

### "Disk-aware backlog"

```json
[
 {"id":"disk","kind":"capacity","unit":"free_gb","comparator":"lt",
  "threshold":100,"limit":true,"points":0},
 {"id":"rated","kind":"rating","op":"gte","value":7.5,"points":25},
 {"id":"runtime","kind":"runtime","op":"lte","value":140,"points":5}
]
```

## Genre spelling

Providers disagree, so genres are canonicalised before matching:

| You type / provider says | Treated as |
|---|---|
| Science Fiction, science fiction, scifi, sf | Sci-Fi |
| Film Noir, noir | Film-Noir |
| romcom, rom-com | Romance |
| Animated | Animation |

Unknown genres are accepted verbatim, so nothing is lost when a provider invents
a new label.

## Why scoring is additive

A movie matching one strong rule can outrank one matching two weak rules —
1960s Japan (40) beats modern China (35). That is deliberate: it is what makes
point values meaningful and tunable. If you want conjunctive behaviour, raise
the value of the rule you care about most, or use a limit to exclude what you
don't want rather than penalising it.

## Inspecting scores

Every ranked row shows the per-rule contribution ("Sci-Fi +40, rating≥7 +15"),
so you can always see why something won or lost.