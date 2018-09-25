# pyre-strict
"""
WDL Documents, encompassing Tasks and Workflows

"""

from abc import ABC, abstractmethod
from typing import Any, List, Optional, Dict, Callable, TypeVar, Tuple, Union
import WDL.Type as T
import WDL.Value as V
import WDL.Expr as E
import WDL.Error as Error
from WDL.Error import SourcePosition, SourceNode

TVDecl = TypeVar("TVDecl", bound="Decl")
class Decl(SourceNode):
    """
    A declaration, consisting of a type, name, and (optionally) an expression.
    Additionally, the type may have quantity constraints (optional/nonempty).
    """
    type : T.Base
    name : str
    optional : bool
    nonempty : bool
    expr : Optional[E.Base]

    def __init__(self, pos : SourcePosition, type : T.Base, name: str, optional : bool = False, nonempty : bool = False, expr : Optional[E.Base] = None) -> None:
        super().__init__(pos)
        self.type = type
        self.name = name
        self.optional = optional
        self.nonempty = nonempty
        self.expr = expr

        if self.nonempty and not isinstance(type, T.Array):
            raise Error.IncompatibleOperand(self, "nonempty quantifier (+) on non-Array type")

    def __str__(self) -> str:
        if self.expr is None:
            return "{} {}".format(str(self.type), self.name)
        return "{} {} = {}".format(str(self.type), self.name, str(self.expr))

    def bind(self, expr : E.Base) -> TVDecl:
        return Decl(self.pos, self.type, self.name, self.optional, self.nonempty, expr)


class Task(SourceNode):
    """WDL Task"""
    name : str
    inputs : List[Decl]
    postinputs: List[Decl]
    command: E.String
    outputs: List[Decl]
    parameter_meta : Dict[str,Any]
    runtime : Dict[str,Any]
    meta : Dict[str,Any]

    def __init__(self, pos : SourcePosition, name : str, inputs : List[Decl], postinputs : List[Decl],
                 command : E.String, outputs : List[Decl], parameter_meta : Dict[str,Any],
                 runtime : Dict[str,Any], meta : Dict[str,Any]) -> None:
        super().__init__(pos)
        self.name = name
        self.inputs = inputs
        self.postinputs = postinputs
        self.command = command
        self.outputs = outputs
        self.parameter_meta = parameter_meta
        self.runtime = runtime
        self.meta = meta
        # TODO: enforce validity constraints on parameter_meta and runtime

    def typecheck(self, type_env : Optional[E.TypeEnv] = None) -> None:
        """Infer and check types on all declarations and the command"""
        if type_env is None:
             type_env = E.TypeEnv()
        for decl in (self.inputs+self.postinputs):
            type_env = _typecheck_decl(decl, type_env)
        self.command.infer_type(type_env).typecheck(T.String())
        for decl in self.outputs:
            type_env = _typecheck_decl(decl, type_env)

def _typecheck_decl(decl : Decl, type_env : E.TypeEnv) -> E.TypeEnv:
    if decl.expr is not None:
        decl.expr.infer_type(type_env).typecheck(decl.type)
    return E.TypeEnv((decl.name, decl.type), type_env)
