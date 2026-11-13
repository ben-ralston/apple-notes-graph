"""Invented notes for the demo library. Nothing here comes from a real Notes
library: the person, their projects, garden and reading list are made up.

Bodies use the small markup ``build_demo_db.py`` understands:

    # Heading          ## Subheading        ``` code block ```
    * bullet           - dashed             1. numbered        (two spaces = one indent)
    [ ] todo           [x] done             > block quote
    **bold** *italic* __underline__ ~~strike~~ {pink|coloured text}
    [text](https://url)   [[Note Title]]   {#tag}   | table | row |
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Note:
    title: str
    folder: str
    body: str
    age_days: int = 200  # days since creation
    idle_days: int = 30  # days since last edit


# (path, ZFOLDERTYPE): 0 = ordinary, 1 = Recently Deleted
FOLDERS = [
    ("Notes", 0),
    ("Projects", 0),
    ("Projects/Archive", 0),
    ("Reading", 0),
    ("Garden", 0),
    ("Kitchen", 0),
    ("Journal", 0),
    ("Recently Deleted", 1),
]

# A note that was deleted outright: links to it are the demo's broken links.
DANGLING_TITLE = "Router firmware notes"

NOTES: list[Note] = []


def add(title: str, folder: str, body: str, age: int = 200, idle: int = 30) -> None:
    NOTES.append(Note(title, folder, body, age, idle))


# --- hubs ----------------------------------------------------------------------

add(
    "★ Index",
    "Notes",
    """
Start here. Each line is a hub for one area.
* [[Projects]] - things being built
* [[Reading list]] - books and what stuck
* [[Garden log]] - beds, plants, schedule
* [[Recipes]] - what we actually cook
* [[Weekly reviews]] - one note per week
* [[Someday / maybe]]
## Working notes
* [[How I take notes]]
* [[Inbox]]
""",
    620,
    2,
)

add(
    "Inbox",
    "Notes",
    """
Unsorted. Empty this on Sundays.
[ ] File the seed order under [[Seed starting schedule]]
[ ] Decide whether the [[Weather station]] gets a second anemometer
[x] Move bike receipts into [[Bike rebuild: parts list]]
""",
    400,
    1,
)

add(
    "Someday / maybe",
    "Notes",
    """
Not now. Review every quarter.
* Cold frame for the north bed, see [[Raised bed plan]]
* Second pass on [[Static site generator]] themes
* Learn to sharpen properly: [[Knife sharpening]]
* Read [[The Timeless Way of Building]] again, slowly
""",
    500,
    40,
)

add(
    "How I take notes",
    "Notes",
    """
# Rules
1. One idea per note, titled as a claim or a thing.
2. Link when writing, not later.
3. Hubs are lists of links with one line of context.
# Where the ideas came from
* [[Evergreen notes]]
* [[Zettelkasten]]
* [[Progressive summarization]]
* [[Spaced repetition]]
> A note that is never linked is a note you will not find.
""",
    580,
    12,
)

# --- projects ------------------------------------------------------------------

add(
    "Projects",
    "Projects",
    """
# Active
* [[Weather station]] - sensors on the shed roof
* [[Static site generator]] - the blog's build tool
* [[Home network]] - VLANs and backups
* [[Bike rebuild]] - the 1980s touring frame
* [[Rain barrel plumbing]]
# Paused
* [[Reading tracker app]]
# Done
* [[Retired: Raspberry Pi cluster]]
* [[Workbench build]]
""",
    600,
    3,
)

add(
    "Weather station",
    "Projects",
    """
A solar-powered station on the shed roof that logs to the home server every minute. {#project}
# Status
**Running since March.** Wind readings are *noisy* below 2 m/s; humidity is __good__ after recalibration. The ~~DHT22~~ was replaced with a {pink|BME280}.
# Hardware
* Microcontroller board with LoRa radio
  * 3.3 V logic, deep sleep between readings
  * Antenna: quarter-wave whip
* Sensors
  * BME280 for temperature, humidity, pressure
  * Reed-switch anemometer
  * Tipping-bucket rain gauge
* Power
  * 6 W panel and one 18650 cell
# Wiring
| Sensor | Pin | Interval |
| BME280 | I2C 0x76 | 60 s |
| Anemometer | GPIO 14 | interrupt |
| Rain gauge | GPIO 27 | interrupt |
# Next steps
[x] Move logger off the breadboard
[x] Recalibrate humidity, see [[Weather station: sensor calibration]]
[ ] Print a vented cap for [[Weather station: enclosure]]
[ ] Publish a daily summary through the [[Static site generator]]
# Firmware loop
```
wake()
read_sensors()
send(packet)
sleep(60)
```
# Notes
Radio range notes are in [[Home network]]. Rainfall totals feed the [[Garden log]].
> Measure twice, solder once.
Datasheet: [BME280 overview](https://example.com/bme280)
""",
    240,
    4,
)

add(
    "Weather station: sensor calibration",
    "Projects",
    """
Salt-solution method for humidity, ice bath for temperature. Part of [[Weather station]].
| Reference | Expected | Measured | Offset |
| Ice bath | 0.0 C | 0.4 C | -0.4 |
| Saturated salt | 75.3 % | 78.1 % | -2.8 |
| Boiling (alt. adjusted) | 99.1 C | 99.6 C | -0.5 |
1. Seal sensor in a jar with the reference for 8 hours.
2. Log every minute, average the last hour.
3. Store offsets in firmware, not in the dashboard.
""",
    180,
    60,
)

add(
    "Weather station: enclosure",
    "Projects",
    """
Stevenson-screen style stack of printed plates. Part of [[Weather station]].
* White PETG, 7 louvred plates
* Mounts to the mast with two U-bolts from the [[Bike rebuild: parts list]] leftovers
[ ] Vented cap
[ ] Insect mesh
""",
    170,
    20,
)

add(
    "Static site generator",
    "Projects",
    """
A small build tool for the blog: Markdown in, HTML out, no runtime. {#project}
# Design
* Pages are files; folders are sections
* [[SSG: template language]] is deliberately tiny
* [[SSG: incremental builds]] keep rebuilds under a second
# Borrowed ideas
* Patterns that repeat, from [[A Pattern Language]]
* Keep the core small, from [[The Mythical Man-Month]]
[ ] RSS feed
[x] Syntax highlighting
""",
    420,
    9,
)

add(
    "SSG: template language",
    "Projects",
    """
Three constructs only: *include*, *each*, *if*. Part of [[Static site generator]].
```
{{ each post in posts }}
  <a href="{{ post.url }}">{{ post.title }}</a>
{{ end }}
```
No expressions beyond dotted lookups. Logic belongs in the build step.
""",
    400,
    90,
)

add(
    "SSG: incremental builds",
    "Projects",
    """
Hash every input; rebuild a page only when one of its inputs changed. Part of [[Static site generator]].
1. Walk the content tree and hash files.
2. Record which templates each page included.
3. Rebuild pages whose hash set changed.
See also [[Backup strategy]] for the same content-hash trick.
""",
    390,
    120,
)

add(
    "Home network",
    "Projects",
    """
One router, two access points, a small server in the hall cupboard. {#project}
* [[Home network: VLAN plan]]
* [[Backup strategy]]
* Old notes: [[Router firmware notes]]
* LoRa gateway for the [[Weather station]] sits next to the server
""",
    520,
    15,
)

add(
    "Home network: VLAN plan",
    "Projects",
    """
Part of [[Home network]].
| VLAN | Purpose | Internet |
| 10 | Trusted devices | yes |
| 20 | Guests | yes |
| 30 | Sensors and cameras | no |
| 40 | Server management | no |
Sensors VLAN has no route out; the [[Weather station]] gateway lives there.
""",
    300,
    200,
)

add(
    "Backup strategy",
    "Projects",
    """
Three copies, two media, one off-site. Part of [[Home network]].
[x] Nightly snapshot to the server
[x] Weekly copy to the external disk
[ ] Quarterly restore test
Restore tests are logged in [[Weekly reviews]].
""",
    480,
    35,
)

add(
    "Bike rebuild",
    "Projects",
    """
A steel touring frame from the 1980s, stripped to bare metal. {#project}
* [[Bike rebuild: parts list]]
* [[Bike rebuild: wheel truing]]
* Paint: powder coat, dark green
Test ride target: before the first frost, see [[Frost dates]].
""",
    150,
    6,
)

add(
    "Bike rebuild: parts list",
    "Projects",
    """
Part of [[Bike rebuild]].
| Part | Status | Cost |
| Bottom bracket | fitted | 28 |
| Brake calipers | ordered | 61 |
| Bar tape | to buy | 14 |
| Tyres 32 mm | fitted | 70 |
[x] Headset bearings
[ ] Cable set
[ ] Mudguards
""",
    140,
    5,
)

add(
    "Bike rebuild: wheel truing",
    "Projects",
    """
Part of [[Bike rebuild]]. Quarter turns only.
1. Find the hop first, then the wobble.
2. Loosen opposite, tighten near.
3. Stress-relieve the spokes and check again.
""",
    120,
    70,
)

add(
    "Rain barrel plumbing",
    "Projects",
    """
Two 200 L barrels off the shed gutter, linked low so they fill together.
* First-flush diverter before barrel one
* Overflow to the [[Raised bed plan]] soak trench
* Level sensor could report through the [[Weather station]]
""",
    210,
    80,
)

add(
    "Reading tracker app",
    "Projects",
    """
Paused. A tiny app to log pages per day and chart streaks.
* Data model is one table; see [[Reading list]] for the fields worth keeping
* Blocked on deciding whether it is worth building at all
""",
    330,
    260,
)

add(
    "Workbench build",
    "Projects",
    """
Done. 2x4 frame, plywood top, lower shelf.
* Top is 60 cm deep, which is too shallow; build 75 next time
* Vise mounted on the left corner
""",
    700,
    400,
)

add(
    "Retired: Raspberry Pi cluster",
    "Projects/Archive",
    """
Four boards in a stack. Fun, loud, pointless. Replaced by one small server, see [[Home network]].
""",
    900,
    700,
)

add(
    "Old blog ideas",
    "Projects/Archive",
    """
* Why the [[Static site generator]] has no plugins
* A year of rain data
* Things I got wrong about backups
""",
    800,
    650,
)

add(
    "2024 goals",
    "Projects/Archive",
    """
[x] Finish the workbench
[x] Read twelve books
[ ] Run a half marathon
""",
    1040,
    690,
)

# --- reading -------------------------------------------------------------------

BOOKS = [
    (
        "A Pattern Language",
        "Christopher Alexander",
        "finished",
        [
            "Patterns are relationships, not objects.",
            "Good rooms have light on two sides.",
        ],
        ["The Timeless Way of Building", "Raised bed plan"],
    ),
    (
        "The Timeless Way of Building",
        "Christopher Alexander",
        "finished",
        [
            "A place is alive when it supports what happens in it.",
            "You cannot design it in one pass.",
        ],
        ["A Pattern Language"],
    ),
    (
        "The Mythical Man-Month",
        "Fred Brooks",
        "finished",
        [
            "Adding people to a late project makes it later.",
            "Conceptual integrity matters most.",
        ],
        ["Static site generator"],
    ),
    (
        "How to Take Smart Notes",
        "Sonke Ahrens",
        "finished",
        [
            "Writing is the thinking, not the record of it.",
            "Notes need context to be reused.",
        ],
        ["Zettelkasten", "Evergreen notes"],
    ),
    (
        "Thinking in Systems",
        "Donella Meadows",
        "finished",
        [
            "Stocks change slowly even when flows change fast.",
            "Look for the feedback loop.",
        ],
        ["Compost", "Rain barrel plumbing"],
    ),
    (
        "The Design of Everyday Things",
        "Don Norman",
        "finished",
        ["Blame the design before the person.", "Make the state visible."],
        ["A Pattern Language"],
    ),
    (
        "Braiding Sweetgrass",
        "Robin Wall Kimmerer",
        "reading",
        ["Take only what is given.", "Reciprocity is a practice."],
        ["Garden log"],
    ),
    (
        "The Art of Fermentation",
        "Sandor Katz",
        "reference",
        [
            "Salt, time and temperature are the whole recipe.",
            "Trust smell before the clock.",
        ],
        ["Sauerkraut", "Sourdough starter"],
    ),
    (
        "Salt Fat Acid Heat",
        "Samin Nosrat",
        "finished",
        ["Salt early.", "Acid is the missing thing more often than salt."],
        ["Recipes", "Roast chicken"],
    ),
    (
        "The One-Straw Revolution",
        "Masanobu Fukuoka",
        "finished",
        ["Do less to the soil.", "Mulch instead of tilling."],
        ["Compost", "Raised bed plan"],
    ),
    (
        "Seeing Like a State",
        "James C. Scott",
        "reading",
        ["Legible is not the same as true.", "Local knowledge resists plans."],
        ["Thinking in Systems"],
    ),
    (
        "The Checklist Manifesto",
        "Atul Gawande",
        "finished",
        ["Checklists catch the stupid errors, not the hard ones.", "Keep them short."],
        ["Backup strategy"],
    ),
    (
        "Zen and the Art of Motorcycle Maintenance",
        "Robert Pirsig",
        "finished",
        ["Care is a kind of attention.", "Stuckness is the start of understanding."],
        ["Bike rebuild"],
    ),
    (
        "Shop Class as Soulcraft",
        "Matthew Crawford",
        "finished",
        ["Manual work is thinking work.", "Repair teaches humility."],
        ["Bike rebuild", "Workbench build"],
    ),
    (
        "The Pragmatic Programmer",
        "Hunt and Thomas",
        "finished",
        ["Do not repeat yourself.", "Fix broken windows early."],
        ["Static site generator", "The Mythical Man-Month"],
    ),
    (
        "Make It Stick",
        "Brown, Roediger, McDaniel",
        "finished",
        ["Retrieval beats re-reading.", "Spacing feels worse and works better."],
        ["Spaced repetition"],
    ),
    (
        "Four Thousand Weeks",
        "Oliver Burkeman",
        "finished",
        ["You will not get everything done.", "Choose what to neglect."],
        ["Someday / maybe"],
    ),
    (
        "The Soul of a New Machine",
        "Tracy Kidder",
        "to read",
        [],
        ["The Mythical Man-Month"],
    ),
    ("Working in Public", "Nadia Eghbal", "to read", [], []),
    ("The Overstory", "Richard Powers", "to read", [], []),
]

add(
    "Reading list",
    "Reading",
    "\n".join(
        ["Books by status. Each has its own note."]
        + [
            f"# {status.capitalize()}\n"
            + "\n".join(
                f"* [[{title}]] - {author}"
                for title, author, s, _, _ in BOOKS
                if s == status
            )
            for status in ("reading", "finished", "reference", "to read")
        ]
        + ["# Method", "* [[How I take notes]]", "* [[Spaced repetition]]"]
    ),
    610,
    5,
)

for n, (title, author, status, takeaways, related) in enumerate(BOOKS):
    lines = [f"*{author}*. Status: **{status}**. Listed in [[Reading list]]."]
    if takeaways:
        lines.append("# What stuck")
        lines += [f"* {t}" for t in takeaways]
    if related:
        lines.append("# Connects to")
        lines += [f"* [[{r}]]" for r in related]
    add(title, "Reading", "\n".join(lines), 560 - n * 22, 25 + n * 17)

add(
    "Zettelkasten",
    "Reading",
    """
A box of small notes, each one idea, each linked to its neighbours.
* The value is in the links, not the notes
* Source: [[How to Take Smart Notes]]
* Compare [[Evergreen notes]]
""",
    540,
    150,
)

add(
    "Evergreen notes",
    "Reading",
    """
Notes written to be kept and improved, titled as claims.
* Atomic, concept-oriented, densely linked
* Related: [[Zettelkasten]], [[Progressive summarization]]
""",
    530,
    160,
)

add(
    "Progressive summarization",
    "Reading",
    """
Highlight in layers on each revisit: bold, then highlight, then a summary at the top.
* Works for reference material, less for ideas
* Compare [[Evergreen notes]]
""",
    520,
    310,
)

add(
    "Spaced repetition",
    "Reading",
    """
Review just before forgetting. Intervals grow after each success.
* Evidence: [[Make It Stick]]
* I only use it for plant names and keyboard shortcuts
""",
    510,
    95,
)

# --- garden --------------------------------------------------------------------

PLANTS = [
    (
        "Tomatoes",
        "bed 1",
        "Start indoors 6 weeks before last frost. Prune suckers on the cordon types.",
        ["Basil", "Seed starting schedule"],
    ),
    (
        "Basil",
        "bed 1",
        "Pinch the tops weekly. Bolts fast in heat.",
        ["Tomatoes", "Pesto"],
    ),
    (
        "Garlic",
        "bed 2",
        "Plant in October, harvest when a third of the leaves brown.",
        ["Frost dates"],
    ),
    (
        "Kale",
        "bed 2",
        "Sweeter after frost. Net against cabbage white.",
        ["Frost dates"],
    ),
    (
        "Runner beans",
        "bed 3",
        "Wigwam of 2.4 m canes. Pick every two days.",
        ["Raised bed plan"],
    ),
    ("Courgettes", "bed 3", "Two plants are enough. Three is a mistake.", ["Compost"]),
    (
        "Carrots",
        "bed 4",
        "Sow thin, cover with mesh against carrot fly.",
        ["Raised bed plan"],
    ),
    (
        "Leeks",
        "bed 4",
        "Dib deep holes and water in; do not backfill.",
        ["Seed starting schedule"],
    ),
    ("Strawberries", "pots", "Replace plants every third year from runners.", []),
    (
        "Rhubarb",
        "corner",
        "Do not pick in the first year. Mulch each winter.",
        ["Compost"],
    ),
    (
        "Raspberries",
        "fence",
        "Autumn fruiting: cut all canes to the ground in February.",
        [],
    ),
    (
        "Chillies",
        "greenhouse",
        "Need a long season. Start in January under lights.",
        ["Seed starting schedule", "Hot sauce"],
    ),
]

add(
    "Garden log",
    "Garden",
    "\n".join(
        [
            "Four raised beds, a greenhouse and a fence line. {#garden}",
            "# Plans",
            "* [[Raised bed plan]]",
            "* [[Seed starting schedule]]",
            "* [[Frost dates]]",
            "* [[Compost]]",
            "# Plants",
        ]
        + [f"* [[{name}]] - {where}" for name, where, _, _ in PLANTS]
        + [
            "# This year",
            "Rainfall comes from the [[Weather station]]. Barrels: [[Rain barrel plumbing]].",
        ]
    ),
    590,
    3,
)

for n, (name, where, text, related) in enumerate(PLANTS):
    lines = [f"Grown in **{where}**. Listed in [[Garden log]].", text]
    if related:
        lines.append("# See also")
        lines += [f"* [[{r}]]" for r in related]
    add(name, "Garden", "\n".join(lines), 450 - n * 18, 14 + n * 21)

add(
    "Raised bed plan",
    "Garden",
    """
Four beds, 1.2 m by 2.4 m, rotated yearly. Part of [[Garden log]].
| Bed | This year | Next year |
| 1 | Tomatoes, basil | Beans |
| 2 | Garlic, kale | Roots |
| 3 | Beans, courgettes | Brassicas |
| 4 | Carrots, leeks | Tomatoes |
Layout follows *light on two sides* from [[A Pattern Language]], more or less.
""",
    470,
    45,
)

add(
    "Seed starting schedule",
    "Garden",
    """
Counted back from the last frost in [[Frost dates]]. Part of [[Garden log]].
| Weeks before | Sow |
| 12 | Chillies |
| 8 | Leeks |
| 6 | Tomatoes |
| 4 | Basil, courgettes |
[ ] Order seed potatoes
[x] Clean the propagator
""",
    460,
    28,
)

add(
    "Frost dates",
    "Garden",
    """
Last frost: mid April, give or take two weeks. First frost: late October.
* Own records start from the [[Weather station]] in March
* Before that, the regional average
""",
    455,
    190,
)

add(
    "Compost",
    "Garden",
    """
Three bays, turned monthly. Part of [[Garden log]].
* Greens and browns by volume, roughly one to two
* Stocks and flows: [[Thinking in Systems]]
* No cooked food, no bindweed
""",
    440,
    75,
)

# --- kitchen -------------------------------------------------------------------

RECIPES = [
    (
        "Roast chicken",
        "Dry brine overnight. 220 C for 20 minutes, then 180 C until 74 C at the thigh.",
        ["Chicken stock"],
    ),
    (
        "Chicken stock",
        "Carcass, onion, carrot, bay. Barely simmer for 4 hours. Never boil.",
        ["Roast chicken", "Risotto"],
    ),
    (
        "Risotto",
        "Toast the rice. Hot stock a ladle at a time. Rest two minutes off the heat.",
        ["Chicken stock"],
    ),
    (
        "Pesto",
        "Basil, pine nuts, garlic, parmesan, oil. Pound, do not blend, if there is time.",
        ["Basil"],
    ),
    (
        "Sauerkraut",
        "2 % salt by weight. Pack hard, keep it under the brine, taste from day 5.",
        ["The Art of Fermentation"],
    ),
    (
        "Hot sauce",
        "Ferment chillies in 3 % brine for two weeks, then blend with vinegar.",
        ["Chillies", "The Art of Fermentation"],
    ),
    (
        "Sourdough loaf",
        "75 % hydration. Four folds, overnight in the fridge, bake in a hot pot.",
        ["Sourdough starter"],
    ),
    (
        "Tomato sauce",
        "Halved tomatoes, garlic, oil, low oven for two hours. Freeze flat.",
        ["Tomatoes"],
    ),
    (
        "Leek and potato soup",
        "Sweat the leeks slowly. Blend half, leave half.",
        ["Leeks", "Chicken stock"],
    ),
    (
        "Flatbreads",
        "Equal weights yoghurt and self-raising flour. Dry pan, very hot.",
        [],
    ),
    (
        "Dal",
        "Red lentils, turmeric, salt. Finish with cumin and garlic fried in butter.",
        [],
    ),
    (
        "Rhubarb crumble",
        "Roast the rhubarb first with orange zest. Oats in the topping.",
        ["Rhubarb"],
    ),
]

add(
    "Recipes",
    "Kitchen",
    "\n".join(
        [
            "What we actually cook. Method notes, not full recipes. {#kitchen}",
            "# Dishes",
        ]
        + [f"* [[{name}]]" for name, _, _ in RECIPES]
        + [
            "# Basics",
            "* [[Sourdough starter]]",
            "* [[Pantry staples]]",
            "* [[Knife sharpening]]",
            "# Books",
            "* [[Salt Fat Acid Heat]]",
            "* [[The Art of Fermentation]]",
        ]
    ),
    570,
    7,
)

for n, (name, method, related) in enumerate(RECIPES):
    lines = [method, "Listed in [[Recipes]]."]
    if related:
        lines.append("# Goes with")
        lines += [f"* [[{r}]]" for r in related]
    add(name, "Kitchen", "\n".join(lines), 430 - n * 20, 20 + n * 26)

add(
    "Sourdough starter",
    "Kitchen",
    """
Fed 1:2:2 with wholemeal rye. Lives in the fridge between bakes.
1. Take out the night before.
2. Feed, leave at room temperature.
3. Use at the peak; put the rest back.
Background in [[The Art of Fermentation]]. Used for [[Sourdough loaf]].
""",
    415,
    110,
)

add(
    "Pantry staples",
    "Kitchen",
    """
Restock when one is left.
[x] Olive oil
[x] Red lentils
[ ] Tinned tomatoes
[ ] Rye flour
[x] Sea salt
[ ] Bay leaves
""",
    405,
    3,
)

add(
    "Knife sharpening",
    "Kitchen",
    """
1000 grit then 3000. Fifteen degrees a side.
* Raise a burr along the whole edge before switching sides
* Strop on newspaper
""",
    395,
    330,
)

# --- journal -------------------------------------------------------------------

WEEKS = [
    (
        44,
        ["Weather station", "Pantry staples"],
        "Humidity offsets finally hold. Restocked the cupboard.",
    ),
    (
        43,
        ["Bike rebuild: parts list", "Garlic"],
        "Calipers ordered. Garlic went in on Saturday.",
    ),
    (
        42,
        ["Static site generator", "Seeing Like a State"],
        "Incremental builds landed. Read two chapters.",
    ),
    (41, ["Backup strategy", "Kale"], "Restore test passed. Netted the kale too late."),
    (
        40,
        ["Bike rebuild", "Sourdough loaf"],
        "Frame back from the powder coater. Good crumb this week.",
    ),
    (
        39,
        ["Weather station: enclosure", "Braiding Sweetgrass"],
        "Printed four of seven plates.",
    ),
    (38, ["Tomato sauce", "Tomatoes"], "Last of the tomatoes picked and roasted."),
    (
        37,
        ["Home network", "Four Thousand Weeks"],
        "Moved the sensors to their own VLAN.",
    ),
    (
        36,
        ["Hot sauce", "Chillies"],
        "Started the ferment with the first ripe chillies.",
    ),
    (
        35,
        ["Rain barrel plumbing", "Compost"],
        "Second barrel linked in. Turned bay two.",
    ),
]

add(
    "Weekly reviews",
    "Journal",
    "\n".join(
        ["One note per week: what moved, what did not."]
        + [f"* [[2026-W{week} weekly review]]" for week, _, _ in WEEKS]
    ),
    300,
    2,
)

for n, (week, touched, summary) in enumerate(WEEKS):
    lines = [summary, "Part of [[Weekly reviews]].", "# Touched"]
    lines += [f"* [[{t}]]" for t in touched]
    lines += ["# Next week", "[ ] Empty the [[Inbox]]"]
    add(
        f"2026-W{week} weekly review", "Journal", "\n".join(lines), 5 + n * 7, 2 + n * 7
    )

# --- orphans -------------------------------------------------------------------

add(
    "Gift ideas",
    "Notes",
    """
* A proper trowel
* Subscription to a seed club
* Enamel bread tin
""",
    260,
    44,
)

add(
    "Car maintenance log",
    "Notes",
    """
| Date | Work | Mileage |
| March | Oil and filter | 61,200 |
| June | Front pads | 64,850 |
| September | Two tyres | 68,300 |
""",
    380,
    50,
)

add(
    "Packing list",
    "Notes",
    """
[ ] Head torch
[ ] Stove and gas
[ ] First aid kit
[x] Map case
""",
    340,
    280,
)

add(
    "Quotes",
    "Notes",
    """
> The best time to plant a tree was twenty years ago. The second best time is now.
> Simple things should be simple.
""",
    450,
    370,
)

add(
    "Meeting notes template",
    "Notes",
    """
# Attendees
# Decisions
# Actions
[ ] Owner, due date
""",
    610,
    500,
)

add(
    "Paint colours",
    "Notes",
    """
* Hall: off-white, matt
* Shed: dark green, same as the bike frame
* Front door: undecided
""",
    275,
    130,
)

# A trashed note: in Recently Deleted, hidden from lists and the graph.
add(
    "Scratch", "Recently Deleted", "Temporary numbers from the calibration run.", 30, 29
)
