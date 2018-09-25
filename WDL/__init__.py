# pyre-strict
"""Toolkit for static analysis of Workflow Description Language (WDL)"""
import lark
import inspect
import WDL._parser
from WDL import Expr as E
from WDL import Type as T
from WDL import Document as D
from WDL.Error import SourcePosition
import WDL.StdLib

def sp(meta) -> SourcePosition:
    return SourcePosition(line=meta.line, column=meta.column,
                          end_line=meta.end_line, end_column=meta.end_column)

# Transformer from lark.Tree to WDL.Expr
class _ExprTransformer(lark.Transformer):

    def boolean_true(self, items, meta) -> E.Base:
        return E.Boolean(sp(meta), True)
    def boolean_false(self, items, meta) -> E.Base:
        return E.Boolean(sp(meta), False)
    def int(self, items, meta) -> E.Base:
        assert len(items) == 1
        return E.Int(sp(meta), int(items[0]))
    def float(self, items, meta) -> E.Base:
        assert len(items) == 1
        return E.Float(sp(meta), float(items[0]))
    def string(self, items, meta) -> E.Base:
        parts = []
        for item in items:
            if isinstance(item, E.Base):
                parts.append(E.Placeholder(item.pos, {}, item))
            elif item.type.endswith("_FRAGMENT"):
                # for an interpolation fragment, item.value will end with "${"
                # so we strip that off. it'd be nice to make the grammar filter
                # that out since it does later filter out the "}"...
                parts.append(item.value[:-2])
            else:
                parts.append(item.value)
        return E.String(sp(meta), parts)
    def array(self, items, meta) -> E.Base:
        return E.Array(sp(meta), items)

    def apply(self, items, meta) -> E.Base:
        assert len(items) >= 1
        return E.Apply(sp(meta), items[0], items[1:])
    def negate(self, items, meta) -> E.Base:
        return E.Apply(sp(meta), "_negate", items)
    def get(self, items, meta) -> E.Base:
        return E.Apply(sp(meta), "_get", items)

    def ifthenelse(self, items, meta) -> E.Base:
        assert len(items) == 3
        return E.IfThenElse(sp(meta), *items)

    def ident(self, items, meta) -> E.Base:
        return E.Ident(sp(meta), [item.value for item in items])

# _ExprTransformer infix operators        
for op in ["land", "lor", "add", "sub", "mul", "div", "rem",
           "eqeq", "neq", "lt", "lte", "gt", "gte"]:
    def fn(self, items, meta, op=op):
        assert len(items) == 2
        return E.Apply(sp(meta), "_"+op, items)
    setattr(_ExprTransformer, op, lark.v_args(meta=True)(classmethod(fn)))

class _TypeTransformer(lark.Transformer):
    def int_type(self, items, meta):
        return T.Int()
    def float_type(self, items, meta):
        return T.Float()
    def boolean_type(self, items, meta):
        return T.Boolean()
    def string_type(self, items, meta):
        return T.String()
    def array_type(self, items, meta):
        assert len(items) == 1
        return T.Array(items[0])

class _TaskTransformer(_ExprTransformer, _TypeTransformer):
    def decl(self, items, meta):
        assert len(items) == 2
        type = items[0]
        assert items[1].type == "CNAME"
        name = items[1]
        return D.Decl(sp(meta), type, name)
    def quantified_decl(self, items, meta):
        assert len(items) == 3
        type = items[0]
        optional = False
        nonempty = False
        if items[1].value == "?":
            optional = True
        elif items[1].value == "+":
            nonempty = True
        else:
            assert False
        assert items[2].type == "CNAME"
        name = items[2]
        return D.Decl(sp(meta), type, name, optional, nonempty)
    def bound_decl(self, items, meta):
        assert len(items) == 2
        return items[0].bind(items[1])
    def input_decls(self, items, meta):
        return {"inputs": items}
    def noninput_decls(self, items, meta):
        return {"decls": items}
    def placeholder_option(self, items, meta):
        assert len(items) == 2
        return (items[0].value, items[1].value[1:-1])
    def placeholder(self, items, meta):
        options = dict(items[:-1])
        # TODO: error on duplicate options
        return E.Placeholder(sp(meta), options, items[-1])
    def command(self, items, meta):
        parts = []
        for item in items:
            if isinstance(item, E.Placeholder):
                parts.append(item)
            elif item.type.endswith("_FRAGMENT"):
                parts.append(item.value[:-2])
            else:
                parts.append(item.value)
        return {"command": E.String(sp(meta), parts)}
    def output_decls(self, items, meta):
        return {"outputs": items}
    def meta_kv(self, items, meta):
        return (items[0].value, items[1])
    def meta_literal(self, items, meta):
        # Within JSON-like meta clauses, literals come out as Expr's since we
        # reused the Expr transformer for convenience. Since the grammar
        # constrains them to be literals, we can evaluate them immediately.
        assert len(items) == 1
        assert isinstance(items[0], WDL.Expr.Base)
        items[0].infer_type(WDL.Expr.TypeEnv())
        return items[0].eval(WDL.Expr.Env()).value
    def meta_object(self, items, meta):
        d = dict()
        for k, v in items:
            assert k not in d # TODO: helpful error for duplicate keys
            d[k] = v
        return d
    def meta_array(self, items, meta):
        return items
    def meta_section(self, items, meta):
        kind = items[0].value
        d = dict()
        d[kind] = items[1]
        return d
    def task(self, items, meta):
        d = {}
        for item in items:
            if isinstance(item, dict):
                for k,v in item.items():
                    assert k not in d # TODO: helpful error for redundant task sections
                    d[k] = v
            else:
                assert isinstance(item, str)
                assert "name" not in d
                d["name"] = item
        return D.Task(sp(meta), d["name"], d.get("inputs", []), d.get("decls", []), d["command"],
                      d.get("outputs", []), d.get("parameter_meta", {}), d.get("runtime", {}),
                      d.get("meta", {}))

# have lark pass the 'meta' with line/column numbers to each transformer method
for _klass in [_ExprTransformer, _TypeTransformer, _TaskTransformer]:
    for name, method in inspect.getmembers(_klass, inspect.isfunction):
        if not name.startswith('_'):
            setattr(_klass, name, lark.v_args(meta=True)(method))

def parse_expr(txt : str) -> E.Base:
    """
    Parse an individual WDL expression into an abstract syntax tree
    
    :param txt: expression text
    """
    return _ExprTransformer().transform(WDL._parser.parse(txt, "expr"))


def parse_task(txt : str) -> D.Task:
    """
    Parse a WDL task
    """
    return _TaskTransformer().transform(WDL._parser.parse(txt, "task"))
