from __future__ import annotations

import typing as t
from itertools import zip_longest

from flake8_annotations import PY_GTE_38, PY_GTE_311
from flake8_annotations.enums import AnnotationType, ClassDecoratorType, FunctionType
from flake8_annotations.visitors import AST_ARG_TYPES, ReturnVisitor, ast

if t.TYPE_CHECKING:
    from flake8_annotations.visitors import AST_DECORATOR_NODES, AST_FUNCTION_TYPES

# Check if we can use the stdlib ast module instead of typed_ast
# stdlib ast gains native type comment support in Python 3.8
if PY_GTE_38:
    from ast import Ellipsis as ast_Ellipsis
else:
    from typed_ast.ast3 import Ellipsis as ast_Ellipsis  # type: ignore[misc]


class Argument:
    """Represent a function argument & its metadata."""

    __slots__ = [
        "argname",
        "lineno",
        "col_offset",
        "annotation_type",
        "has_type_annotation",
        "has_3107_annotation",
        "has_type_comment",
    ]

    def __init__(
        self,
        argname: str,
        lineno: int,
        col_offset: int,
        annotation_type: AnnotationType,
        has_type_annotation: bool = False,
        has_3107_annotation: bool = False,
        has_type_comment: bool = False,
    ):
        self.argname = argname
        self.lineno = lineno
        self.col_offset = col_offset
        self.annotation_type = annotation_type
        self.has_type_annotation = has_type_annotation
        self.has_3107_annotation = has_3107_annotation
        self.has_type_comment = has_type_comment

    def __str__(self) -> str:
        """
        Format the Argument object into a readable representation.

        The output string will be formatted as:
          '<Argument: <argname>, Annotated: <has_type_annotation>>'
        """
        return f"<Argument: {self.argname}, Annotated: {self.has_type_annotation}>"

    def __repr__(self) -> str:
        """Format the Argument object into its "official" representation."""
        # Python 3.11 introduces a backwards-incompatible change to Enum str/repr
        # See: https://bugs.python.org/issue40066
        # See: https://bugs.python.org/issue44559
        if PY_GTE_311:
            annotation_type = repr(self.annotation_type)
        else:
            annotation_type = str(self.annotation_type)

        return (
            f"Argument("
            f"argname={self.argname!r}, "
            f"lineno={self.lineno}, "
            f"col_offset={self.col_offset}, "
            f"annotation_type={annotation_type}, "
            f"has_type_annotation={self.has_type_annotation}, "
            f"has_3107_annotation={self.has_3107_annotation}, "
            f"has_type_comment={self.has_type_comment}"
            ")"
        )

    @classmethod
    def from_arg_node(cls, node: ast.arg, annotation_type_name: str) -> "Argument":
        """Create an Argument object from an ast.arguments node."""
        annotation_type = AnnotationType[annotation_type_name]
        new_arg = cls(node.arg, node.lineno, node.col_offset, annotation_type)

        new_arg.has_type_annotation = False
        if node.annotation:
            new_arg.has_type_annotation = True
            new_arg.has_3107_annotation = True

        if node.type_comment:
            new_arg.has_type_annotation = True
            new_arg.has_type_comment = True

        return new_arg


class Function:
    """
    Represent a function and its relevant metadata.

    Note: while Python differentiates between a function and a method, for the purposes of this
    tool, both will be referred to as functions outside of any class-specific context. This also
    aligns with ast's naming convention.
    """

    __slots__ = [
        "name",
        "lineno",
        "col_offset",
        "function_type",
        "is_class_method",
        "class_decorator_type",
        "is_return_annotated",
        "has_type_comment",
        "has_only_none_returns",
        "is_nested",
        "decorator_list",
        "args",
    ]

    def __init__(
        self,
        name: str,
        lineno: int,
        col_offset: int,
        function_type: FunctionType = FunctionType.PUBLIC,
        is_class_method: bool = False,
        class_decorator_type: t.Union[ClassDecoratorType, None] = None,
        is_return_annotated: bool = False,
        has_type_comment: bool = False,
        has_only_none_returns: bool = True,
        is_nested: bool = False,
        *,
        decorator_list: t.List[AST_DECORATOR_NODES],
        args: t.List[Argument],
    ):
        self.name = name
        self.lineno = lineno
        self.col_offset = col_offset
        self.function_type = function_type
        self.is_class_method = is_class_method
        self.class_decorator_type = class_decorator_type
        self.is_return_annotated = is_return_annotated
        self.has_type_comment = has_type_comment
        self.has_only_none_returns = has_only_none_returns
        self.is_nested = is_nested
        self.decorator_list = decorator_list
        self.args = args

    def is_fully_annotated(self) -> bool:
        """
        Check that all of the function's inputs are type annotated.

        Note that self.args will always include an Argument object for return
        """
        return all(arg.has_type_annotation for arg in self.args)

    def is_dynamically_typed(self) -> bool:
        """Determine if the function is dynamically typed, defined as completely lacking hints."""
        return not any(arg.has_type_annotation for arg in self.args)

    def get_missed_annotations(self) -> t.List[Argument]:
        """Provide a list of arguments with missing type annotations."""
        return [arg for arg in self.args if not arg.has_type_annotation]

    def get_annotated_arguments(self) -> t.List[Argument]:
        """Provide a list of arguments with type annotations."""
        return [arg for arg in self.args if arg.has_type_annotation]

    def has_decorator(self, check_decorators: t.Set[str]) -> bool:
        """
        Determine whether the function node is decorated by any of the provided decorators.

        Decorator matching is done against the provided `check_decorators` set, allowing the user
        to specify any expected aliasing in the relevant flake8 configuration option. Decorators are
        assumed to be either a module attribute (e.g. `@typing.overload`) or name
        (e.g. `@overload`). For the case of a module attribute, only the attribute is checked
        against `overload_decorators`.

        NOTE: Deeper decorator imports (e.g. `a.b.overload`) are not explicitly supported
        """
        for decorator in self.decorator_list:
            # Drop to a helper to allow for simpler handling of callable decorators
            return self._decorator_checker(decorator, check_decorators)
        else:
            return False

    def _decorator_checker(
        self, decorator: AST_DECORATOR_NODES, check_decorators: t.Set[str]
    ) -> bool:
        """
        Check the provided decorator for a match against the provided set of check names.

        Decorators are assumed to be of the following form:
            * `a.name` or `a.name()`
            * `name` or `name()`

        NOTE: Deeper imports (e.g. `a.b.name`) are not explicitly supported.
        """
        if isinstance(decorator, ast.Name):
            # e.g. `@overload`, where `decorator.id` will be the name
            if decorator.id in check_decorators:
                return True
        elif isinstance(decorator, ast.Attribute):
            # e.g. `@typing.overload`, where `decorator.attr` will be the name
            if decorator.attr in check_decorators:
                return True
        elif isinstance(decorator, ast.Call):  # pragma: no branch
            # e.g. `@overload()` or `@typing.overload()`, where `decorator.func` will be `ast.Name`
            # or `ast.Attribute`, which we can check recursively
            # Ignore typing here, the AST stub just uses `expr` as the type for `decorator.func`
            return self._decorator_checker(decorator.func, check_decorators)  # type: ignore[arg-type]  # noqa: E501

        # There shouldn't be any possible way to get here
        return False  # pragma: no cover

    def __str__(self) -> str:
        """
        Format the Function object into a readable representation.

        The output string will be formatted as:
          '<Function: <name>, Args: <args>>'
        """
        # Manually join the list so we get Argument's __str__ instead of __repr__
        # Function will always have a list of at least one Argument ("return" is always added)
        str_args = f"[{', '.join([str(arg) for arg in self.args])}]"

        return f"<Function: {self.name}, Args: {str_args}>"

    def __repr__(self) -> str:
        """Format the Function object into its "official" representation."""
        # Python 3.11 introduces a backwards-incompatible change to Enum str/repr
        # See: https://bugs.python.org/issue40066
        # See: https://bugs.python.org/issue44559
        if PY_GTE_311:
            function_type = repr(self.function_type)
            class_decorator_type = repr(self.class_decorator_type)
        else:
            function_type = str(self.function_type)
            class_decorator_type = str(self.class_decorator_type)

        return (
            f"Function("
            f"name={self.name!r}, "
            f"lineno={self.lineno}, "
            f"col_offset={self.col_offset}, "
            f"function_type={function_type}, "
            f"is_class_method={self.is_class_method}, "
            f"class_decorator_type={class_decorator_type}, "
            f"is_return_annotated={self.is_return_annotated}, "
            f"has_type_comment={self.has_type_comment}, "
            f"has_only_none_returns={self.has_only_none_returns}, "
            f"is_nested={self.is_nested}, "
            f"decorator_list={self.decorator_list}, "
            f"args={self.args}"
            ")"
        )

    @classmethod
    def from_function_node(
        cls, node: AST_FUNCTION_TYPES, lines: t.List[str], **kwargs: t.Any
    ) -> "Function":
        """
        Create an Function object from ast.FunctionDef or ast.AsyncFunctionDef nodes.

        Accept the source code, as a list of strings, in order to get the column where the function
        definition ends.

        With exceptions, input kwargs are passed straight through to Function's __init__. The
        following kwargs will be overridden:
          * function_type
          * class_decorator_type
          * args
        """
        # Extract function types from function name
        kwargs["function_type"] = cls.get_function_type(node.name)

        # Identify type of class method, if applicable
        if kwargs.get("is_class_method", False):
            kwargs["class_decorator_type"] = cls.get_class_decorator_type(node)

        # Store raw decorator list for use by property methods
        kwargs["decorator_list"] = node.decorator_list

        # Instantiate empty args list here since it has no default (mutable defaults bad!)
        kwargs["args"] = []

        new_function = cls(node.name, node.lineno, node.col_offset, **kwargs)

        # Iterate over arguments by type & add
        for arg_type in AST_ARG_TYPES:
            args = node.args.__getattribute__(arg_type)
            if args:
                if not isinstance(args, list):
                    args = [args]

                new_function.args.extend(
                    [Argument.from_arg_node(arg, arg_type.upper()) for arg in args]
                )

        # Create an Argument object for the return hint
        def_end_lineno, def_end_col_offset = cls.colon_seeker(node, lines)
        return_arg = Argument("return", def_end_lineno, def_end_col_offset, AnnotationType.RETURN)
        if node.returns:
            return_arg.has_type_annotation = True
            return_arg.has_3107_annotation = True
            new_function.is_return_annotated = True

        new_function.args.append(return_arg)

        # Type comments in-line with input arguments are handled by the Argument class
        # If a function-level type comment is present, attempt to parse for any missed type hints
        if node.type_comment:
            new_function.has_type_comment = True
            new_function = cls.try_type_comment(new_function, node)

        # Check for the presence of non-`None` returns using the special-case return node visitor
        return_visitor = ReturnVisitor(node)
        return_visitor.visit(node)
        new_function.has_only_none_returns = return_visitor.has_only_none_returns

        return new_function

    @staticmethod
    def colon_seeker(node: AST_FUNCTION_TYPES, lines: t.List[str]) -> t.Tuple[int, int]:
        """
        Find the line & column indices of the function definition's closing colon.

        Processing paths are Python version-dependent, as there are differences in where the
        docstring is placed in the AST:
            * Python >= 3.8, docstrings are contained in the body of the function node
            * Python < 3.8, docstrings are contained in the function node

        NOTE: AST's line numbers are 1-indexed, column offsets are 0-indexed. Since `lines` is a
        list, it will be 0-indexed.
        """
        # Special case single line function definitions
        if node.lineno == node.body[0].lineno:
            return Function._single_line_colon_seeker(node, lines[node.lineno - 1])

        # With Python < 3.8, the function node includes the docstring & the body does not, so
        # we have rewind through any docstrings, if present, before looking for the def colon
        # We should end up with lines[def_end_lineno - 1] having the colon
        def_end_lineno = node.body[0].lineno
        if not PY_GTE_38:
            # If the docstring is on one line then no rewinding is necessary.
            n_triple_quotes = lines[def_end_lineno - 1].count('"""')
            if n_triple_quotes == 1:  # pragma: no branch
                # Docstring closure, rewind until the opening is found & take the line prior
                while True:
                    def_end_lineno -= 1
                    if '"""' in lines[def_end_lineno - 1]:  # pragma: no branch
                        # Docstring has closed
                        break

        # Once we've gotten here, we've found the line where the docstring begins, so we have
        # to step up one more line to get to the close of the def
        def_end_lineno -= 1

        # Use str.rfind() to account for annotations on the same line, definition closure should
        # be the last : on the line
        def_end_col_offset = lines[def_end_lineno - 1].rfind(":")

        return def_end_lineno, def_end_col_offset

    @staticmethod
    def _single_line_colon_seeker(node: AST_FUNCTION_TYPES, line: str) -> t.Tuple[int, int]:
        """Locate the closing colon for a single-line function definition."""
        col_start = node.col_offset
        col_end = node.body[0].col_offset
        def_end_col_offset = line.rfind(":", col_start, col_end)

        return node.lineno, def_end_col_offset

    @staticmethod
    def try_type_comment(func_obj: "Function", node: AST_FUNCTION_TYPES) -> "Function":
        """
        Attempt to infer type hints from a function-level type comment.

        If a function is type commented it is assumed to have a return annotation, otherwise Python
        will fail to parse the hint
        """
        # If we're in this function then the node is guaranteed to have a type comment, so we can
        # ignore mypy's complaint about an incompatible type for `node.type_comment`
        # Because we're passing in the `func_type` arg, we know that our return is guaranteed to be
        # ast.FunctionType
        hint_tree: ast.FunctionType = ast.parse(node.type_comment, "<func_type>", "func_type")  # type: ignore[assignment, arg-type]  # noqa: E501
        hint_tree = Function._maybe_inject_class_argument(hint_tree, func_obj)

        for arg, hint_comment in zip_longest(func_obj.args, hint_tree.argtypes):
            if isinstance(hint_comment, ast_Ellipsis):
                continue

            if arg and hint_comment:
                arg.has_type_annotation = True
                arg.has_type_comment = True

        # Return arg is always last
        func_obj.args[-1].has_type_annotation = True
        func_obj.args[-1].has_type_comment = True
        func_obj.is_return_annotated = True

        return func_obj

    @staticmethod
    def _maybe_inject_class_argument(
        hint_tree: ast.FunctionType, func_obj: "Function"
    ) -> ast.FunctionType:
        """
        Inject `self` or `cls` args into a type comment to align with PEP 3107-style annotations.

        Because PEP 484 does not describe a method to provide partial function-level type comments,
        there is a potential for ambiguity in the context of both class methods and classmethods
        when aligning type comments to method arguments.

        These two class methods, for example, should lint equivalently:

            def bar(self, a):
                # type: (int) -> int
                ...

            def bar(self, a: int) -> int
                ...

        When this example type comment is parsed by `ast` and then matched with the method's
        arguments, it associates the `int` hint to `self` rather than `a`, so a dummy hint needs to
        be provided in situations where `self` or `class` are not hinted in the type comment in
        order to achieve equivalent linting results to PEP-3107 style annotations.

        A dummy `ast.Ellipses` constant is injected if the following criteria are met:
            1. The function node is either a class method or classmethod
            2. The number of hinted args is at least 1 less than the number of function args
        """
        if not func_obj.is_class_method:
            # Short circuit
            return hint_tree

        if func_obj.class_decorator_type != ClassDecoratorType.STATICMETHOD:
            if len(hint_tree.argtypes) < (len(func_obj.args) - 1):  # Subtract 1 to skip return arg
                # Ignore mypy's objection to this assignment, Ellipsis subclasses expr so I'm not
                # sure how to make Mypy happy with this but I think it still makes semantic sense
                hint_tree.argtypes = [ast.Ellipsis()] + hint_tree.argtypes  # type: ignore[assignment, operator]  # noqa: E501

        return hint_tree

    @staticmethod
    def get_function_type(function_name: str) -> FunctionType:
        """
        Determine the function's FunctionType from its name.

        MethodType is determined by the following priority:
          1. Special: function name prefixed & suffixed by "__"
          2. Private: function name prefixed by "__"
          3. Protected: function name prefixed by "_"
          4. Public: everything else
        """
        if function_name.startswith("__") and function_name.endswith("__"):
            return FunctionType.SPECIAL
        elif function_name.startswith("__"):
            return FunctionType.PRIVATE
        elif function_name.startswith("_"):
            return FunctionType.PROTECTED
        else:
            return FunctionType.PUBLIC

    @staticmethod
    def get_class_decorator_type(
        function_node: AST_FUNCTION_TYPES,
    ) -> t.Union[ClassDecoratorType, None]:
        """
        Get the class method's decorator type from its function node.

        For the purposes of this tool, only @classmethod and @staticmethod decorators are
        identified; all other decorators are ignored

        If @classmethod or @staticmethod decorators are not present, this function will return None
        """
        # @classmethod and @staticmethod will show up as ast.Name objects, where callable decorators
        # will show up as ast.Call, which we can ignore
        decorators = [
            decorator.id
            for decorator in function_node.decorator_list
            if isinstance(decorator, ast.Name)
        ]

        if "classmethod" in decorators:
            return ClassDecoratorType.CLASSMETHOD
        elif "staticmethod" in decorators:
            return ClassDecoratorType.STATICMETHOD
        else:
            return None
