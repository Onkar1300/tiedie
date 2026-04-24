"""Generate gRPC Python stubs for the TieDie AP contract.

This script is the canonical way to regenerate stubs for:
- Gateway (tiedie/gateway/grpc_proto)
- Pi AP service (tiedie/pi-ap/grpc_proto)

Run from repo root:
    python tiedie/proto/generate_stubs.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
import importlib.resources
import re


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _grpc_tools_proto_include() -> str:
    try:
        return str(importlib.resources.files("grpc_tools").joinpath("_proto"))
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "grpcio-tools is required. Install with: pip install grpcio grpcio-tools"
        ) from exc


def _generate(proto_path: Path, out_dir: Path) -> None:
    from grpc_tools import protoc  # type: ignore

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "__init__.py").touch(exist_ok=True)

    include_dir = _grpc_tools_proto_include()
    proto_root = proto_path.parent

    args = [
        "protoc",
        f"-I{proto_root}",
        f"-I{include_dir}",
        f"--python_out={out_dir}",
        f"--grpc_python_out={out_dir}",
        f"--pyi_out={out_dir}",
        str(proto_path),
    ]

    rc = protoc.main(args)
    if rc != 0:
        raise SystemExit(rc)

    _fix_grpc_relative_imports(out_dir)


def _fix_grpc_relative_imports(out_dir: Path) -> None:
    """Make generated grpc stub imports package-relative.

    grpcio-tools can emit `import foo_pb2 as foo__pb2` which breaks when
    importing from within a package (it expects `from . import foo_pb2`).
    """

    for path in out_dir.glob("*_pb2_grpc.py"):
        text = path.read_text(encoding="utf-8")

        def _rewrite(match: re.Match[str]) -> str:
            module = match.group("module")
            alias = match.group("alias")
            return f"from . import {module} as {alias}"

        updated = re.sub(
            r"^import (?P<module>\w+_pb2) as (?P<alias>\w+)$",
            _rewrite,
            text,
            flags=re.MULTILINE,
        )

        if updated != text:
            path.write_text(updated, encoding="utf-8")


def main() -> None:
    root = _repo_root()
    proto_path = root / "tiedie" / "proto" / "access_point.proto"

    gateway_out = root / "tiedie" / "gateway" / "grpc_proto"
    pi_out = root / "tiedie" / "pi-ap" / "grpc_proto"

    if not proto_path.exists():
        raise FileNotFoundError(proto_path)

    _generate(proto_path, gateway_out)
    _generate(proto_path, pi_out)

    print("Generated stubs:")
    print(f"- {gateway_out}")
    print(f"- {pi_out}")


if __name__ == "__main__":
    # Ensure relative execution works.
    os.chdir(_repo_root())
    sys.path.insert(0, str(_repo_root()))
    main()
