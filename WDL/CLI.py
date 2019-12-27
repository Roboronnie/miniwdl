"""
miniwdl command-line interface
"""
# PYTHON_ARGCOMPLETE_OK
import sys
import os
import subprocess
import tempfile
import glob
import json
import math
import argcomplete
import logging
import urllib
import asyncio
import atexit
import docker
from shlex import quote as shellquote
from datetime import datetime
from argparse import ArgumentParser, Action, SUPPRESS
import importlib_metadata
from . import *
from ._util import (
    provision_run_dir,
    write_values_json,
    VERBOSE_LEVEL,
    NOTICE_LEVEL,
    install_coloredlogs,
    ensure_swarm,
    parse_byte_size,
)
from ._util import StructuredLogMessage as _

quant_warning = False


def main(args=None):
    sys.setrecursionlimit(1_000_000)  # permit as much call stack depth as OS can give us
    os.environ["COLUMNS"] = os.environ.get("COLUMNS", "100")

    parser = ArgumentParser("miniwdl")
    parser.add_argument(
        "--version",
        nargs=0,
        action=PipVersionAction,
        help="show miniwdl package version information",
    )
    subparsers = parser.add_subparsers()
    subparsers.required = True
    subparsers.dest = "command"
    fill_common(fill_check_subparser(subparsers))
    fill_common(fill_cromwell_subparser(subparsers), path=False)  # FIXME path issue #131
    fill_common(fill_run_subparser(subparsers))
    fill_common(fill_run_self_test_subparser(subparsers))
    fill_common(fill_localize_subparser(subparsers))

    argcomplete.autocomplete(parser)
    args = parser.parse_args(args if args is not None else sys.argv[1:])

    try:
        if args.command == "check":
            check(**vars(args))
        elif args.command == "run":
            runner(**vars(args))
        elif args.command == "run_self_test":
            run_self_test(**vars(args))
        elif args.command == "localize":
            localize(**vars(args))
        elif args.command == "cromwell":
            cromwell(**vars(args))
        else:
            assert False
    except (
        Error.SyntaxError,
        Error.ImportError,
        Error.ValidationError,
        Error.MultipleValidationErrors,
    ) as exn:
        global quant_warning
        print_error(exn)
        if args.check_quant and quant_warning:
            print(
                "* Hint: for compatibility with older existing WDL code, try setting --no-quant-check to relax quantifier validation rules.",
                file=sys.stderr,
            )
        if args.debug:
            raise exn
        sys.exit(2)
    sys.exit(0)


class PipVersionAction(Action):
    def __call__(self, parser, namespace, values, option_string=None):
        try:
            print(f"miniwdl v{importlib_metadata.version('miniwdl')}")
        except importlib_metadata.PackageNotFoundError:
            print("miniwdl version unknown")

        # show plugin versions
        # importlib_metadata doesn't seem to provide EntryPoint.dist to get from an entry point to
        # the metadata of the package providing it; continuing to use pkg_resources for this. Risk
        # that they give inconsistent results?
        import pkg_resources

        for plugin_group in ["miniwdl.plugin.file_download"]:
            for plugin in pkg_resources.iter_entry_points(group=plugin_group):
                print(f"{plugin_group}\t{plugin}\t{plugin.dist}")
        print("Cromwell " + CROMWELL_VERSION)
        sys.exit(0)


def fill_common(subparser, path=True):
    group = subparser.add_argument_group("language")
    group.add_argument(
        "--no-quant-check",
        dest="check_quant",
        action="store_false",
        help="relax static typechecking of optional types, and permit coercion of T to Array[T] (discouraged; for backwards compatibility with older WDL)",
    )
    if path:
        group.add_argument(
            "-p",
            "--path",
            metavar="DIR",
            type=str,
            action="append",
            help="local directory to search for imports",
        )
    subparser.add_argument(
        "--debug", action="store_true", help="maximally verbose logging & exception tracebacks"
    )


def fill_check_subparser(subparsers):
    check_parser = subparsers.add_parser(
        "check", help="Load and typecheck a WDL document; show an outline with lint warnings"
    )
    check_parser.add_argument(
        "uri", metavar="URI", type=str, nargs="+", help="WDL document filename/URI"
    )
    check_parser.add_argument(
        "--no-shellcheck",
        dest="shellcheck",
        action="store_false",
        help="don't use shellcheck on task commands even if available, and suppress message if it isn't",
    )
    return check_parser


def check(uri=None, path=None, check_quant=True, shellcheck=True, **kwargs):
    # Load the document (read, parse, and typecheck)
    if not shellcheck:
        Lint._shellcheck_available = False

    for uri1 in uri or []:
        doc = load(uri1, path or [], check_quant=check_quant, read_source=read_source)

        Lint.lint(doc)

        # Print an outline
        print(os.path.basename(uri1))
        outline(doc, 0, show_called=(doc.workflow is not None))

    if shellcheck and Lint._shellcheck_available == False:
        print(
            "* Hint: install shellcheck (www.shellcheck.net) to check task commands. (--no-shellcheck suppresses this message)",
            file=sys.stderr,
        )


def outline(obj, level, file=sys.stdout, show_called=True):
    # recursively pretty-print a brief outline of the workflow
    s = "".join(" " for i in range(level * 4))

    first_descent = []

    def descend(dobj=None, first_descent=first_descent):
        # show lint for the node just prior to first descent beneath it
        if not first_descent and hasattr(obj, "lint"):
            for (pos, klass, msg) in sorted(obj.lint, key=lambda t: t[0]):
                print(
                    "{}    (Ln {}, Col {}) {}, {}".format(s, pos.line, pos.column, klass, msg),
                    file=file,
                )
        first_descent.append(False)
        if dobj:
            outline(
                dobj,
                level + (1 if not isinstance(dobj, Decl) else 0),
                file=file,
                show_called=show_called,
            )

    # document
    if isinstance(obj, Document):
        # workflow
        if obj.workflow:
            descend(obj.workflow)
        # tasks
        for task in sorted(obj.tasks, key=lambda task: (not task.called, task.name)):
            descend(task)
        # imports
        for imp in sorted(obj.imports, key=lambda t: t.namespace):
            print("    {}{} : {}".format(s, imp.namespace, os.path.basename(imp.uri)), file=file)
            descend(imp.doc)
    # workflow
    elif isinstance(obj, Workflow):
        print(
            "{}workflow {}{}".format(
                s, obj.name, " (not called)" if show_called and not obj.called else ""
            ),
            file=file,
        )
        for elt in (obj.inputs or []) + obj.body + (obj.outputs or []):
            descend(elt)
    # task
    elif isinstance(obj, Task):
        print(
            "{}task {}{}".format(
                s, obj.name, " (not called)" if show_called and not obj.called else ""
            ),
            file=file,
        )
        for decl in (obj.inputs or []) + obj.postinputs + obj.outputs:
            descend(decl)
    # call
    elif isinstance(obj, Call):
        if obj.name != obj.callee_id[-1]:
            print("{}call {} as {}".format(s, ".".join(obj.callee_id), obj.name), file=file)
        else:
            print("{}call {}".format(s, ".".join(obj.callee_id)), file=file)
    # scatter
    elif isinstance(obj, Scatter):
        print("{}scatter {}".format(s, obj.variable), file=file)
        for elt in obj.body:
            descend(elt)
    # if
    elif isinstance(obj, Conditional):
        print("{}if".format(s), file=file)
        for elt in obj.body:
            descend(elt)
    # decl
    elif isinstance(obj, Decl):
        pass

    descend()


def print_error(exn):
    global quant_warning
    if isinstance(exn, Error.MultipleValidationErrors):
        for exn1 in exn.exceptions:
            print_error(exn1)
    else:
        if isinstance(getattr(exn, "pos", None), SourcePosition):
            print(
                "({} Ln {} Col {}) {}".format(exn.pos.uri, exn.pos.line, exn.pos.column, str(exn)),
                file=sys.stderr,
            )
        else:
            print(str(exn), file=sys.stderr)
        if isinstance(exn, Error.ImportError) and hasattr(exn, "__cause__"):
            print_error(exn.__cause__)
        if isinstance(exn, Error.ValidationError) and exn.source_text:
            # show source excerpt
            lines = exn.source_text.split("\n")
            error_line = lines[exn.pos.line - 1].replace("\t", " ")
            print("    " + error_line, file=sys.stderr)
            end_line = exn.pos.end_line
            end_column = exn.pos.end_column
            if end_line > exn.pos.line:
                end_line = exn.pos.line
                end_column = len(error_line) + 1
            while end_column > exn.pos.column + 1 and error_line[end_column - 2] == " ":
                end_column = end_column - 1
            print(
                "    " + " " * (exn.pos.column - 1) + "^" * (end_column - exn.pos.column),
                file=sys.stderr,
            )
            if isinstance(exn, Error.StaticTypeMismatch) and exn.actual.coerces(
                exn.expected, check_quant=False
            ):
                quant_warning = True


async def read_source(uri, path, importer):
    if uri.startswith("http:") or uri.startswith("https:"):
        fn = os.path.join(
            tempfile.mkdtemp(prefix="miniwdl_import_uri_"),
            os.path.basename(urllib.parse.urlsplit(uri).path),
        )
        urllib.request.urlretrieve(uri, filename=fn)
        with open(fn, "r") as infile:
            return ReadSourceResult(infile.read(), uri)
    elif importer and (
        importer.pos.abspath.startswith("http:") or importer.pos.abspath.startswith("https:")
    ):
        assert not os.path.isabs(uri), "absolute import from downloaded WDL"
        return await read_source(urllib.parse.urljoin(importer.pos.abspath, uri), [], importer)
    return await read_source_default(uri, path, importer)


def fill_run_subparser(subparsers):
    run_parser = subparsers.add_parser(
        "run", help="Run workflow/task locally with built-in runtime [beta test]"
    )
    run_parser.add_argument("uri", metavar="URI", type=str, help="WDL document filename/URI")
    run_parser.add_argument(
        "inputs",
        metavar="input_key=value",
        type=str,
        nargs="*",
        help="Workflow inputs. Arrays may be supplied by repeating, key=value1 key=value2 ...",
    ).completer = runner_input_completer
    group = run_parser.add_argument_group("input")
    group.add_argument(
        "-i",
        "--input",
        metavar="INPUT.json",
        dest="input_file",
        help="Cromwell-style input JSON file; command-line inputs will be merged in",
    )
    group.add_argument(
        "--empty",
        metavar="input_key",
        action="append",
        help="explicitly set an array input to the empty array (to override a default)",
    )
    group.add_argument(
        "--task",
        metavar="TASK_NAME",
        help="name of task to run (for WDL documents with multiple tasks & no workflow)",
    )
    group.add_argument(
        "-j",
        "--json",
        dest="json_only",
        action="store_true",
        help="just print Cromwell-style input JSON to standard output, then exit",
    )
    group = run_parser.add_argument_group("output")
    group.add_argument(
        "-d",
        "--dir",
        metavar="DIR",
        dest="run_dir",
        help="directory under which to create a timestamp-named subdirectory for this run (defaults to current working directory); supply '.' or 'some/dir/.' to instead run in this directory exactly",
    )
    group = run_parser.add_argument_group("task runtime")
    group.add_argument(
        "-@",
        metavar="N",
        dest="max_tasks",
        type=int,
        default=None,
        help="maximum concurrent tasks (default: # host processors, effectively lower when tasks require multiple processors)",
    )
    group.add_argument(
        "--runtime-cpu-max",
        metavar="N",
        type=int,
        default=None,
        help="maximum effective runtime.cpu for any task (default: # host processors)",
    )
    group.add_argument(
        "--runtime-memory-max",
        metavar="N",
        type=str,
        default=None,
        help="maximum effective runtime.memory for any task (default: total host memory)",
    )
    group.add_argument(
        "--runtime-defaults",
        metavar="JSON",
        type=str,
        default=None,
        help="""default runtime settings for all tasks (JSON filename or literal object e.g. '{"maxRetries":2}')""",
    )
    group.add_argument(
        "--copy-input-files",
        action="store_true",
        help="copy input files for each task and mount them read/write (unblocks task commands that mv/rm/write them)",
    )
    group.add_argument(
        "--as-me",
        action="store_true",
        help="run all containers as the invoking user uid:gid (more secure, but potentially blocks task commands e.g. apt-get)",
    )
    run_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="increase logging detail & stream tasks' stderr",
    )
    run_parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable colored logging on terminal (also set by NO_COLOR environment variable)",
    )
    # TODO:
    # way to specify None for an optional value (that has a default)
    return run_parser


def runner(
    uri,
    task=None,
    inputs=[],
    input_file=None,
    empty=[],
    json_only=False,
    runtime_defaults=None,
    runtime_memory_max=None,
    path=None,
    check_quant=True,
    **kwargs,
):
    # load WDL document
    doc = load(uri, path or [], check_quant=check_quant, read_source=read_source)

    # parse and validate the provided inputs
    target, input_env, input_json = runner_input(doc, inputs, input_file, empty, task=task)

    if json_only:
        print(json.dumps(input_json, indent=2))
        sys.exit(0)

    # set up logging
    level = NOTICE_LEVEL
    if kwargs["verbose"]:
        level = VERBOSE_LEVEL
    if kwargs["debug"]:
        level = logging.DEBUG
    if kwargs["no_color"]:
        # picked up by _util.install_coloredlogs()
        os.environ["NO_COLOR"] = os.environ.get("NO_COLOR", "")
    logging.basicConfig(level=level)
    logger = logging.getLogger("miniwdl-run")
    install_coloredlogs(logger)

    for pkg in ["miniwdl", "docker", "lark-parser", "argcomplete", "pygtail"]:
        try:
            logger.debug(importlib_metadata.version(pkg))
        except importlib_metadata.PackageNotFoundError:
            logger.debug(f"{pkg} UNKNOWN")
    logger.debug("dockerd: " + str(docker.from_env().version()))

    rerun_sh = f"pushd {shellquote(os.getcwd())} && miniwdl {' '.join(shellquote(t) for t in sys.argv[1:])}; popd"

    # configuration
    run_kwargs = dict(
        (k, kwargs[k])
        for k in ["copy_input_files", "run_dir", "runtime_cpu_max", "as_me", "max_tasks"]
    )
    if runtime_memory_max:
        run_kwargs["runtime_memory_max"] = parse_byte_size(runtime_memory_max)
    if runtime_defaults:
        if runtime_defaults.lstrip()[0] == "{":
            run_kwargs["runtime_defaults"] = json.loads(runtime_defaults)
        else:
            with open(runtime_defaults, "r") as infile:
                run_kwargs["runtime_defaults"] = json.load(infile)

    ensure_swarm(logger)

    # run & handle any errors
    try:
        rundir, output_env = runtime.run(target, input_env, **run_kwargs)
    except Exception as exn:
        outer_rundir = None
        inner_rundir = None
        while isinstance(exn, runtime.RunFailed):
            exn_rundir = getattr(exn, "run_dir")
            logger.error(_(str(exn), dir=exn_rundir))
            outer_rundir = outer_rundir or exn_rundir
            inner_rundir = exn_rundir
            exn = exn.__cause__
            assert exn
        if isinstance(exn, runtime.task.CommandFailed) and not (
            kwargs["verbose"] or kwargs["debug"]
        ):
            logger.notice(_("standard error", file=getattr(exn, "stderr_file")))
            logger.notice("run with --verbose to include task standard error streams in this log")
        if isinstance(getattr(exn, "pos", None), SourcePosition):
            pos = getattr(exn, "pos")
            logger.error(
                "({} Ln {} Col {}) {}{}".format(
                    pos.uri,
                    pos.line,
                    pos.column,
                    exn.__class__.__name__,
                    (", " + str(exn) if str(exn) else ""),
                )
            )
        else:
            logger.error(f"{exn.__class__.__name__}{(', ' + str(exn) if str(exn) else '')}")
        if outer_rundir:
            with open(os.path.join(outer_rundir, "rerun"), "w") as rerunfile:
                print(rerun_sh, file=rerunfile)
        if kwargs["debug"]:
            raise
        sys.exit(2)

    # report
    with open(os.path.join(rundir, "rerun"), "w") as rerunfile:
        print(rerun_sh, file=rerunfile)
    outputs_json = {"outputs": values_to_json(output_env, namespace=target.name), "dir": rundir}
    print(json.dumps(outputs_json, indent=2))
    return outputs_json


def runner_input_completer(prefix, parsed_args, **kwargs):
    # argcomplete completer for `miniwdl run` and `miniwdl cromwell`
    if "uri" in parsed_args:
        # load document. in the completer setting, we need to substitute the home directory
        # and environment variables
        uri = os.path.expandvars(os.path.expanduser(parsed_args.uri))
        if not (runtime.download.able(uri) or os.path.exists(uri)):
            argcomplete.warn("file not found: " + uri)
            return []
        try:
            doc = load(
                uri,
                path=(parsed_args.path if hasattr(parsed_args, "path") else []),
                check_quant=parsed_args.check_quant,
                read_source=read_source,
            )
        except Exception as exn:
            argcomplete.warn(
                "unable to load {}; try 'miniwdl check' on it ({})".format(uri, str(exn))
            )
            return []
        # resolve target
        if doc.workflow:
            target = doc.workflow
        elif len(doc.tasks) == 1:
            target = doc.tasks[0]
        elif len(doc.tasks) > 1:
            argcomplete.warn("WDL document contains multiple tasks and no workflow")
            return []
        else:
            argcomplete.warn("WDL document is empty")
            return []
        assert target
        # figure the available input names (starting with prefix, if any)
        available_input_names = [nm + "=" for nm in values_to_json(target.available_inputs)]
        if prefix and prefix.find("=") == -1:
            available_input_names = [nm for nm in available_input_names if nm.startswith(prefix)]
        # TODO idea -- complete only required inputs until they're all present, then start
        # completing the non-required inputs. Tricky with arrays, because we want to keep
        # allowing their completion even after already supplied.
        # compute set of inputs already supplied
        return available_input_names


def runner_input(doc, inputs, input_file, empty, task=None, check_required=True):
    """
    - Determine the target workflow/task
    - Check types of supplied inputs
    - Check all required inputs are supplied
    - Return inputs as Env.Bindings[Value.Base]
    """

    # resolve target
    target = None
    if task:
        target = next((t for t in doc.tasks if t.name == task), None)
        if not target:
            die(f"no such task {task} in document")
    elif doc.workflow:
        target = doc.workflow
    elif len(doc.tasks) == 1:
        target = doc.tasks[0]
    elif len(doc.tasks) > 1:
        die("specify --task for WDL document with multiple tasks and no workflow")
    else:
        die("Empty WDL document")
    assert target

    # build up an values env of the provided inputs
    available_inputs = target.available_inputs
    input_env = runner_input_json_file(
        available_inputs, (target.name if isinstance(target, Workflow) else ""), input_file
    )

    # set explicitly empty arrays
    for empty_name in empty or []:
        try:
            decl = available_inputs[empty_name]
        except KeyError:
            die(
                "No such input to {}: {}\n{}".format(
                    target.name, empty_name, runner_input_help(target)
                )
            )
        if not isinstance(decl.type, Type.Array) or decl.type.nonempty:
            die("Cannot set input {} {} to empty array".format(str(decl.type), decl.name))
        input_env = input_env.bind(empty_name, Value.Array(decl.type.item_type, []), decl)

    # add in command-line inputs
    for one_input in inputs:
        if not one_input or not one_input[0].isalpha():
            # let user just see runner_input_help
            die(
                f"{target.name} ({target.pos.uri})\n{'-'*(len(target.name)+len(target.pos.uri)+3)}\n{runner_input_help(target)}"
            )

        # parse [namespace], name, and value
        buf = one_input.split("=", 1)
        if len(buf) != 2 or not buf[0]:
            die("Invalid input name=value pair: " + one_input)
        name, s_value = buf

        # find corresponding input declaration
        try:
            decl = available_inputs[name]
        except KeyError:
            die(
                "No such input to {}: {}\n{}".format(target.name, buf[0], runner_input_help(target))
            )

        # create a Value based on the expected type
        v = runner_input_value(s_value, decl.type)

        # insert value into input_env
        try:
            existing = input_env[name]
        except KeyError:
            existing = None
        if existing:
            if isinstance(v, Value.Array):
                assert isinstance(existing, Value.Array) and v.type.coerces(existing.type)
                existing.value.extend(v.value)
            else:
                die("non-array input {} duplicated\n{}".format(buf[0], runner_input_help(target)))
        else:
            input_env = input_env.bind(name, v, decl)

    # check for missing inputs
    if check_required:
        missing_inputs = values_to_json(target.required_inputs.subtract(input_env))
        if missing_inputs:
            die(
                "missing required inputs for {}: {}\n{}".format(
                    target.name, ", ".join(missing_inputs.keys()), runner_input_help(target)
                )
            )

    # make a pass over the Env to create a dict for Cromwell-style input JSON
    return (
        target,
        input_env,
        values_to_json(input_env, namespace=(target.name if isinstance(target, Workflow) else "")),
    )


def runner_input_json_file(available_inputs, namespace, input_file):
    """
    Load user-supplied inputs JSON file, if any
    """
    ans = Env.Bindings()

    if input_file:
        input_file = input_file.strip()
    if input_file:
        if input_file[0] == "{":
            input_json = json.loads(input_file)
        elif input_file == "-":
            input_json = json.load(sys.stdin)
        else:
            input_json = json.loads(
                asyncio.get_event_loop()
                .run_until_complete(read_source(input_file, [], None))
                .source_text
            )
        ans = values_from_json(input_json, available_inputs, namespace=namespace)

        # join relative file paths to the cwd

        def absolutify_files(v: Value.Base) -> Value.Base:
            if isinstance(v, Value.File):
                if "://" not in v.value and not os.path.isabs(v.value):
                    v.value = os.path.normpath(os.path.join(os.getcwd(), v.value))
            for ch in v.children:
                absolutify_files(ch)
            return v

        ans = ans.map(lambda binding: Env.Binding(binding.name, absolutify_files(binding.value)))

    return ans


def runner_input_help(target):
    # TODO: get help message from parameter_meta
    # TODO: show default values of optionals
    ans = []
    required_inputs = target.required_inputs
    ans.append("\nrequired inputs:")
    for b in required_inputs:
        ans.append("  {} {}".format(str(b.value.type), b.name))
    optional_inputs = target.available_inputs.subtract(target.required_inputs)
    if target.inputs is None:
        # if the target doesn't have an input{} section (pre WDL 1.0), exclude
        # declarations bound to a non-constant expression (heuristic)
        optional_inputs = optional_inputs.filter(
            lambda b: b.value.expr is None or is_constant_expr(b.value.expr)
        )
    if optional_inputs:
        ans.append("\noptional inputs:")
        for b in optional_inputs:
            ans.append("  {} {}".format(str(b.value.type), b.name))
    ans.append("\noutputs:")
    for b in target.effective_outputs:
        ans.append("  {} {}".format(str(b.value), b.name))
    return "\n".join(ans)


def is_constant_expr(expr):
    """
    Decide if the expression is "constant" for the above purposes
    """
    if isinstance(expr, (Expr.Int, Expr.Float, Expr.Boolean)):
        return True
    if isinstance(expr, Expr.String) and (
        len(expr.parts) == 2 or (len(expr.parts) == 3 and isinstance(expr.parts[1], str))
    ):
        return True
    if isinstance(expr, Expr.Array):
        return not [item for item in expr.items if not is_constant_expr(item)]
    # TODO: Pair, Map, Struct???
    return False


def runner_input_value(s_value, ty):
    """
    Given an input value from the command line (right-hand side of =) and the
    WDL type of the corresponding input decl, create an appropriate Value.
    """
    if isinstance(ty, Type.String):
        return Value.String(s_value)
    if isinstance(ty, Type.File):
        downloadable = runtime.download.able(s_value)
        if not (downloadable or os.path.isfile(s_value)):
            die("File not found: " + s_value)
        return Value.File(
            os.path.abspath(os.path.expanduser(s_value)) if not downloadable else s_value
        )
    if isinstance(ty, Type.Boolean):
        if s_value == "true":
            return Value.Boolean(True)
        if s_value == "false":
            return Value.Boolean(False)
        die("Boolean input should be true or false instead of {}".format(s_value))
    if isinstance(ty, Type.Int):
        return Value.Int(int(s_value))
    if isinstance(ty, Type.Float):
        return Value.Float(float(s_value))
    if isinstance(ty, Type.Array) and isinstance(
        ty.item_type, (Type.String, Type.File, Type.Int, Type.Float)
    ):
        # just produce a length-1 array, to be combined ex post facto
        return Value.Array(ty.item_type, [runner_input_value(s_value, ty.item_type)])
    return die(
        "No command-line support yet for inputs of type {}; workaround: specify in JSON file with --input".format(
            str(ty)
        )
    )


def fill_run_self_test_subparser(subparsers):
    run_parser = subparsers.add_parser(
        "run_self_test",
        help="Run a trivial workflow to smoke-test installation, docker permission, etc.",
    )
    run_parser.add_argument(
        "--dir",
        metavar="DIR",
        default=None,
        help="run the test in specified directory, instead of some new temporary directory",
    )
    run_parser.add_argument(
        "--as-me", action="store_true", help="run all containers as the current user uid:gid"
    )
    return run_parser


def run_self_test(**kwargs):
    dn = kwargs["dir"]
    if not dn:
        dn = tempfile.mkdtemp(prefix="miniwdl_run_self_test_")
    with open(os.path.join(dn, "test.wdl"), "w") as outfile:
        outfile.write(
            """
            version 1.0
            workflow hello_caller {
                input {
                    File who
                }
                scatter (name in read_lines(who)) {
                    call hello {
                        input:
                            who = write_lines([name])
                    }
                    if (defined(hello.message)) {
                        String msg = read_string(select_first([hello.message]))
                    }
                }
                output {
                    Array[String] messages = select_all(msg)
                }
            }
            task hello {
                input {
                    File who
                }
                command {
                    if grep -v ^\# "${who}" ; then
                        echo "Hello, $(cat ${who})!" | tee message.txt 1>&2
                    fi
                }
                output {
                    File? message = "message.txt"
                }
                runtime {
                    docker: "ubuntu:18.04"
                    memory: "1G"
                }
            }
            """
        )

    check(uri=[os.path.join(dn, "test.wdl")])

    argv = [
        "run",
        os.path.join(dn, "test.wdl"),
        "who=https://raw.githubusercontent.com/chanzuckerberg/miniwdl/master/tests/alyssa_ben.txt",
        "--dir",
        dn,
        "--debug",
    ]
    if kwargs["as_me"]:
        argv.append("--as-me")
    try:
        outputs = main(argv)["outputs"]
        assert len(outputs["hello_caller.messages"]) == 2
        assert outputs["hello_caller.messages"][0].rstrip() == "Hello, Alyssa P. Hacker!"
        assert outputs["hello_caller.messages"][1].rstrip() == "Hello, Ben Bitdiddle!"
    except SystemExit as exn:
        assert getattr(exn, "code") == 0  # because of --debug
    except:
        atexit.register(
            lambda: print(
                "* Hint: ensure Docker is installed, running, and user has permission to control it per https://docs.docker.com/install/linux/linux-postinstall/#manage-docker-as-a-non-root-user",
                file=sys.stderr,
            )
        )
        raise

    print("miniwdl run_self_test OK", file=sys.stderr)
    if os.geteuid() == 0:
        print(
            "* Hint: non-root users should be able to run miniwdl if they have permission to control Docker per https://docs.docker.com/install/linux/linux-postinstall/#manage-docker-as-a-non-root-user",
            file=sys.stderr,
        )


def fill_cromwell_subparser(subparsers):
    cromwell_parser = subparsers.add_parser(
        "cromwell", help="Run workflow/task locally using Cromwell " + CROMWELL_VERSION
    )
    cromwell_parser.add_argument("uri", metavar="URI", type=str, help="WDL document filename/URI")
    cromwell_parser.add_argument(
        "inputs",
        metavar="input_key=value",
        type=str,
        nargs="*",
        help="Workflow inputs. Arrays may be supplied by repeating, key=value1 key=value2 ...",
    ).completer = runner_input_completer
    group = cromwell_parser.add_argument_group("input")
    group.add_argument(
        "--empty",
        metavar="input_key",
        action="append",
        help="explicitly set an array input to the empty array (to override a default)",
    )
    group.add_argument(
        "-i",
        "--input",
        metavar="INPUT.json",
        dest="input_file",
        help="file with Cromwell-style input JSON; command-line inputs will be merged in",
    )
    group.add_argument(
        "-j",
        "--json",
        dest="json_only",
        action="store_true",
        help="just print Cromwell-style input JSON to standard output, then exit",
    )
    group = cromwell_parser.add_argument_group("Cromwell configuration")
    group.add_argument(
        "-d",
        "--dir",
        metavar="DIR",
        dest="rundir",
        help="directory under which to create a timestamp-named subdirectory for this run (defaults to current working directory); supply '.' or 'some/dir/.' to instead operate in this directory directly",
    )
    group.add_argument(
        "-o",
        "--options",
        metavar="OPTIONS.json",
        dest="options_file",
        help="file with Cromwell workflow options JSON",
    )
    group.add_argument(
        "-c",
        "--config",
        metavar="CONFIG.conf",
        dest="config",
        type=str,
        help="Cromwell backend configuration CONF file path (also set by CROMWELL_CONFIG environment variable)",
    )
    group.add_argument(
        "-r",
        "--jar",
        metavar="jarfile",
        dest="jarfile",
        type=str,
        help="Cromwell jarfile file path (also set by CROMWELL_JAR environment variable). Overrides default behavior of downloading a hard-coded version",
    )
    # TODO:
    # way to specify None for an optional value (that has a default)
    return cromwell_parser


def cromwell(
    uri,
    inputs,
    input_file,
    json_only,
    empty,
    check_quant,
    rundir=None,
    options_file=None,
    jarfile=None,
    config=None,
    path=None,
    **kwargs,
):
    path = path or []

    # load WDL document
    doc = load(uri, path, check_quant=check_quant, read_source=read_source)

    # validate the provided inputs and prepare Cromwell-style JSON
    target, _, input_json = runner_input(doc, inputs, input_file, empty)

    if json_only:
        print(json.dumps(input_json, indent=2))
        sys.exit(0)

    rundir = provision_run_dir(target.name, rundir)
    os.makedirs(os.path.join(rundir, "cromwell"), exist_ok=False)

    # write the JSON inputs file
    input_json_filename = None
    print("Cromwell input: " + json.dumps(input_json, indent=2), file=sys.stderr)
    input_json_filename = os.path.join(rundir, "inputs.json")
    with open(input_json_filename, "w") as outfile:
        print(json.dumps(input_json, indent=2), file=outfile)

    # write Cromwell options
    cromwell_options = {"final_workflow_log_dir": os.path.join(rundir, "cromwell")}

    if options_file:
        with open(options_file, "r") as infile:
            more_options = json.load(infile)
            for k in more_options:
                if k not in ["final_workflow_log_dir", "use_relative_output_paths"]:
                    cromwell_options[k] = more_options[k]

    cromwell_options_filename = os.path.join(rundir, "cromwell", "options.json")
    with open(cromwell_options_filename, "w") as options_json:
        print(json.dumps(cromwell_options, indent=2), file=options_json)

    # setup Cromwell config file
    config_setting = None
    if config:
        config_setting = "-Dconfig.file={}".format(config)
    elif "CROMWELL_CONFIG" in os.environ:
        config_setting = "-Dconfig.file={}".format(os.getenv("CROMWELL_CONFIG"))

    # launch Cromwell
    jarpath = ensure_cromwell_jar(jarfile)
    cromwell_cmd = ["java", "-DLOG_LEVEL=info", "-DLOG_MODE=pretty"]
    cromwell_cmd.extend([config_setting] if config_setting else [])
    cromwell_cmd.extend(
        [
            "-jar",
            jarpath,
            "run",
            (os.path.abspath(uri) if "://" not in uri else uri),
            "-o",
            cromwell_options_filename,
            "-i",
            input_json_filename,
        ]
    )

    for p in path:
        # FIXME issue #131
        cromwell_cmd.append("--imports")
        cromwell_cmd.append(p)
    print(" ".join(["+"] + [shellquote(s) for s in cromwell_cmd]), file=sys.stderr)
    proc = subprocess.Popen(
        cromwell_cmd, cwd=os.path.join(rundir, "cromwell"), stdout=subprocess.PIPE
    )

    # stream in Cromwell stdout, which mixes a bunch of stuff. tee it to stderr
    # while recording it so we can go back to look for the output JSON later.
    cromwell_output_lines = []
    while proc.poll() is None:
        line = proc.stdout.readline()
        if line:
            line = str(line, "utf-8").rstrip()
            print(line, file=sys.stderr)
            cromwell_output_lines.append(line)
    assert isinstance(proc.returncode, int)

    # deal with Cromwell outputs

    # remove world-write permissions from created temp files
    subprocess.call(["chmod", "-Rf", "o-w", rundir])

    if proc.returncode == 0:
        # sniff for the outputs JSON as the last subsequence of stdout lines
        # delimited by { and }
        last_lbrace = None
        last_rbrace = None
        try:
            last_lbrace = max(loc for loc, val in enumerate(cromwell_output_lines) if val == "{")
            last_rbrace = max(loc for loc, val in enumerate(cromwell_output_lines) if val == "}")
        except ValueError:
            pass
        try:
            if last_lbrace is None or last_rbrace is None or last_lbrace >= last_rbrace:
                raise KeyError
            outputs_json = json.loads(
                "\n".join(cromwell_output_lines[last_lbrace : (last_rbrace + 1)])
            )
        except:
            die("failed to find outputs JSON in Cromwell standard output")

        # load the outputs, make file links and outputs.json
        outputs = values_from_json(
            outputs_json["outputs"], target.effective_outputs, namespace=target.name
        )
        outputs = runtime.link_outputs(outputs, rundir)

        assert "dir" not in outputs_json
        outputs_json["dir"] = rundir
        outputs_json["outputs"] = values_to_json(outputs, namespace=target.name)
        print(json.dumps(outputs_json, indent=2))
        with open(os.path.join(rundir, "outputs.json"), "w") as outfile:
            print(json.dumps(outputs_json["outputs"], indent=2), file=outfile)

    sys.exit(proc.returncode)


CROMWELL_VERSION = "47"
CROMWELL_JAR_SIZE = 196_298_777


def ensure_cromwell_jar(jarfile=None):
    """
    Return local path to Cromwell JAR file, first downloading it if necessary.
    """
    if jarfile:
        jarpath = jarfile
    elif "CROMWELL_JAR" in os.environ:
        jarpath = os.getenv("CROMWELL_JAR")
    else:
        CROMWELL_JAR_URL = "https://github.com/broadinstitute/cromwell/releases/download/{v}/cromwell-{v}.jar".format(
            v=CROMWELL_VERSION
        )
        CROMWELL_JAR_NAME = os.path.basename(CROMWELL_JAR_URL)

        jarpath = os.path.join(tempfile.gettempdir(), CROMWELL_JAR_NAME)
        try:
            if os.path.getsize(jarpath) == CROMWELL_JAR_SIZE:
                return jarpath
        except:
            pass
        print(
            "Downloading Cromwell to {}; it'll be reused there, or specify --jar with your own version".format(
                jarpath
            ),
            file=sys.stderr,
        )
        urllib.request.urlretrieve(CROMWELL_JAR_URL, filename=jarpath)
        assert os.path.getsize(jarpath) == CROMWELL_JAR_SIZE, (
            "unexpected size of downloaded " + jarpath
        )
    return jarpath


def fill_localize_subparser(subparsers):
    localize_parser = subparsers.add_parser(
        "localize",
        help="Download URIs found in Cromwell-style input JSON and rewrite",
        description=f"Download URIs found in Cromwell-style input JSON, and rewrite it with the local filenames.",
    )
    localize_parser.add_argument(
        "wdlfile", metavar="DOC.wdl", type=str, help="WDL document filename/URI"
    )
    localize_parser.add_argument(
        "infile",
        metavar="INPUT.json",
        type=str,
        help="input JSON filename (- for standard input) or literal object",
    )
    localize_parser.add_argument(
        "name",
        metavar="NAME",
        type=str,
        nargs="?",
        default=None,
        help="short name to include in local paths (default: basename of JSON file)",
    )
    localize_parser.add_argument(
        "-d",
        "--dir",
        metavar="DIR",
        dest="run_dir",
        help="base directory in which to store downloaded files",
    )
    localize_parser.add_argument(
        "-o",
        metavar="LOCAL.json",
        type=str,
        dest="outfile",
        help="write transformed JSON to file instead of standard output",
    )
    localize_parser.add_argument(
        "--task",
        metavar="TASK_NAME",
        help="name of task (for WDL documents with multiple tasks & no workflow)",
    )
    return localize_parser


def localize(
    wdlfile, infile, name=None, outfile=None, task=None, path=None, check_quant=True, **kwargs
):
    # load WDL document
    doc = load(wdlfile, path or [], check_quant=check_quant, read_source=read_source)

    # parse the provided input JSON
    target, input_env, input_json = runner_input(
        doc, [], infile, [], task=task, check_required=False
    )

    # read input JSON
    name = name or os.path.basename(infile).split(".")[0]

    # scan for Files that appear to be downloadable URIs
    def scan(x):
        if isinstance(x, Value.File) and runtime.download.able(x.value):
            yield x.value
        for y in x.children:
            yield from scan(y)

    uris = set()
    for b in input_env:
        uris |= set(scan(b.value))

    if uris:
        logging.basicConfig(level=NOTICE_LEVEL)

        # cheesy trick: provide the list of URIs as File inputs to a dummy workflow, causing the
        # runtime to download them
        localizer_wdl = (
            """
            version 1.0
            workflow localize_%s {
                input {
                    Array[File] uris
                }
                output {
                    Array[File] files = uris
                }
            }
            """
            % name
        )
        localizer = parse_document(localizer_wdl)
        localizer.typecheck()
        subdir, outputs = runtime.run(
            localizer.workflow,
            values_from_json({"uris": list(uris)}, localizer.workflow.available_inputs),
            **kwargs,
        )

        # recover the mapping of URIs to downloaded files
        uri_to_file = {}
        assert isinstance(outputs["files"], Value.Array)
        for uri, elt in zip(uris, outputs["files"].value):
            assert isinstance(elt, Value.File) and os.path.isfile(elt.value)
            uri_to_file[uri] = elt.value

        # rewrite the input Env to replace URIs with filenames
        def rewrite(x):
            if isinstance(x, Value.File) and x.value in uri_to_file:
                x.value = uri_to_file[x.value]
            for y in x.children:
                rewrite(y)

        for b in input_env:
            rewrite(b.value)

    # write out the possibly-modified JSON
    result_json = values_to_json(
        input_env, namespace=(target.name if isinstance(target, Workflow) else "")
    )
    if outfile in [None, "", "-"]:
        print(json.dumps(result_json, indent=2))
    else:
        with open(outfile, "w") as outp:
            print(json.dumps(result_json, indent=2), file=outp)


def die(msg, status=2):
    print("\n" + msg + "\n", file=sys.stderr)
    sys.exit(status)
