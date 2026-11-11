"""The Desktop entry must stay a single file the Hermes loader will accept."""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "desktop" / "plugin.js"


class DesktopPluginTests(unittest.TestCase):
    def test_more_list_and_lock_replace_skip(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")
        for gone in ("No skill / skip", "hsj-skip", "skipKey", "function slashLeft", "hsj-menu-open", "MENU_MAX_H"):
            self.assertNotIn(gone, text)
        for present in (
            "hsj-more", "hsj-trash", "hsj-unlock", "hsj-menu-list", "icons.Trash2", "if (lockedRef.current) return",
            "PopoverContent", "variant: 'menu'", "hsj-menu-anchor", "disabled: held.length === 0",
        ):
            self.assertIn(present, text)

    def test_colours_come_from_the_host_theme(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{6}\b", text.split("function ensureStyles")[1].split("document.head.appendChild")[0]))
        self.assertNotIn("var(--ui-bg)", text)
        self.assertNotIn("var(--ui-bg,", text)
        self.assertNotIn("chipColor(index)", text)
        self.assertIn("icons.Zap", text)

    def test_imports_only_sdk_and_react(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")
        specifiers = re.findall(r"""(?:from|import)\s+['"]([^'"]+)['"]""", text)
        specifiers += re.findall(r"""from\s+['"]([^'"]+)['"]""", text)
        self.assertTrue(specifiers)
        self.assertEqual(set(specifiers), {"@hermes/plugin-sdk", "react"})
        self.assertNotIn("helpers.js", text)
        self.assertNotIn("react/jsx-runtime", text)
        self.assertNotIn("IN CONTEXT", text)
        self.assertNotIn("UNLOCKED", text)
        self.assertNotIn("hsj-drawer", text)
        self.assertIn("withoutLoaded", text)
        self.assertIn("hsj-suggest", text)
        self.assertIn("hsj-ctx", text)
        self.assertIn("hsj-fan", text)
        self.assertIn("COMPOSER_AREAS.top", text)
        self.assertIn("hsj-overlay", text)
        self.assertIn("document.body.appendChild", text)
        self.assertIn("elementsFromPoint", text)
        self.assertIn("composerCardFrom", text)
        self.assertIn("composerStackBox", text)
        self.assertIn("extendStackTop", text)
        self.assertIn("position: fixed", text)
        # catalog rule 8: no document-wide MutationObserver over Hermes' UI
        self.assertNotIn("observe(document.body", text)
        self.assertNotIn("subtree: true", text)
        self.assertNotIn("PromptEcho", text)
        self.assertNotIn("hsj-echo", text)
        self.assertIn("Escape", text)
        self.assertIn("hsj-held", text)
        self.assertIn("180ms ease-out", text)
        self.assertIn("zIndex: skills.length - index", text)
        self.assertNotIn("bottom: calc(100% + 10px)", text)
        chip = re.search(r"\.hsj-fan-chip \{([^}]+)\}", text)
        self.assertIsNotNone(chip)
        self.assertIn("position: absolute", chip.group(1))
        self.assertIn("gap: 6px", chip.group(1))
        api = (ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
        self.assertIn('"bridged"', api)

    def test_fan_v3_equal_chips_and_hover_strips_match_visible_ends(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")
        start = text.index("const FAN_MAX")
        end = text.index("function estimateChipWidth")
        mag = re.search(r"const MAG_WIDTH = \d+", text).group(0)
        script = mag + "\n" + text[start:end] + """
const check = (ok, msg) => { if (!ok) { console.error(msg); process.exit(1) } }
for (const bar of [0, 400, 640, 900, 1600]) {
  const max = fanMaxWidth(bar)
  check(max <= FAN_MAX && max >= FAN_MIN, 'fan width within bounds at ' + bar)
  if (bar >= 600) check(max + magazineWidth(bar) <= bar * 0.6, 'piles leave the middle to the chips at ' + bar)
}
check(fanMaxWidth(640) < fanMaxWidth(1600) && magazineWidth(640) < magazineWidth(1600), 'piles shrink with the window')
for (const max of [FAN_MIN, 220, FAN_MAX]) for (const n of [1, 2, 5, 20, 200]) {
  const g = fanLayout(n, max)
  check(g.width <= max + 0.01, 'never wider than its max: ' + n + ' @ ' + max)
  check(g.chip >= FAN_CHIP_MIN || g.chip === max, 'chips stay readable')
  check(g.lanes.length === n, 'one strip per chip')
  if (n > 1) {
    // strips tile the row exactly: no gap, no overlap, ending at the row's end
    for (let i = 1; i < n; i += 1) check(Math.abs(g.lanes[i].left - (g.lanes[i - 1].left + g.lanes[i - 1].width)) < 0.01, 'strips tile ' + n)
    check(Math.abs(g.lanes[n - 1].left + g.lanes[n - 1].width - g.width) < 0.01, 'last strip ends at the edge')
    for (let i = 2; i < n; i += 1) check(Math.abs(g.lanes[i].width - g.lanes[1].width) < 0.01, 'equal visible ends ' + n)
  }
}
const NAMES = ['plan', 'hermes-agent', 'goal-loop']
for (const max of [FAN_MIN, 220, FAN_MAX]) for (const names of [['plan'], NAMES, Array.from({ length: 40 }, (_, i) => 'skill-number-' + i)]) {
  const natural = names.map(fanChipWidth)
  const g = fanLayout(names.length, max, natural)
  check(g.width <= max + 0.01, 'fitted: never wider than its max')
  check(g.lanes.length === names.length && g.widths.length === names.length, 'fitted: one strip per chip')
  for (let i = 0; i < names.length; i += 1) {
    check(g.widths[i] <= natural[i] + 0.01, 'fitted: a chip is never wider than its name needs')
    check(g.left[i] >= -0.01, 'fitted: no chip sticks out on the left')
    check(Math.abs(g.left[i] + g.widths[i] - (g.lanes[i].left + g.lanes[i].width)) < 0.01, 'fitted: a chip ends where its strip ends')
    if (i) check(Math.abs(g.lanes[i].left - (g.lanes[i - 1].left + g.lanes[i - 1].width)) < 0.01, 'fitted: strips tile')
  }
}
const roomy = fanLayout(3, FAN_MAX, NAMES.map(fanChipWidth))
check(roomy.lanes.every((lane, i) => Math.abs(lane.width - fanChipWidth(NAMES[i])) < 0.01), 'short names show whole when there is room')
check(fanLayout(1, FAN_MAX, [fanChipWidth('plan')]).width < 60, 'a short name gets a small chip')
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_bar_clears_the_whole_composer_dock(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")
        self.assertIn('[data-slot="composer-dock"]', text)
        self.assertIn("DOCK_RISE_MAX", text)
        self.assertIn("layout.widths[index]", text)

    def test_jump_pill_rises_over_the_bar_and_scrolling_does_not_move_it(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")
        # The dock alone sets the bar's height: chat text behind it is ignored.
        self.assertIn("top = Math.min(dockTop, cardRect.top)", text)
        self.assertNotIn("if (dockTop < top &&", text)
        # Hermes' pill (found by place, the composer is not in its pane) is
        # lifted by the bar's height and reset when the bar goes.
        self.assertIn(".thread-jump-button { bottom:", text)
        self.assertIn("var(--hsj-jump-rise, 0px)", text)
        self.assertIn("pill.style.setProperty(JUMP_RISE, rise)", text)
        # Defined once, called when the bar leaves the screen and on cleanup.
        self.assertIn("function dropJumpRise()", text)
        self.assertEqual(text.count("dropJumpRise()"), 3)
        self.assertNotIn("composer-bounds", text)

    def test_fan_card_floats_above_and_takes_no_room(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")
        wrap = re.search(r"\.hsj-detail-wrap \{([^}]+)\}", text)
        self.assertIsNotNone(wrap)
        self.assertIn("position: absolute", wrap.group(1))
        self.assertIn("bottom: 100%", wrap.group(1))
        self.assertNotIn("rootWidth", text)  # the fan never widens for the card

    def test_fan_clears_chrome_stacked_on_the_prompt(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")
        start = text.index("function extendStackTop")
        end = text.index("function pushStackRect")
        script = text[start:end] + """
const card = { top: 620, bottom: 710, left: 200, right: 1100 }
const banner = { top: 576, bottom: 612, left: 200, right: 1100 }
const above = { top: 540, bottom: 568, left: 200, right: 1100 }
const transcript = { top: 80, bottom: 560, left: 200, right: 1100 }
const rail = { top: 560, bottom: 600, left: 0, right: 160 }
if (extendStackTop(card, [banner]) !== 576) process.exit(1)
if (extendStackTop(card, [banner, above]) !== 576) process.exit(1)
if (extendStackTop(card, [transcript]) !== 620) process.exit(1)
if (extendStackTop(card, [rail]) !== 620) process.exit(1)
const wrapped = stackTop(card, [], [{ top: 576, bottom: 710, left: 200, right: 1100 }])
if (wrapped !== 576) process.exit(1)
const column = stackTop(card, [], [{ top: 40, bottom: 900, left: 180, right: 1120 }])
if (column !== 620) process.exit(1)
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_host_floats_do_not_lift_the_bar(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")
        self.assertIn("function isHostFloat", text)
        self.assertIn("composer-completion-drawer", text)
        self.assertIn("node.style.clipPath", text)
        self.assertIn("(box.right - box.left) * 0.6", text)

    def test_list_has_stars_fixed_size_and_card_keep(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")
        for present in (
            "hsj-menu-star", "'/favorites'", "'/sticky'", "hsj-keep", "function rehold",
            "height: min(15rem, 56vh)", "'Favourites'", "'Most used'", "onKeep: keepSkill",
        ):
            self.assertIn(present, text)
        self.assertNotIn("max-height: min(", text.split(".hsj-menu-list {")[1].split("}")[0])
        self.assertNotIn("icons.Star", text)

    def test_magnifier_replaces_more_button(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")
        for gone in ("More (", "'All skills'", "hsj-held-sep", "moreCount", "fullMatches"):
            self.assertNotIn(gone, text)
        for present in ("title: 'Search all skills'", "'data-slot': 'command-input-wrapper'", ".slice(0, size)"):
            self.assertIn(present, text)


class LocalSearchTests(unittest.TestCase):
    def test_instant_search_prefix_initials_and_typos(self) -> None:
        text = PLUGIN.read_text(encoding="utf-8")

        def pick(name: str) -> str:
            start = text.index(f"function {name}(")
            depth = 0
            for index in range(text.index("{", start), len(text)):
                depth += {"{": 1, "}": -1}.get(text[index], 0)
                if depth == 0:
                    return text[start:index + 1]
            raise AssertionError(name)

        helpers = "\n".join(pick(name) for name in ("buildNameIndex", "trigrams", "subsequenceScore", "partsAnyOrder", "editDistance", "localSearch"))
        script = helpers + """
const list = buildNameIndex([['senior-architect', ''], ['android-dev', ''], ['android-cli', ''], ['andrej-karpathy', ''], ['kotlin', ''],
  ['kotlin-coroutines-expert', ''], ['2d-games', ''], ['3d-games', '']])
const names = (q) => localSearch(q, list, [], 5).map((row) => row.name)
const check = (ok, msg) => { if (!ok) { console.error(msg); process.exit(1) } }
check(names('2d')[0] === '2d-games', '2d prefix')
check(names('architec-senior')[0] === 'senior-architect', 'parts in any order: ' + names('architec-senior'))
check(names('cli-andriod')[0] === 'android-cli', 'any order with a typo: ' + names('cli-andriod'))
check(names('triage:2d-gam')[0] === '2d-games', 'triage: prefix stripped')
check(names('kce').includes('kotlin-coroutines-expert'), 'initials')
check(names('andriod')[0].startsWith('android'), 'typo: ' + names('andriod'))
check(!localSearch('kotlin', list, ['kotlin'], 5).some((row) => row.name === 'kotlin'), 'held skills excluded')
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
