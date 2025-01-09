import dataclasses
import json
import logging
import re
import subprocess
import tomllib
from pathlib import Path
from typing import NotRequired, TypedDict, cast

PKG_NAME_RE = re.compile(r"^([-a-zA-Z\d]+)(\[[-a-zA-Z\d,]+])?[^;]*(;.*)?$")

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class Batch:
    name: str | None
    optional: bool
    dependencies: list[str]


def pip():
    args = ["uv", "pip", "list", "--format", "json"]
    out = subprocess.check_output(args)
    return {x["name"]: x for x in json.loads(out)}


def uv(subcommand: str, packages: list[str], group: str | None, optional: bool):
    extra_arguments: list[str] = []
    if group:
        if optional:
            extra_arguments.extend(["--optional", group])
        else:
            extra_arguments.extend(["--group", group])

    args = ["uv", subcommand, *packages, "--no-sync"] + extra_arguments
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


class ToolUVDict(TypedDict):
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

    batches: list[Batch] = []

    sources: dict[str, ToolUVSourceDict] = {}
    if "tool" in pyproject and "uv" in pyproject["tool"] and "sources" in pyproject["tool"]["uv"]:
        sources = pyproject["tool"]["uv"]["sources"]

    if "dependencies" in pyproject["project"]:
        if len(pyproject["project"]["dependencies"]) > 0:
            batches.append(Batch(None, False, pyproject["project"]["dependencies"]))

    if "optional-dependencies" in pyproject["project"]:
        for name, dependencies in pyproject["project"]["optional-dependencies"].items():
            batches.append(Batch(name, True, dependencies))

    if "dependency-groups" in pyproject:
        for name, dependencies in pyproject["dependency-groups"].items():
            batches.append(Batch(name, False, dependencies))

    for batch in batches:
        to_remove: list[str] = []
        to_add: list[str] = []
        to_add_source: list[str] = []
        print(f"Processing {batch.name or 'dependencies'} Optional: {batch.optional}")
        print(json.dumps(batch.dependencies, indent=4))
        for dependency in batch.dependencies:
            package_match = PKG_NAME_RE.match(dependency)
            assert package_match, f"invalid package name '{dependency}'"
            package, extras, constraint = package_match.groups()
            to_remove.append(package)
            if package in sources:
                source = sources[package]
                if "path" not in source:
                    logger.warning(f"Package {package} has a source but no path")
                    return
                to_add_source.append(package)
                to_add.append(source["path"])
            else:
                to_add.append(f"{package}{extras or ''}{constraint or ''}")
        uv("remove", to_remove, group=batch.name, optional=batch.optional)
        uv("add", to_add, group=batch.name, optional=batch.optional)

        to_add = []
        if len(to_add_source) > 0:
            pipinfo = pip()
            for package in to_add_source:
                if package not in pipinfo:
                    logger.warning(f"Package {package} not found in pip list")
                    continue
                to_add.append(f"{package}>={pipinfo[package]["version"]}")
            uv("add", to_add, group=batch.name, optional=batch.optional)
