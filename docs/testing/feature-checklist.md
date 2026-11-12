# Feature checklist

Every Triage feature, one check each. For a tester (person or bot) with Hermes Desktop, a test profile and the setup from [`conversation-tests.md`](conversation-tests.md). Scenarios A to L there cover the model's side in depth; this list covers everything, including what you can only see on screen.

Rules: a test profile only, no keys in prompts or files, change no file in this repository, no personal data in the report. If a check fails, record what happened instead and go on.

Catalog: the 3 demo skills (`git-commit`, `pr-review`, `weather-brief`) plus at least 30 real skills, connected under **Settings → Plugins → triage → Skill folders**. Where a check names a draft, type exactly that.

## 1. Install and catalog

| # | Do | Expect |
| --- | --- | --- |
| 1.1 | `python run.py status` | The skill count matches your folders. |
| 1.2 | Open Hermes Desktop, type anything in the prompt | A row of chips appears at the top of the prompt box, each with a percentage. |
| 1.3 | Add a new folder with one `SKILL.md` to a connected skill folder | Within a few seconds `python run.py list` shows it. |
| 1.4 | Edit that `SKILL.md`'s description and save | `python run.py list` shows the new description. |
| 1.5 | Delete that folder | It leaves `python run.py list`. |
| 1.6 | Leave Hermes idle for 11 minutes, then read `agent.log` | No `triage:` line reports skills as changed when you changed none. |

## 2. Chips while typing

| # | Do | Expect |
| --- | --- | --- |
| 2.1 | Type `review the commit message` | `git-commit` is among the chips with a percentage. |
| 2.2 | Clear the prompt | Chips stay up and follow the conversation (dashed). An empty new chat may show none. |
| 2.3 | Type `let's go with the second option` | No chip attaches itself (no dashed pill appears on the left). |
| 2.4 | Type `use git-commit for this task` | `git-commit` attaches itself: a dashed pill appears on the left. |
| 2.5 | Delete the draft | The dashed pill leaves again. |

## 3. Holding and sending

| # | Do | Expect |
| --- | --- | --- |
| 3.1 | Type `help me with my change`, click the `git-commit` chip | A pill appears on the left pile; the prompt text is unchanged; the row refills. |
| 3.2 | Click two more chips | The pile shows the newest pill with a count badge; hovering it lists every held skill. |
| 3.3 | Click a pill on the pile | That skill leaves the pile and can come back as a chip. |
| 3.4 | Send the message | The pills fly to the fan on the right; the chips hide while the reply runs and return after. |
| 3.5 | Ask `Quote the Marker line of the skill you were given.` | The answer is `COMMIT_BODY_ONLY`, without a `skill_view` or `skill_load` call. |

## 4. The fan and the skill card

| # | Do | Expect |
| --- | --- | --- |
| 4.1 | Look at the right side after 3.4 | Each loaded skill is a chip as wide as its name; short names give small chips. |
| 4.2 | Hover a fan chip | A card opens with the name, description and two buttons: **Keep** and **View body**. |
| 4.3 | Click **View body** | The skill's text is shown. |
| 4.4 | In a new chat, ask the model to load a skill itself (scenario B) | That skill appears on the fan too. |

## 5. Search

| # | Do | Expect |
| --- | --- | --- |
| 5.1 | Type `/wea` at the end of the prompt | The chip row turns into search results marked `/`; `weather-brief` is among them. Hermes' own `/` menu may open and covers the part of the row it overlaps. |
| 5.2 | Click `weather-brief` | It is held on the pile; `/wea` leaves the prompt. |
| 5.3 | Click the magnifier left of the chips, prompt empty | A list opens: **Favourites**, at most 5 **Most used**, then **Suggested**. Its search strip looks like Hermes' Ctrl+K palette. |
| 5.4 | Type `pr` in the list's search field | Matching skills from the whole catalog; arrow keys move, Enter or a click holds the skill. |
| 5.5 | Click a row's star | The skill moves to Favourites; it is not held. Close and reopen the list: the star is still on. |
| 5.6 | Click the star again | It leaves Favourites. |

## 6. Trash and lock

| # | Do | Expect |
| --- | --- | --- |
| 6.1 | Hold two skills, click the trash button | The pile empties; chips turn grey; a lock button appears. |
| 6.2 | Type `use git-commit for this task` | Nothing attaches itself and chips cannot be clicked. |
| 6.3 | Click the lock button | Chips work again. |
| 6.4 | Lock again, then send a message | The lock lifts after sending. |

## 7. Placement on the composer

| # | Do | Expect |
| --- | --- | --- |
| 7.1 | In a chat with a goal or a changed-files row above the prompt | Those rows stay where Hermes puts them; the chip row is inside the prompt box, below them. |
| 7.2 | Type `/` | Hermes' menu opens above the prompt box; the chip row stays in place. |
| 7.3 | In a long chat, scroll up | Hermes' "N messages" button shows above the prompt box, clear of the chips; the chip row does not move. |
| 7.4 | Make the window narrow, then wide | The row fits the composer's width; nothing spills off screen. |

## 8. Long chats

| # | Do | Expect |
| --- | --- | --- |
| 8.1 | Run scenario I (`/compress` after a few turns) | Skills whose text left the context leave the fan; their chips can be picked again. No fan chip is see-through. |
| 8.2 | After the compression, clear the prompt | The chips fit the conversation; none is chosen for the words "context", "compaction" or "handoff" alone. |
| 8.3 | Run scenario J (Keep, then `/compress`) | The kept skill goes back to the left pile and loads with the next message. |
| 8.4 | Run scenario L | A chat that got a new id still shows its skills. |

## 9. The model's side

| # | Do | Expect |
| --- | --- | --- |
| 9.1 | Scenario A | No skills list in the system prompt. |
| 9.2 | Scenario B | The model finds and loads a skill with `skill_search` and `skill_load`. |
| 9.3 | Scenario C | `skill_view` is refused for a skill nobody loaded. |
| 9.4 | Scenario H | On the command line a clear request loads the skill; an unclear one shows a short list and loads nothing. |

## 10. Settings

| # | Do | Expect |
| --- | --- | --- |
| 10.1 | Set `shortlist_size` to `3`, restart the gateway | The row shows 3 chips. |
| 10.2 | Set `auto_accept_threshold` to `0` | Draft 2.4 no longer attaches anything. |
| 10.3 | Set `always_load` to `weather-brief`, start a new chat, send `hi` | The model can quote `WEATHER_BODY_ONLY` without loading it. |
| 10.4 | Put every setting back | |

## 11. Command line

| # | Do | Expect |
| --- | --- | --- |
| 11.1 | `python run.py peek review the commit message` | A ranked list with percentages; `git-commit` first. |
| 11.2 | `python run.py eval --db <demo catalog> --cases examples/eval-demo.jsonl` | A table of results; no error. |
| 11.3 | `python run.py restore` (at the very end) | Hermes' own skills are given back; Hermes lists them again after a restart. |

## Report

One line per check: `<number> pass | fail | could not run — <one line on what happened, if not pass>`. Then the environment (Hermes version, OS, model id, skill count) and anything that surprised you.
