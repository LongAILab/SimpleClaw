"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
from pathlib import Path

from simpleclaw.config.paths import get_base_workspace_path, resolve_workspace_root

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(
        self,
        workspace: Path,
        builtin_skills_dir: Path | None = None,
        shared_workspace: Path | None = None,
    ):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        if shared_workspace is None:
            root = resolve_workspace_root(workspace)
            candidate = get_base_workspace_path(root)
            shared_workspace = candidate if candidate != workspace else None
        self.shared_workspace = shared_workspace
        self.shared_skills = shared_workspace / "skills" if shared_workspace is not None else None
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR

    def _prompt_skill_locations(self) -> list[tuple[str, Path]]:
        """Return prompt-visible skill directories in precedence order."""
        locations: list[tuple[str, Path]] = [("workspace", self.workspace_skills)]
        if self.shared_skills is not None:
            locations.append(("shared", self.shared_skills))
        return locations

    def _skill_locations(self) -> list[tuple[str, Path]]:
        """Return all skill directories including builtin fallback."""
        locations = self._prompt_skill_locations()
        if self.builtin_skills is not None:
            locations.append(("builtin", self.builtin_skills))
        return locations

    def list_skills(
        self,
        filter_unavailable: bool = True,
        *,
        include_builtin: bool = False,
        source_filter: str | None = None,
    ) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = []
        seen_names: set[str] = set()

        locations = self._skill_locations() if include_builtin else self._prompt_skill_locations()
        for source, root in locations:
            if not root.exists():
                continue
            for skill_dir in sorted(root.iterdir(), key=lambda path: path.name):
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if not skill_file.exists() or skill_dir.name in seen_names:
                        continue
                    seen_names.add(skill_dir.name)
                    if source_filter is None or source == source_filter:
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": source})

        # Filter by requirements
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        for _, root in self._skill_locations():
            skill_file = root / name / "SKILL.md"
            if skill_file.exists():
                return skill_file.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                content = self._strip_leading_heading(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self, source_filter: str | None = None) -> str:
        """
        Build a concise summary of prompt-visible skills.

        This is used for progressive loading - the agent can inspect the full
        skill content only when needed.

        Returns:
            Markdown bullet list.
        """
        all_skills = self.list_skills(filter_unavailable=False, source_filter=source_filter)
        if not all_skills:
            return ""

        always_skills = set(self.get_always_skills(source_filter=source_filter))
        lines = []
        for s in all_skills:
            name = s["name"]
            if name in always_skills:
                continue
            desc = self._get_skill_description(name)
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                suffix = f" [requires: {missing}]" if missing else " [unavailable]"
            else:
                suffix = ""
            lines.append(f"- `{name}`: {desc}{suffix}")

        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _strip_leading_heading(self, content: str) -> str:
        """Drop the first top-level markdown heading to avoid repeated titles in prompts."""
        lines = content.splitlines()
        if lines and lines[0].startswith("# "):
            return "\n".join(lines[1:]).strip()
        return content

    def _parse_skill_metadata(self, raw: str) -> dict:
        """Parse skill metadata JSON from frontmatter (supports simpleclaw, openclaw, and legacy nanobot keys)."""
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            return data.get("simpleclaw", data.get("openclaw", data.get("nanobot", {})))
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """Get simpleclaw metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_skill_metadata(meta.get("metadata", ""))

    def get_always_skills(self, source_filter: str | None = None) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True, source_filter=source_filter):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_skill_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # Simple YAML parsing
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata

        return None
