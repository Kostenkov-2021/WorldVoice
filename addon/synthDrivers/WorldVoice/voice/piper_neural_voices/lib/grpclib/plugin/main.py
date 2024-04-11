import os
import sys

from typing import List, Any, Collection, Iterator, NamedTuple, cast
from typing import Dict, Tuple, Optional, Deque
from contextlib import contextmanager
from collections import deque

from google.protobuf.descriptor_pb2 import FileDescriptorProto, DescriptorProto
from google.protobuf.compiler.plugin_pb2 import CodeGeneratorRequest
from google.protobuf.compiler.plugin_pb2 import CodeGeneratorResponse

from .. import const
from .. import client
from .. import server


_CARDINALITY = {
    (False, False): const.Cardinality.UNARY_UNARY,
    (True, False): const.Cardinality.STREAM_UNARY,
    (False, True): const.Cardinality.UNARY_STREAM,
    (True, True): const.Cardinality.STREAM_STREAM,
}


class Method(NamedTuple):
    name: str
    cardinality: const.Cardinality
    request_type: str
    reply_type: str


class Service(NamedTuple):
    name: str
    methods: List[Method]


class Buffer:
    def __init__(self) -> None:
        self._lines: List[str] = []
        self._indent = 0

    def add(self, string: str, *args: Any, **kwargs: Any) -> None:
        line = " " * self._indent * 4 + string.format(*args, **kwargs)
        self._lines.append(line.rstrip(" "))

    @contextmanager
    def indent(self) -> Iterator[None]:
        self._indent += 1
        try:
            yield
        finally:
            self._indent -= 1

    def content(self) -> str:
        return "\n".join(self._lines) + "\n"


def render(
    proto_file: str,
    package: str,
    imports: Collection[str],
    services: Collection[Service],
) -> str:
    buf = Buffer()
    buf.add("# Generated by the Protocol Buffers compiler. DO NOT EDIT!")
    buf.add("# source: {}", proto_file)
    buf.add("# plugin: {}", __name__)
    if not services:
        return buf.content()

    buf.add("import abc")
    buf.add("import typing")
    buf.add("")
    buf.add("import {}", const.__name__)
    buf.add("import {}", client.__name__)
    buf.add("if typing.TYPE_CHECKING:")
    with buf.indent():
        buf.add("import {}", server.__name__)

    buf.add("")
    for mod in imports:
        buf.add("import {}", mod)
    for service in services:
        if package:
            service_name = "{}.{}".format(package, service.name)
        else:
            service_name = service.name
        buf.add("")
        buf.add("")
        buf.add("class {}Base(abc.ABC):", service.name)
        with buf.indent():
            for (name, _, request_type, reply_type) in service.methods:
                buf.add("")
                buf.add("@abc.abstractmethod")
                buf.add(
                    "async def {}(self, stream: '{}.{}[{}, {}]') -> None:",
                    name,
                    server.__name__,
                    server.Stream.__name__,
                    request_type,
                    reply_type,
                )
                with buf.indent():
                    buf.add("pass")
            buf.add("")
            buf.add(
                "def __mapping__(self) -> typing.Dict[str, {}.{}]:",
                const.__name__,
                const.Handler.__name__,
            )
            with buf.indent():
                buf.add("return {{")
                with buf.indent():
                    for method in service.methods:
                        name, cardinality, request_type, reply_type = method
                        full_name = "/{}/{}".format(service_name, name)
                        buf.add(
                            "'{}': {}.{}(",
                            full_name,
                            const.__name__,
                            const.Handler.__name__,
                        )
                        with buf.indent():
                            buf.add("self.{},", name)
                            buf.add(
                                "{}.{}.{},",
                                const.__name__,
                                const.Cardinality.__name__,
                                cardinality.name,
                            )
                            buf.add("{},", request_type)
                            buf.add("{},", reply_type)
                        buf.add("),")
                buf.add("}}")

        buf.add("")
        buf.add("")
        buf.add("class {}Stub:", service.name)
        with buf.indent():
            buf.add("")
            buf.add(
                "def __init__(self, channel: {}.{}) -> None:".format(
                    client.__name__, client.Channel.__name__
                )
            )
            with buf.indent():
                if len(service.methods) == 0:
                    buf.add("pass")
                for method in service.methods:
                    name, cardinality, request_type, reply_type = method
                    full_name = "/{}/{}".format(service_name, name)
                    method_cls: type
                    if cardinality is const.Cardinality.UNARY_UNARY:
                        method_cls = client.UnaryUnaryMethod
                    elif cardinality is const.Cardinality.UNARY_STREAM:
                        method_cls = client.UnaryStreamMethod
                    elif cardinality is const.Cardinality.STREAM_UNARY:
                        method_cls = client.StreamUnaryMethod
                    elif cardinality is const.Cardinality.STREAM_STREAM:
                        method_cls = client.StreamStreamMethod
                    else:
                        raise TypeError(cardinality)
                    method_cls = cast(type, method_cls)  # FIXME: redundant
                    buf.add(
                        "self.{} = {}.{}(".format(
                            name, client.__name__, method_cls.__name__
                        )
                    )
                    with buf.indent():
                        buf.add("channel,")
                        buf.add("{!r},".format(full_name))
                        buf.add("{},", request_type)
                        buf.add("{},", reply_type)
                    buf.add(")")
    return buf.content()


def _get_proto(request: CodeGeneratorRequest, name: str) -> FileDescriptorProto:
    return next(f for f in request.proto_file if f.name == name)


def _strip_proto(proto_file_path: str) -> str:
    for suffix in [".protodevel", ".proto"]:
        if proto_file_path.endswith(suffix):
            return proto_file_path[: -len(suffix)]

    return proto_file_path


def _base_module_name(proto_file_path: str) -> str:
    basename = _strip_proto(proto_file_path)
    return basename.replace("-", "_").replace("/", ".")


def _proto2pb2_module_name(proto_file_path: str) -> str:
    return _base_module_name(proto_file_path) + "_pb2"


def _proto2grpc_module_name(proto_file_path: str) -> str:
    return _base_module_name(proto_file_path) + "_grpc"


def _type_names(
    proto_file: FileDescriptorProto,
    message_type: DescriptorProto,
    parents: Optional[Deque[str]] = None,
) -> Iterator[Tuple[str, str]]:
    if parents is None:
        parents = deque()

    proto_name_parts = [""]
    if proto_file.package:
        proto_name_parts.append(proto_file.package)
    proto_name_parts.extend(parents)
    proto_name_parts.append(message_type.name)

    py_name_parts = [_proto2pb2_module_name(proto_file.name)]
    py_name_parts.extend(parents)
    py_name_parts.append(message_type.name)

    yield ".".join(proto_name_parts), ".".join(py_name_parts)

    parents.append(message_type.name)
    for nested in message_type.nested_type:
        yield from _type_names(proto_file, nested, parents=parents)
    parents.pop()


def main() -> None:
    with os.fdopen(sys.stdin.fileno(), "rb") as inp:
        request = CodeGeneratorRequest.FromString(inp.read())

    types_map: Dict[str, str] = {}
    for pf in request.proto_file:
        for mt in pf.message_type:
            types_map.update(_type_names(pf, mt))

    response = CodeGeneratorResponse()

    # See https://github.com/protocolbuffers/protobuf/blob/v3.12.0/docs/implementing_proto3_presence.md  # noqa
    if hasattr(CodeGeneratorResponse, "Feature"):
        response.supported_features = CodeGeneratorResponse.FEATURE_PROTO3_OPTIONAL

    for file_to_generate in request.file_to_generate:
        proto_file = _get_proto(request, file_to_generate)

        imports = [
            _proto2pb2_module_name(dep)
            for dep in list(proto_file.dependency) + [file_to_generate]
        ]

        services = []
        for service in proto_file.service:
            methods = []
            for method in service.method:
                cardinality = _CARDINALITY[
                    (method.client_streaming, method.server_streaming)
                ]
                methods.append(
                    Method(
                        name=method.name,
                        cardinality=cardinality,
                        request_type=types_map[method.input_type],
                        reply_type=types_map[method.output_type],
                    )
                )
            services.append(Service(name=service.name, methods=methods))

        file = response.file.add()
        module_name = _proto2grpc_module_name(file_to_generate)
        file.name = module_name.replace(".", "/") + ".py"
        file.content = render(
            proto_file=proto_file.name,
            package=proto_file.package,
            imports=imports,
            services=services,
        )

    with os.fdopen(sys.stdout.fileno(), "wb") as out:
        out.write(response.SerializeToString())
