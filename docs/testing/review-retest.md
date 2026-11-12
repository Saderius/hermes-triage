# Re-test after the catalog review

What changed for NousResearch/hermes-agent#154048, and how to check each change. Hermes Desktop, a test profile, Triage installed from this repository (restart the gateway, then Ctrl+R in Hermes). Report one line per check: `<number> pass | fail — <what happened instead>`.

Rules: a test profile only, no keys in prompts or files, change no file in this repository, no personal data in the report.

## A. The chip bar is a row inside the prompt box

| # | Do | Expect |
| --- | --- | --- |
| A1 | Type `Review my React component and write unit tests for it` | A row of chips with percentages appears **inside the prompt box, at its top**, above the text you type. Nothing floats above the box. |
| A2 | Clear the prompt in a chat with a few turns | The row shows dashed chips for the conversation. In a brand-new empty chat the row disappears and leaves no empty gap in the box. |
| A3 | Make the window narrow, then wide | The row fits the box; long names end in "…"; nothing sticks out of the box. |
| A4 | Type `/` | Hermes' own `/` menu opens above the prompt box. The chip row does not move and stays usable. |
| A5 | In a long chat, scroll up | Hermes' "N messages" button shows above the prompt box and does not cover the chips. |
| A6 | A chat with a goal or changed-files row | Those rows stay where Hermes puts them; the chips are inside the box below them. |

## B. The held pile

| # | Do | Expect |
| --- | --- | --- |
| B1 | Click a chip | A pill appears in the slot on the left; the prompt text is unchanged; the row refills. |
| B2 | Click two more chips | The slot shows the newest pill and a badge `3`. |
| B3 | Hover the pile | A small list opens above it with all three held skills. Moving the pointer into the list keeps it open. |
| B4 | Click a pill in that list | That skill leaves the pile. |
| B5 | Click the pill in the slot itself | It leaves the pile. A click does not open the list. |

## C. The search list (magnifier)

| # | Do | Expect |
| --- | --- | --- |
| C1 | Prompt empty, click the magnifier | The list opens above the row (below it when there is no room), with the search field focused: Favourites, at most 5 Most used, Suggested. |
| C2 | Type `pr`, press the arrow keys, then Enter | The highlight moves; Enter holds the skill; the list stays open. |
| C3 | Click the magnifier again, then reopen and press Escape, then reopen and click outside | Each one closes the list. |
| C4 | Click a row's star, close and reopen | The skill is under Favourites; it was not held. |

## D. The fan and the skill card

| # | Do | Expect |
| --- | --- | --- |
| D1 | Hold two skills and send | The pills slide right and the skills appear on the fan at the right end of the row. |
| D2 | Hover a fan chip | A card opens above that chip: name, description, **Keep**, **View body**. |
| D3 | Move the pointer from the chip into the card | The card stays open. |
| D4 | Hover another fan chip | The card moves over that chip and shows that skill. |
| D5 | Click **View body**, then **Keep** | The skill's text shows in the card; the button reads **Kept**. |
| D6 | Click a fan chip, move the pointer away | The card stays open (pinned). Escape or a click elsewhere closes it. |

## E. Trash and lock

| # | Do | Expect |
| --- | --- | --- |
| E1 | Hold two skills, click the trash button | The pile empties, chips turn grey, a lock button appears. |
| E2 | Click the lock button | Chips work again. |

## F. Backend fixes

| # | Do | Expect |
| --- | --- | --- |
| F1 | Ask the model: `Add one line "Reviewed." to the end of the skill <a skill in Hermes' own skills folder> using skill_manage` | Hermes' own `skill_manage` handles it: the tool result does not say `served_by: triage`. |
| F2 | In the test profile's `config.yaml` set `skills: guard_agent_created: true`, restart the gateway, and ask the model to patch a skill from a connected skill folder with `skill_manage` | Triage refuses; the message names `skills.guard_agent_created`. The skill file is unchanged. Set it back afterwards. |
| F3 | Start Hermes with a second profile (`HERMES_HOME` set to another folder) | Triage keeps its catalog and settings under that profile's folder, not the first one's. |

## G. Admission check

| # | Do | Expect |
| --- | --- | --- |
| G1 | `hermes plugins validate <this repository>` | `desktop surface — stays inside the plugin SDK surface` and `Validation passed.` |

End the report with anything that looked wrong or surprising, even where every check passed.
