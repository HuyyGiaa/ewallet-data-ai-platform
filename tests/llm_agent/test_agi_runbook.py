"""Agent integration: the AGI evidence runbook is executable as written (static checks only).

- every ``bash`` block parses (``bash -n``) after the list indentation is removed;
- every repository script or module it calls exists, and every ``--flag`` passed to
  the repository's own tools is accepted by that tool's ``--help``;
- mutating commands appear only in blocks marked ``[A1]`` or ``[A2]`` or behind the
  campaign script's approval string;
- the rubric mapping never presents AGI evidence as Substrate (row 27) or
  autoscaling proof.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/runbooks/edai2/AGENT_INTEGRATION_EVIDENCE_RUNBOOK.md"
CONTRACTS = ROOT / "archive/legacy-docs/docs/edai2/agents/AGENT_INTEGRATION_CONTRACTS.md"   # archived AGI-era contract map
FENCE = re.compile(r"^( *)```bash\n(.*?)^\1```", re.S | re.M)
TOOLS = {
    "scripts/llm_agent/agi_probe.py": ("script", ("request", "mcp", "a2a-route")),
    "scripts/llm_agent/g3_a2a_smoke.py": ("script", ()),
    "ops.llm_agent.drift_artifacts": ("module", ("baseline", "window", "describe")),
    "ops.llm_agent.agi_inputs": ("module", ("redis-secret", "drift-configmaps")),
    "ops.llm_agent.registry_catalog": ("module", ()),
}
MUTATING = re.compile(r"\b(helm [^|;]*\b(upgrade|rollback|install|uninstall)\b(?![^|;]*--dry-run)|"
                      r"kubectl [^|;]*\b(apply|create|delete|patch|scale|edit)\b|scan_and_push\.sh|"
                      r"--approve\b|\bpublish\b --registry)")


def blocks() -> list[tuple[str, str]]:
    """(preceding text, dedented code) for every bash fence."""

    text = RUNBOOK.read_text(encoding="utf-8")
    result = []
    for match in FENCE.finditer(text):
        indent = match.group(1)
        code = "\n".join(line[len(indent):] if line.startswith(indent) else line
                         for line in match.group(2).splitlines())
        result.append((text[max(0, match.start() - 1500):match.start()], code))
    return result


def help_text(tool: str, sub: str | None) -> str:
    kind, _ = TOOLS[tool]
    command = [sys.executable, str(ROOT / tool)] if kind == "script" else [sys.executable, "-m", tool]
    command += ([sub] if sub else []) + ["--help"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, env={"PYTHONPATH": str(ROOT),
                                                                                       "PATH": "/usr/bin:/bin"})
    return result.stdout + result.stderr


class RunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blocks = blocks()

    def test_every_bash_block_parses(self):
        self.assertGreater(len(self.blocks), 15)
        for _, code in self.blocks:
            with self.subTest(code=code.splitlines()[0][:80]):
                result = subprocess.run(["bash", "-n"], input=code, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_referenced_repository_paths_exist(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        for path in sorted(set(re.findall(r"(?<![\w/.-])((?:scripts|ops|infra|docs)/[\w./-]+\.(?:sh|py|yaml|json|md))", text))):
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).exists(), path)
        for module in sorted(set(re.findall(r"-m (ops\.llm_agent\.\w+)", text))):
            with self.subTest(module=module):
                self.assertTrue((ROOT / (module.replace(".", "/") + ".py")).exists(), module)

    def test_flags_passed_to_repository_tools_exist(self):
        cache: dict[tuple[str, str | None], str] = {}
        code = "\n".join(code for _, code in self.blocks).replace("\\\n", " ")
        statements = re.split(r"\n|;|\(|\)", code)
        for statement in statements:
            for tool, (_, subcommands) in TOOLS.items():
                position = statement.find(tool)
                if position < 0:
                    continue
                rest = statement[position + len(tool):].split()
                sub = rest[0] if rest and rest[0] in subcommands else None
                if subcommands and sub is None:
                    continue
                key = (tool, sub)
                if key not in cache:
                    cache[key] = help_text(tool, sub)
                for flag in re.findall(r"(?<!\S)(--[a-z][a-z0-9-]*)", " ".join(rest)):
                    if flag in ("--help",):
                        continue
                    with self.subTest(tool=tool, sub=sub, flag=flag):
                        self.assertIn(flag, cache[key])

    def test_campaign_script_commands_are_real_subcommands(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        script = (ROOT / "scripts/llm_agent/agi_campaign.sh").read_text(encoding="utf-8")
        for command in sorted(set(re.findall(r"\$C (\w[\w-]*)", text))):
            with self.subTest(command=command):
                self.assertRegex(script, rf"\n    {re.escape(command)}\) ")

    def test_mutations_only_in_approved_blocks(self):
        for context, code in self.blocks:
            marker = context[context.rfind("**"):] if "**" in context else context[-400:]
            heading = context[context.rfind("\n#"):] if "\n#" in context else ""
            approved = "[A1]" in marker or "[A2]" in marker or "[A1]" in heading or "[A2]" in heading
            for line in code.splitlines():
                if line.strip().startswith("#"):
                    continue
                if MUTATING.search(line):
                    with self.subTest(line=line.strip()[:100]):
                        self.assertTrue(approved or "AGI_APPROVAL=A" in line,
                                        "mutating command outside an [A1]/[A2] block")

    def test_mandatory_gates_run_through_the_fail_closed_script(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertNotIn("helm_object_diff.py", text, "diffs run inside agi_campaign.sh, never piped through tee")
        self.assertNotRegex(text, r"helm upgrade [^\n]*--atomic", "upgrades run only via agi_campaign.sh upgrade")
        self.assertIn("AGI_APPROVAL=A1:$CAMPAIGN $C upgrade mcp", text)
        self.assertIn("AGI_APPROVAL=A2:$CAMPAIGN $C rollback agents", text)
        for _, code in self.blocks:
            if code.lstrip().startswith("("):
                with self.subTest(code=code.splitlines()[0][:60]):
                    self.assertIn("set -euo pipefail", code.splitlines()[0])

    def test_rubric_mapping_does_not_overstate(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        agi14 = text[text.index("### AGI-14"):text.index("## Stage 5")]
        self.assertIn("**not** evidence for row 27", agi14)
        self.assertNotRegex(agi14, r"Rubric:[^\n]*\b25[–-]27\b")
        contracts = CONTRACTS.read_text(encoding="utf-8")
        matrix = contracts[contracts.index("## 8. Evidence-to-rubric matrix"):]
        separate = matrix[matrix.index("Requirements that stay separate"):]
        for row in ("18", "19", "20", "22", "25", "27", "34"):
            with self.subTest(row=row):
                self.assertRegex(separate, rf"\n\| {row}\b")
        agi_table = matrix[:matrix.index("Requirements that stay separate")]
        agi_rows = [line.split("|")[1:3] for line in agi_table.splitlines() if re.match(r"\| \d\d \|", line)]
        self.assertEqual(len(agi_rows), 14)
        for number, primary in agi_rows:
            with self.subTest(agi=number.strip()):
                self.assertNotRegex(primary, r"\b27\b", "no AGI item is primary evidence for Substrate")


@unittest.skipUnless(shutil.which("bash"), "bash required")
class CampaignScriptSyntaxTests(unittest.TestCase):
    def test_campaign_script_parses_and_documents_its_modes(self):
        script = ROOT / "scripts/llm_agent/agi_campaign.sh"
        self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)
        usage = subprocess.run(["bash", str(script)], capture_output=True, text=True)
        self.assertEqual(usage.returncode, 2)
        for mode in ("[READ-ONLY]", "[MUTATES, needs A1]", "[MUTATES, needs A2]"):
            self.assertIn(mode, usage.stdout)


if __name__ == "__main__":
    unittest.main()
