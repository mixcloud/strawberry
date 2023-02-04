import abc
import dataclasses
import inspect
import itertools
import sys
import uuid
from typing import (
    Any,
    Awaitable,
    ClassVar,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Sequence,
    Sized,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
)
from typing_extensions import Literal, get_args, get_origin

from strawberry.field import field
from strawberry.lazy_type import LazyType
from strawberry.object_type import interface, type
from strawberry.scalars import ID
from strawberry.type import StrawberryContainer
from strawberry.types.info import Info
from strawberry.types.types import TypeDefinition
from strawberry.utils.await_maybe import AwaitableOrValue

from .utils import from_base64, to_base64

_T = TypeVar("_T")
_R = TypeVar("_R")
connection_typename = "arrayconnection"

NodeType = TypeVar("NodeType", bound="Node")


class GlobalIDValueError(ValueError):
    """GlobalID value error, usually related to parsing or serialization."""


@dataclasses.dataclass(order=True, frozen=True)
class GlobalID:
    """Global ID for relay types.

    Different from `strawberry.ID`, this ID wraps the original object ID in a string
    that contains both its GraphQL type name and the ID itself, and encodes it
    to a base64_ string.

    This object contains helpers to work with that, including method to retrieve
    the python object type or even the encoded node itself.

    Attributes:
        type_name:
            The type name part of the id
        node_id:
            The node id part of the id

    .. _base64:
        https://en.wikipedia.org/wiki/Base64

    """

    _nodes_cache: ClassVar[Dict[Tuple[int, str], Type["Node"]]] = {}

    type_name: str
    node_id: str

    def __post_init__(self):
        if not isinstance(self.type_name, str):
            raise GlobalIDValueError(
                f"type_name is expected to be a string, found {repr(self.type_name)}"
            )
        if not isinstance(self.node_id, str):
            raise GlobalIDValueError(
                f"node_id is expected to be a string, found {repr(self.node_id)}"
            )

    def __str__(self):
        return to_base64(self.type_name, self.node_id)

    @classmethod
    def from_id(cls, value: Union[str, ID]):
        """Create a new GlobalID from parsing the given value.

        Args:
            value:
                The value to be parsed, as a base64 string in the
                "TypeName:NodeID" format

        Returns:
            An instance of GLobalID

        Raises:
            GlobalIDValueError:
                If the value is not in a GLobalID format

        """
        try:
            type_name, node_id = from_base64(value)
        except ValueError as e:
            raise GlobalIDValueError(str(e)) from e

        return cls(type_name=type_name, node_id=node_id)

    def resolve_type(self, info: Info) -> Type["Node"]:
        """Resolve the internal type name to its type itself.

        Args:
            info:
                The strawberry execution info resolve the type name from

        Returns:
            The resolved GraphQL type for the execution info

        """
        schema = info.schema
        # Put the schema in the key so that different schemas can have different types
        key = (id(schema), self.type_name)
        origin = self._nodes_cache.get(key)

        if origin is None:
            type_def = info.schema.get_type_by_name(self.type_name)
            assert isinstance(type_def, TypeDefinition)
            origin = type_def.origin
            if isinstance(origin, LazyType):
                origin = origin.resolve_type()
            assert issubclass(origin, Node)
            self._nodes_cache[key] = origin

        return origin

    @overload
    def resolve_node(
        self,
        info: Info,
        *,
        required: Literal[True] = ...,
        ensure_type: Type[_T],
    ) -> _T:
        ...

    @overload
    def resolve_node(
        self,
        info: Info,
        *,
        required: Literal[True] = ...,
        ensure_type: Awaitable[Type[_T]],
    ) -> Awaitable[_T]:
        ...

    @overload
    def resolve_node(
        self,
        info: Info,
        *,
        required: Literal[True],
        ensure_type: None = ...,
    ) -> AwaitableOrValue["Node"]:
        ...

    @overload
    def resolve_node(
        self,
        info: Info,
        *,
        required: bool = ...,
        ensure_type: None = ...,
    ) -> AwaitableOrValue[Optional["Node"]]:
        ...

    def resolve_node(self, info, *, required=False, ensure_type=None) -> Any:
        """Resolve the type name and node id info to the node itself.

        Tip: When you know the expected type, calling `ensure_type` should help
        not only to enforce it, but also help with typing since it will know that,
        if this function returns successfully, the retval should be of that
        type and not `Node`.

        Args:
            info:
                The strawberry execution info resolve the type name from
            required:
                If the value is required to exist. Note that asking to ensure
                the type automatically makes required true.
            ensure_type:
                Optionally check if the returned node is really an instance
                of this type.

        Returns:
            The resolved node

        Raises:
            TypeError:
                If ensure_type was provided and the type is not an instance of it

        """
        n_type = self.resolve_type(info)
        node = n_type.resolve_node(
            self.node_id,
            info=info,
            required=required or ensure_type is not None,
        )

        if node is not None and ensure_type is not None:
            origin = get_origin(ensure_type)
            if origin and issubclass(origin, Awaitable):
                ensure_type = get_args(ensure_type)[0]

            origin = get_origin(ensure_type)
            if origin and origin is Union:
                ensure_type = tuple(get_args(ensure_type))

            if inspect.isawaitable(node):

                async def resolve():
                    res = await cast(Awaitable[Node], node)
                    if not isinstance(res, ensure_type):
                        raise TypeError(f"{ensure_type} expected, found {repr(res)}")
                    return res

                node = resolve()
            else:
                if not isinstance(node, ensure_type):
                    raise TypeError(f"{ensure_type} expected, found {repr(node)}")

        return node


@interface(description="An object with a Globally Unique ID")
class Node(abc.ABC):
    """Node interface for GraphQL types.

    All types that are relay ready should inherit from this interface and
    implement the following methods.

    Attributes:
        ID_ATTR:
            (Optional) Define id field of node

    Methods:
        resolve_id:
            (Optional) Called to resolve the node's id.
            By default it returns `getattr(node, getattr(node, 'ID_ATTR'. 'id'))`
            to use the one provided when creating the node itself.
        resolve_node:
            Called to retrieve a node given its id
        resolve_nodes:
            Called to retrieve an iterable of node given their ids

    """

    @field(description="The Globally Unique ID of this object")
    @classmethod
    def id(cls, root: "Node", info: Info) -> GlobalID:
        # FIXME: We want to support both integration objects that doesn't define
        # a resolve_id and also the ones that does override it. Is there a better
        # way of handling this?
        if isinstance(root, Node):
            resolve_id = root.__class__.resolve_id
        else:
            # Try to use a custom resolve_id from the type itself. If it doesn't
            # define one, fallback to cls.resolve_id
            try:
                parent_type = info._raw_info.parent_type
                type_def = info.schema.get_type_by_name(parent_type.name)
                if not isinstance(type_def, TypeDefinition):
                    raise RuntimeError

                resolve_id = type_def.origin.resolve_id
            except (RuntimeError, AttributeError):
                resolve_id = cls.resolve_id

        node_id = resolve_id(root, info=info)
        resolve_typename = (
            root.__class__.resolve_typename
            if isinstance(root, Node)
            else cls.resolve_typename
        )
        type_name = resolve_typename(root, info)
        assert type_name

        if isinstance(node_id, str):
            # str is the default and is faster to check for it than is_awaitable
            return GlobalID(type_name=type_name, node_id=node_id)
        elif isinstance(node_id, (int, uuid.UUID)):
            # those are very common ids and are safe to convert to str
            return GlobalID(type_name=type_name, node_id=str(node_id))
        elif inspect.isawaitable(node_id):

            async def resolve():
                return GlobalID(
                    type_name=type_name,
                    node_id=await cast(Awaitable, node_id),
                )

            return resolve()

        # If node_id is not str, GlobalID will raise an error for us
        return GlobalID(type_name=type_name, node_id=cast(str, node_id))

    @classmethod
    def resolve_id(
        cls: Type[NodeType],
        root: NodeType,
        *,
        info: Optional[Info] = None,
    ) -> AwaitableOrValue[str]:
        """Resolve the node id.

        By default this returns `getattr(node, getattr(node, 'ID_ATTR'. 'id'))`.
        Override this to return something else.

        Args:
            info:
                The strawberry execution info resolve the type name from
            root:
                The node to resolve

        Returns:
            The resolved id (which is expected to be str)

        """
        id_attr = getattr(cls, "ID_ATTR", "id")
        return getattr(root, id_attr)

    @classmethod
    def resolve_typename(cls: Type[NodeType], root: NodeType, info: Info):
        return info.path.typename

    @classmethod
    @abc.abstractmethod
    def resolve_nodes(
        cls: Type[NodeType],
        *,
        info: Optional[Info] = None,
        node_ids: Optional[Iterable[str]] = None,
    ) -> AwaitableOrValue[Iterable[NodeType]]:
        """Resolve a list of nodes.

        This method *should* be defined by anyone implementing the `Node` interface.

        Args:
            info:
                The strawberry execution info resolve the type name from
            node_ids:
                Optional list of ids that, when provided, should be used to filter
                the results to only contain the nodes of those ids. When empty,
                all nodes of this type shall be returned.

        Returns:
            An iterable of resolved nodes.

        """
        raise NotImplementedError  # pragma:nocover

    @overload
    @classmethod
    @abc.abstractmethod
    def resolve_node(
        cls: Type[NodeType],
        node_id: str,
        *,
        info: Optional[Info] = ...,
        required: Literal[True],
    ) -> AwaitableOrValue[NodeType]:
        ...

    @overload
    @classmethod
    @abc.abstractmethod
    def resolve_node(
        cls: Type[NodeType],
        node_id: str,
        *,
        info: Optional[Info] = ...,
        required: bool = ...,
    ) -> AwaitableOrValue[Optional[NodeType]]:
        ...

    @classmethod
    @abc.abstractmethod
    def resolve_node(
        cls,
        node_id: str,
        *,
        info: Optional[Info] = None,
        required: bool = False,
    ):
        """Resolve a node given its id.

        This method *should* be defined by anyone implementing the `Node` interface.

        Args:
            info:
                The strawberry execution info resolve the type name from
            node_id:
                The id of the node to be retrieved
            required:
                if the node is required or not to exist. If not, then None
                should be returned if it doesn't exist. Otherwise an exception
                should be raised.

        Returns:
            The resolved node or None if it was not found

        """
        raise NotImplementedError  # pragma:nocover


@type(description="Information to aid in pagination.")
class PageInfo:
    """Information to aid in pagination.

    Attributes:
        has_next_page:
            When paginating forwards, are there more items?
        has_previous_page:
            When paginating backwards, are there more items?
        start_cursor:
            When paginating backwards, the cursor to continue
        end_cursor:
            When paginating forwards, the cursor to continue

    """

    has_next_page: bool = field(
        description="When paginating forwards, are there more items?",
    )
    has_previous_page: bool = field(
        description="When paginating backwards, are there more items?",
    )
    start_cursor: Optional[str] = field(
        description="When paginating backwards, the cursor to continue.",
    )
    end_cursor: Optional[str] = field(
        description="When paginating forwards, the cursor to continue.",
    )


@type(description="An edge in a connection.")
class Edge(Generic[NodeType]):
    """An edge in a connection.

    Attributes:
        cursor:
            A cursor for use in pagination
        node:
            The item at the end of the edge

    """

    cursor: str = field(
        description="A cursor for use in pagination",
    )
    node: NodeType = field(
        description="The item at the end of the edge",
    )

    @classmethod
    def from_node(cls, node: NodeType, *, cursor: Any = None):
        return cls(cursor=to_base64(connection_typename, cursor), node=node)


@type(description="A connection to a list of items.")
class Connection(Generic[NodeType]):
    """A connection to a list of items.

    Attributes:
        page_info:
            Pagination data for this connection
        edges:
            Contains the nodes in this connection
        total_count:
            Total quantity of existing nodes

    """

    page_info: PageInfo = field(
        description="Pagination data for this connection",
    )
    edges: List[Edge[NodeType]] = field(
        description="Contains the nodes in this connection",
    )
    total_count: Optional[int] = field(
        description="Total quantity of existing nodes",
        default=None,
    )

    @classmethod
    def from_nodes(
        cls,
        nodes: Iterable[NodeType],
        *,
        info: Optional[Info] = None,
        total_count: Optional[int] = None,
        before: Optional[str] = None,
        after: Optional[str] = None,
        first: Optional[int] = None,
        last: Optional[int] = None,
        **kwargs,
    ):
        """Resolve a connection from the list of nodes.

        This uses the described Relay Pagination algorithm_

        Args:
            info:
                The strawberry execution info resolve the type name from
            nodes:
                An iterable of nodes to transform to a connection
            total_count:
                Optionally provide a total count so that the connection
                doesn't have to calculate it. Might be useful for some ORMs
                for performance reasons.
            before:
                Returns the items in the list that come before the specified cursor
            after:
                Returns the items in the list that come after the specified cursor
            first:
                Returns the first n items from the list
            last:
                Returns the items in the list that come after the specified cursor

        Returns:
            The resolved `Connection`

        .. _Relay Pagination algorithm:
            https://relay.dev/graphql/connections.htm#sec-Pagination-algorithm

        """
        if total_count is None:
            # Support ORMs that define .count() (e.g. django)
            try:
                total_count = int(nodes.count())  # type:ignore
            except (AttributeError, ValueError, TypeError):
                if isinstance(nodes, Sized):
                    total_count = len(nodes)

        # TODO: This should be configurable
        max_results = 100
        if max_results is None:
            max_results = sys.maxsize

        start = 0
        end: Optional[int] = total_count if total_count is not None else sys.maxsize

        if after:
            after_type, after_parsed = from_base64(after)
            assert after_type == connection_typename
            start = int(after_parsed) + 1
        if before:
            before_type, before_parsed = from_base64(before)
            assert before_type == connection_typename
            end = int(before_parsed)

        if isinstance(first, int):
            if first < 0:
                raise ValueError("Argument 'first' must be a non-negative integer.")

            if first > max_results:
                raise ValueError(
                    f"Argument 'first' cannot be higher than {max_results}."
                )

            assert end is not None
            end = min(end, start + first)
        if isinstance(last, int):
            if last < 0:
                raise ValueError("Argument 'last' must be a non-negative integer.")

            if last > max_results:
                raise ValueError(
                    f"Argument 'last' cannot be higher than {max_results}."
                )

            if end == sys.maxsize:
                # This is the worst case, someone is asking for last without
                # specifying an after argument. We basically want the
                # total_count - last in here. If we don't have the total_count
                # (e.g. because nodes is a generator), the slice below will
                # have to iterate over it all, so we can transform it to a list
                # here to retrieve that total_count right now
                if total_count is None:
                    nodes = list(nodes)
                    total_count = len(nodes)

                start = max(start, total_count - last)
                end = None
            else:
                assert end is not None
                start = max(start, end - last)

        # If at this point end is still inf, consider it to be start + max_results
        if end == sys.maxsize:
            end = start + max_results

        expected = end - start if end is not None else abs(start)
        # If no parameters are given, end could be total_results at this point.
        # Make sure we don't exceed max_results in here
        if expected > max_results:
            end = start + max_results
            expected = end - start

        # Overfetch by 1 to check if we have a next result
        type_def = cast(TypeDefinition, cls._type_definition)  # type:ignore
        field_def = type_def.get_field("edges")
        assert field_def

        field = field_def.type
        while isinstance(field, StrawberryContainer):
            field = field.of_type

        edge_class = cast(Edge[NodeType], field)
        iterator = (
            cast(Sequence, nodes)[start : end + 1 if end is not None else None]
            if hasattr(nodes, "__getitem__")
            else itertools.islice(nodes, start, end + 1 if end is not None else None)
        )
        edges = [
            edge_class.from_node(v, cursor=start + i) for i, v in enumerate(iterator)
        ]

        # Remove the overfetched result
        if len(edges) == expected + 1:
            edges = edges[:-1]
            has_next_page = True
        else:
            has_next_page = False

        page_info = PageInfo(
            start_cursor=edges[0].cursor if edges else None,
            end_cursor=edges[-1].cursor if edges else None,
            has_previous_page=start > 0,
            has_next_page=has_next_page,
        )

        return cls(
            edges=edges,
            page_info=page_info,
            total_count=total_count,
        )