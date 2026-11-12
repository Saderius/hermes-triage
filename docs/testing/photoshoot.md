# Photoshoot: one shot per Triage feature

For a photographer (person or bot) making README, catalog and X images of Triage in Hermes Desktop. Run it after the [feature checklist](feature-checklist.md) passed, so every shot shows a working feature.

## Rules

- Real screenshots only. Do not generate, retouch or redraw any part of the app. Banners may crop and arrange real shots and add the exact text given below, nothing else.
- No personal data in any shot: no real names, emails, user names in paths, host names, keys. Use a test profile and neutral chat titles. Check every shot before packing.
- Do not change plugin code or fake chat content by editing the page. If something looks broken, shoot it anyway, put it in `bugs/` and describe it.

## Setup

- Hermes Desktop with Triage installed as the [README](../../README.md) says. Check: chips appear at the top of the prompt box while you type.
- Catalog: the 3 demo skills (`examples/demo-skills`) plus at least 30 real skills, connected under **Settings → Plugins → triage → Skill folders**. More skills give livelier chips.
- Window about 1440x900, sidebar visible, dark theme first. If the Acrylic plugin is also installed, use its Acrylic preset.
- A model configured, and one chat with a few real turns before you start, for example: `Review my last commit message and suggest a better one`, then `Give me an example for a bug fix in a login form`.
- Star two skills first (magnifier → star), so the Favourites group is not empty.
- Shot E15 needs a chat long enough to scroll; E16 needs `/compress` (see scenario I in [conversation-tests.md](conversation-tests.md)).

## Shots

PNG, full window. File name: `E<nn>-<theme>-<feature>.png`, for example `E01-dark-chips.png`. For every shot also save a tight crop of the composer area (chip row, prompt and the fan): `E<nn>-<theme>-<feature>-crop.png`. Hover only where a shot says so; otherwise no mouse pointer.

| Shot | Feature | How to set it up |
| --- | --- | --- |
| E01 | Chips while typing | Type `review the commit message`. Chips with percentages at the top of the prompt box. |
| E02 | Auto-attach | Type `use git-commit for this task`. The dashed pill on the left that you did not click. |
| E03 | Held pile | Type `help me with my change`, click three chips. Hover the pile so its list of held skills opens. |
| E04 | Loaded fan | Send E03's message, wait for the reply. The fan of loaded skills on the right. |
| E05 | Skill card | Hover a chip on the fan. Its card with **Keep** and **View body**. |
| E06 | Skill text | Click **View body** on that card. |
| E07 | Kept | Click **Keep** on the card; it reads **Kept**. |
| E08 | Chips follow the chat | Clear the prompt. Dashed chips for what the chat is about. |
| E09 | `/` search in the prompt | Type `/wea` at the end of the prompt. Chips marked `/`. If Hermes' own `/` menu opens over them, shoot it as it is, then press Escape and shoot again. |
| E10 | Search list, empty | Clear the prompt, click the magnifier. Favourites, Most used, Suggested, with stars. |
| E11 | Search list, typed | Type `pr` in the list's search field. Highlight a row with the arrow keys. |
| E12 | Trash and lock | Hold two skills, click the trash button. Grey chips and the lock button. |
| E13 | With Hermes' rows | A chat where Hermes shows a goal or changed-files row above the prompt, with the chips in the prompt box. Skip if you cannot get one. |
| E14 | Hermes' `/` menu wins | Type `/` alone. Hermes' menu open over the left part of the chip row. |
| E15 | Scrolling | In a long chat, scroll up. Hermes' "N messages" button above the prompt box. |
| E16 | After a compression | After `/compress`: the fan holds only what is still in context, the kept skill (E07) is back on the left pile. |
| E17 | Command line | A terminal showing `python run.py peek review the commit message` and its output. |
| E18 | Optional: context saved | Hermes' `/context` view in one chat with Triage on, then the same with Triage disabled (`hermes plugins disable triage`, restart). Skip if `/context` shows no skills figure. |

Then repeat E01, E03, E04, E10 in the light theme.

Optional video: 10 to 15 seconds, mp4. Type `help me with my change`, click two chips, send, watch the pills fly to the fan.

## Banners (built only from the shots above)

- Catalog banner, 1200x600 (2:1).
- GitHub social preview, 1280x640.
- X post images, 1600x900, the best 4.

Text, exactly: `Triage: the right skill, without the skill list in your context`. No Nous or Hermes logos other than what the app itself shows.

## Return

`photoshoot-triage.zip` with `shots/`, `banners/`, `video/`, `bugs/` and `INDEX.md`. `INDEX.md` lists every shot (file, feature, theme, what is visible, anything off), what was skipped and why, and your 6 best picks for the catalog's screenshot slots.
