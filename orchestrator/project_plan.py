from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectFeature:
    feature_id: str
    title: str
    status: str
    dependencies: tuple[str, ...]
    section: str


@dataclass(frozen=True)
class ProjectPlan:
    path: Path
    project: str
    active_feature_id: str | None
    active_status: str | None
    features: tuple[ProjectFeature, ...]
    raw: str

    @classmethod
    def load(cls, path: Path) -> "ProjectPlan":
        if not path.exists():
            raise FileNotFoundError(f"Project plan not found: {path}")
        raw = path.read_text(encoding="utf-8")
        project = _value(raw, r"^-s*\*\*Project:\*\*\s*(.+)$") or path.parent.name
        active_id = _value(raw, r"^-s*\*\*Feature ID:\*\*\s*([A-Z]+-\d+)")
        active_status = _value(raw, r"^-s*\*\*Status:\*\*\s*(TODO|IN_PROGRESS|BLOCKED|DONE|SKIPPED)")
        features = tuple(_parse_features(raw))
        return cls(path, project, active_id, active_status, features, raw)

    def next_feature(self) -> ProjectFeature | None:
        in_progress = [f for f in self.features if f.status == "IN_PROGRESS"]
        if in_progress:
            return in_progress[0]
        done = {f.feature_id for f in self.features if f.status == "DONE"}
        for feature in self.features:
            if feature.status == "TODO" and all(dep in done for dep in feature.dependencies):
                return feature
        return None

    def task_context(self, feature: ProjectFeature) -> str:
        heading = f"### {feature.feature_id} — {feature.title}"
        match = re.search(re.escape(heading) + r"(.*?)(?=\n###\s+F-\d+\s+—|\n---\n|\Z)", self.raw, re.S)
        section = match.group(1).strip() if match else feature.section
        return (
            f"PROJECT: {self.project}\n"
            f"PROJECT PLAN: {self.path.name}\n"
            f"ACTIVE FEATURE: {feature.feature_id}\n"
            f"FEATURE STATUS: {feature.status}\n"
            f"DEPENDENCIES: {', '.join(feature.dependencies) or 'None'}\n\n"
            f"FEATURE SPECIFICATION:\n{section}\n\n"
            "EXECUTION CONTRACT:\n"
            "- Work only on the active feature.\n"
            "- Do not search for or implement unrelated TODOs.\n"
            "- Do not redesign completed features.\n"
            "- Inspect only relevant files first.\n"
            "- Implement, test, fix, and verify the acceptance checks.\n"
            "- Do not mark the feature DONE until its acceptance checks pass.\n"
            "- Preserve valid existing work.\n"
        )


def _value(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.M)
    return match.group(1).strip() if match else None


def _parse_features(text: str) -> list[ProjectFeature]:
    matches = list(re.finditer(r"^###\s+(F-\d+)\s+—\s+(.+)$", text, re.M))
    features: list[ProjectFeature] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[match.start():end].strip()
        status = _value(section, r"^-s*\*\*Status:\*\*\s*(TODO|IN_PROGRESS|BLOCKED|DONE|SKIPPED)") or "TODO"
        dep_value = _value(section, r"^-s*\*\*Dependencies:\*\*\s*(.+)$") or ""
        dependencies = tuple(re.findall(r"F-\d+", dep_value))
        features.append(ProjectFeature(match.group(1), match.group(2).strip(), status, dependencies, section))
    return features
