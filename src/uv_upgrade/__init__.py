import dataclasses
import json
import logging
import re
import subprocess
import tomllib
from pathlib import Path
from typing import NamedTuple, NotRequired, TypedDict, cast

from rich import print

PKG_NAME_RE = re.compile(r"^([\-a-zA-Z\d]+)(\[[-a-zA-Z\d,]+])?[^;]*(;.*)?$")

logger = logging.getLogger(__name__)


class Dependency(NamedTuple):
    name: str
    extra: str
    constraint: str

    @classmethod
    def from_line(cls, line: str):
        package_match = PKG_NAME_RE.match(line)
        assert package_match, f"invalid package name '{line}'"
        package, extras, constraint = package_match.groups()
        return cls(package, extras, constraint)

    def __str__(self):
        return f"{self.name}{self.extra or ''}{self.constraint or ''}"


class Extra(NamedTuple):
    name: str


class Group(NamedTuple):
    name: str


class Index(NamedTuple):
    name: str
    url: str


@dataclasses.dataclass(slots=True, frozen=True)
class Source:
    pass


@dataclasses.dataclass(slots=True, frozen=True)
class IndexSource(Source):
    index: Index

    def __str__(self):
        return f"{self.index.name}={self.index.url}"


@dataclasses.dataclass(slots=True, frozen=True)
class GitSource(Source):
    url: str

    def __str__(self):
        return f"git+{self.url}"


@dataclasses.dataclass
class Deps:
    dependencies: dict[Extra | Group | None, list[str]]
    sources: dict[str, Source]


def lockinfo():
    with open("uv.lock", "rb") as fp:
        return {p["name"]: p for p in tomllib.load(fp)["package"]}


def pip():
    args = ["uv", "pip", "list", "--format", "json"]
    print(args)
    out = subprocess.check_output(args)
    return {x["name"]: x for x in json.loads(out)}


def uv_remove(packages: list[Dependency], group: Extra | Group | None = None):
    extra_arguments: list[str] = []
    if isinstance(group, Extra):
        extra_arguments.extend(["--optional", group.name])
    elif isinstance(group, Group):
        extra_arguments.extend(["--group", group.name])

    args = ["uv", "remove", *(str(x) for x in packages), "--no-sync", "--no-cache"] + extra_arguments
    logger.info(f"Running: {' '.join(args)}")
    subprocess.check_call(args)


def uv_add(packages: list[Dependency], group: Extra | Group | None = None, sources: dict[str, Source] | None = None):
    sources = sources or {}

    extra_arguments: list[str] = []
    if isinstance(group, Extra):
        extra_arguments.extend(["--optional", group.name])
    elif isinstance(group, Group):
        extra_arguments.extend(["--group", group.name])

    normal_deps = [dep for dep in packages if dep.name not in sources]
    special_deps = [dep for dep in packages if dep.name in sources]

    for dep in special_deps:
        source = sources[dep.name]
        if isinstance(source, IndexSource):
            args = ["uv", "add", str(dep), "--index", str(source), "--no-sync", "--no-cache"] + extra_arguments
        elif isinstance(source, GitSource):
            args = ["uv", "add", str(source), "--no-sync", "--no-cache"] + extra_arguments
        else:
            raise ValueError(f"Unknown source type {source}")
        logger.info(f"Running: {' '.join(args)}")
        subprocess.check_call(args)

    if len(normal_deps) > 0:
        args = ["uv", "add", *(str(x) for x in normal_deps), "--no-sync", "--no-cache"] + extra_arguments
        logger.info(f"Running: {' '.join(args)}")
        subprocess.check_call(args)


ProjectDict = TypedDict(
    "ProjectDict",
    {
        "dependencies": NotRequired[list[str]],
        "optional-dependencies": NotRequired[dict[str, list[str]]],
    },
)


class ToolUVSourceDict(TypedDict):
    path: NotRequired[str]
    git: NotRequired[str]
    index: NotRequired[str]


class ToolUVSourceIndexDict(TypedDict):
    name: str
    url: str


class ToolUVDict(TypedDict):
    index: NotRequired[list[ToolUVSourceIndexDict]]
    sources: NotRequired[dict[str, ToolUVSourceDict]]


class ToolDict(TypedDict):
    uv: NotRequired[ToolUVDict]


PyProject = TypedDict(
    "PyProject",
    {"project": ProjectDict, "dependency-groups": NotRequired[dict[str, list[str]]], "tool": NotRequired[ToolDict]},
)


def load_pyproject() -> PyProject:
    return cast(PyProject, tomllib.loads(Path("pyproject.toml").read_text()))


def main():
    """WARNING:
    from the `pyproject.toml` file, this may delete:
        - comments
        - upper bounds etc
        - markers
        - ordering of dependencies
    """

    logging.basicConfig(level=logging.INFO)
    pyproject = load_pyproject()

    print(pip())

    indexes: dict[str, Index] = {}
    if "tool" in pyproject and "uv" in pyproject["tool"] and "index" in pyproject["tool"]["uv"]:
        for index in pyproject["tool"]["uv"]["index"]:
            indexes[index["name"]] = Index(index["name"], index["url"])

    sources: dict[str, Source] = {}
    if "tool" in pyproject and "uv" in pyproject["tool"] and "sources" in pyproject["tool"]["uv"]:
        for name, source in pyproject["tool"]["uv"]["sources"].items():
            if "index" in source:
                sources[name] = IndexSource(indexes[source["index"]])
            elif "git" in source:
                sources[name] = GitSource(source["git"])
            else:
                raise ValueError(f"Unknown source type {source}")

    dependency_groups: dict[Extra | Group | None, list[Dependency]] = {}

    if "dependencies" in pyproject["project"] and len(pyproject["project"]["dependencies"]) > 0:
        dependency_groups[None] = [Dependency.from_line(dep) for dep in pyproject["project"]["dependencies"]]

    if "optional-dependencies" in pyproject["project"]:
        for name, dependencies in pyproject["project"]["optional-dependencies"].items():
            dependency_groups[Extra(name)] = [Dependency.from_line(dep) for dep in dependencies]

    if "dependency-groups" in pyproject:
        for name, dependencies in pyproject["dependency-groups"].items():
            dependency_groups[Group(name)] = [Dependency.from_line(dep) for dep in dependencies]

    for group, dependencies in dependency_groups.items():
        uv_remove(dependencies, group=group)

    for group, dependencies in dependency_groups.items():
        uv_add(dependencies, group=group, sources=sources)
