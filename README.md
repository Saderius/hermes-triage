# Triage

Skill triage for the Hermes composer. Built for the Nous Research Hermes Plugin Hackathon.

Hermes lists every installed skill in the system prompt. With a few skills that is fine. With thousands, the list costs context on every turn, and the model still has to guess which skill fits. Triage removes the list. Skills live in a searchable catalog, and a skill's text enters a turn only when you attach it or your message clearly asks for it.

Plugin only: Triage does not patch Hermes. How everything behaves, with diagrams: [`docs/designs/how-triage-works.md`](docs/designs/how-triage-works.md).

## What you see in Hermes Desktop

- **Chips above the prompt.** As you type, Triage ranks the catalog and suggests skills, each with a score. No model is involved; ranking 2,500 skills takes about 0.2 s.
- **Click to hold, Enter to load.** A clicked chip becomes a pill on the left pile. Sending the message loads every held skill, and the pills fly to the fan on the right.
- **The fan shows what is loaded.** Skills loaded in this chat sit on the right, including ones the model or an older turn loaded. Hover a chip to read the skill's card.
- **Chips follow the conversation.** With an empty prompt the row shows skills for what the chat is about, not only for what you type.
- **Auto-attach only on evidence.** "do you have access to firebase here?" attaches `firebase` by itself. "let's go with the second option" does not attach `go`.
- **Trash and lock.** The trash button clears the pile and locks it for this message, so nothing attaches by itself.

## Searching for a skill

There are two ways, and both search every skill in the catalog.

1. **Type `/` in the prompt.** Type `/` and a few letters at the end of your message, for example `/fire`. The chip row above the prompt turns into search results, marked `/` instead of a score. Click one to hold it; the `/fire` text leaves the prompt. Hermes' own `/` menu opens at the same time and stays on top where it overlaps the chips.
2. **Open the search box.** Click the magnifier left of the chips. A list opens with a search field. With the field empty it shows your **Favourites**, your **Most used** skills and **Suggested** ones. Star a row to make it a favourite. Typing searches all skills; click a row or press Enter to hold it.

## Long chats and compression

- When Hermes compresses a chat, the skills whose text left the context leave the fan, and their chips can be picked again.
- **Keep** on a skill's card marks it to come back: after a compression it returns to the left pile and rides with your next message.
- The chips keep following what you wrote. Hermes' own notes in the chat (the compression summary, "model changed" and "interrupted" notes) are not read as your words.
- When Hermes gives a compressed or resumed chat a new id, Triage still finds its skills.

## For the model

The model gets two small tools, `skill_search` and `skill_load`, so it can find and load a skill itself. Hermes' own `skill_view` stays blocked for a skill until it was loaded in that chat, so the model cannot read every skill's text by name.

## Install

Needs Hermes 0.23.0 or newer. The repository root is the plugin.

```bash
mkdir -p ~/.hermes/plugins
cp -a . ~/.hermes/plugins/triage
hermes plugins enable triage
```

Restart the gateway. For the chips in Hermes Desktop, link `desktop-plugins\triage` to this repository's `desktop` folder and press Ctrl+R in Hermes ([`DEVELOPMENT.md`](DEVELOPMENT.md), Step 2b).

Then add your skill folders under **Settings → Plugins → triage → Skill folders**. They are indexed where they live and kept in sync as you edit them; nothing is copied.

Before uninstalling, run `python run.py restore` in the plugin folder. It gives Hermes back the skills Triage hid from it.

## Try it without Hermes

```bash
python run.py migrate --from examples/demo-skills --db /tmp/triage.db
python run.py peek --db /tmp/triage.db review the commit message
```

```text
No-skill probability: 45%.
1. 45% git-commit — name commit; description commit, message
2. 10% pr-review — name review; description review
```

## When a skill attaches itself

The draft must score 0.90 or more and give evidence in its prose. Paths, `@file` references, code and URLs do not count. Evidence is one of:

- the full name of a skill whose name has two or more words (`android-cli`);
- words of the skill's name or description that are rare in your catalog.

A skill that attached itself leaves again when the draft stops supporting it. Skills you attached yourself are never removed for you.

## Measured

`python run.py eval` scores the ranking on labelled drafts. On 94 real drafts against 2,586 skills:

| Measure | Result |
| --- | --- |
| Right skill is the first chip | 24 of 43 |
| Right skill is in the top five | 30 of 43 |
| A skill attached itself and was right | 15 of 20 (27 of 63 before the evidence rule) |
| Drafts that want no skill, but got one | 1 of 51 (9 of 51 before) |

## Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `auto_accept_threshold` | `0.90` | Score at which a skill may attach itself. `0` turns it off. |
| `shortlist_size` | `5` | Chips in the row (1 to 8). |
| `max_attached` | `0` | Cap on held skills per message. `0` = no limit. |
| `skill_folders` | empty | Folders to index and keep in sync (`;`-separated). |
| `always_load` | empty | Skills loaded on every chat's first prompt. |
| `model_tools` | `true` | Give the model `skill_search` and `skill_load`. |
| `hide_hermes_skills` | `true` | Hide Hermes' own skills from Hermes while Triage has them. |
| `strip_skills_index` | `true` | Remove a leftover skills list from outgoing requests. |
| `embedder` | empty | Optional class that adds meaning-based ranking. |
| `debug` | `false` | Write `[triage-diag]` lines to Hermes' `desktop.log`. |

## Tests

```bash
python -m unittest discover -s tests
```

141 tests, standard library only. For testers:

- [`docs/testing/feature-checklist.md`](docs/testing/feature-checklist.md): one check for every feature, on screen and off.
- [`docs/testing/conversation-tests.md`](docs/testing/conversation-tests.md): scenarios with a real model in a real conversation.
- [`docs/testing/photoshoot.md`](docs/testing/photoshoot.md): one screenshot per feature, for READMEs and banners.

## Limitations

- A request that shares no words with a skill finds nothing, unless you add an embedder.
- Skills named after a common word (`commit`, `documentation`) are suggested but do not attach themselves.
- After a change of topic, the earlier topic's skill can stay on top for a while.
- The first message of a brand-new chat takes the newest pile saved in the last 10 minutes; two windows starting chats at once could swap piles.
- A redesign of Hermes' composer can move the chip row. Loading skills does not depend on it.

## License

MIT. See [LICENSE](LICENSE).
