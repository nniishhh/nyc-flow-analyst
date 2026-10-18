# Data dictionary: MTA subway origin-destination estimates

Edit this file to teach the analyst more about the data. Everything here is
loaded before every answer.

## The dataset

- **Name:** MTA Subway Origin-Destination Ridership Estimate: Beginning 2026
- **Page:** https://data.ny.gov/d/28vm-gjqr
- **Endpoint (SoQL):** https://data.ny.gov/resource/28vm-gjqr.json
- **Size:** about 10 million rows per month. January to August 2026 is
  published (~83M rows); MTA adds a month at a time.
- **One row** = one origin station complex → one destination station complex,
  for one weekday, one hour, in one month.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `year`, `month` | number | When the trips happened. `month` 1–12. |
| `day_of_week` | text | `Monday` … `Sunday`, capitalized. |
| `hour_of_day` | number | 0–23, the hour the trip **started**, rounded down. |
| `origin_station_complex_id` | number | Where the trip started. Joins to `stations.station_id`. |
| `origin_station_complex_name` | text | e.g. `Bedford Av (L)` |
| `destination_station_complex_id` | number | Where the trip is inferred to have ended. |
| `destination_station_complex_name` | text | |
| `origin_latitude`, `origin_longitude`, `destination_…` | number | Station complex coordinates. |
| `estimated_average_ridership` | number | Estimated riders for that pair and hour, **averaged over every matching weekday in that month**. |

Never select `origin_point` / `destination_point` (geo objects) or `timestamp`.

## How to compute the numbers people ask for

- **Riders on an average day** (e.g. "an average Saturday"):
  `sum(estimated_average_ridership) / {{month_count}}` with
  `day_of_week = 'Saturday'` and no month filter. Each month's value is already
  a per-day average, so summing months and dividing by the month count
  averages across months.
- **With a month filter**, divide by the number of months in the filter
  instead (e.g. `month IN (6, 7, 8)` → `/ 3`).
- **A time window** sums hours: `hour_of_day BETWEEN 7 AND 9` means trips
  starting 7:00–9:59 a.m.
- **A whole day** is all 24 hours summed.
- **Weekday** means Monday–Friday. Average them by dividing by 5 × months.
- **Departures** from a place: filter `origin_station_complex_id`.
  **Arrivals** to a place: filter `destination_station_complex_id`.
- **Share of trips:** compute the total in the same query or a second one; never
  compare totals from different filters.
- **Change vs another day:** `(a - b) / b`, said as "up 61%", never "1.61x".

## Neighborhoods

Stations roll up to 108 NYC Planning neighborhoods (2020 NTAs, names
shortened, e.g. `Midtown South–Flatiron` with an en dash). Local lookups:

- `stations`: `station_id`, `station`, `borough`, `latitude`, `longitude`, `neighborhood`
- `neighborhoods`: `neighborhood`, `borough`, `latitude`, `longitude`, `stations`

In SoQL, `{{neighborhood:Williamsburg}}` expands to that neighborhood's station
ids. People's names for places often differ from NTA names:

| People say | Use |
|---|---|
| Times Square, Midtown | `Midtown` (Grand Central is `East Midtown`) |
| Union Square, Flatiron | `Midtown South–Flatiron` |
| Wall Street, FiDi | `Financial District` |
| Williamsburg (Bedford Av) | `Williamsburg`; also consider `East Williamsburg`, `South Williamsburg` |

When unsure, run `nyc.py neighborhood "<name>"` to see which stations it
covers, and say which neighborhood you used.

## SoQL rules (Socrata's SQL)

- No `FROM` clause; the query runs against the dataset.
- Strings use single quotes; names are case-sensitive (`'Saturday'`).
- `sum`, `avg`, `count`, `min`, `max`, `GROUP BY`, `ORDER BY`, `HAVING`, `LIMIT` work.
- Always `GROUP BY` and aggregate on the server. Never pull raw rows.
- `LIMIT` up to 50000. Grouping by destination station needs about 500.
- Group by **ids**, not names, and join names locally from `stations`.

## Caveats to state when they matter

- These are **modeled estimates**, not counted journeys. MTA infers destinations
  from each rider's next MetroCard/OMNY entry; the subway records no exits.
- Trips that don't tap again (walk home, take a bus) are estimated, not observed.
- Stations are complexes: Times Sq–42 St includes all its lines.
- Neighborhoods follow NYC Planning boundaries, which may not match how people
  use the names.
