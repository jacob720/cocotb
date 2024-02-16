# Copyright (c) 2013, 2018 Potential Ventures Ltd
# Copyright (c) 2013 SolarFlare Communications Inc
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of Potential Ventures Ltd,
#       SolarFlare Communications Inc nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL POTENTIAL VENTURES LTD BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""All things relating to regression capabilities."""

import functools
import hashlib
import inspect
import logging
import os
import pdb
import random
import re
import sys
import time
import warnings
from importlib import import_module
from itertools import product
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
)

import cocotb
from cocotb import ANSI, simulator
from cocotb._outcomes import Error, Outcome, capture
from cocotb._xunit_reporter import XUnitReporter
from cocotb.result import SimFailure, TestSuccess
from cocotb.task import Task, _RunningTest
from cocotb.utils import (
    get_sim_time,
    remove_traceback_frames,
    want_color_output,
)

_pdb_on_exception = "COCOTB_PDB_ON_EXCEPTION" in os.environ


_logger = logging.getLogger(__name__)

_Failed: Type[BaseException]
try:
    import pytest
except ModuleNotFoundError:
    _Failed = AssertionError
else:
    try:
        with pytest.raises(Exception):
            pass
    except BaseException as _raises_e:
        _Failed = type(_raises_e)
    else:
        assert False, "pytest.raises doesn't raise an exception when it fails"


class Test:
    """A cocotb test in a regression.

    Args:
        func:
            The test function object.

        name:
            The name of the test function.
            Defaults to ``func.__qualname__`` (the dotted path to the test function in the module).

        module:
            The name of the module containing the test function.
            Defaults to ``func.__module__`` (the name of the module containing the test function).

        doc:
            The docstring for the test.
            Defaults to ``func.__doc__`` (the docstring of the test function).

        timeout_time:
            Simulation time duration before the test is forced to fail with a :exc:`~cocotb.result. SimTimeoutError`.

        timeout_unit:
            Units of ``timeout_time``, accepts any units that :class:`~cocotb.triggers.Timer` does.

        expect_fail:
            If ``True`` and the test fails a functional check via an ``assert`` statement, :pytest:class:`pytest.raises`,
            :pytest:class:`pytest.warns`, or :pytest:class:`pytest.deprecated_call`, the test is considered to have passed.
            If ``True`` and the test passes successfully, the test is considered to have failed.

        expect_error:
            Mark the result as a pass only if one of the given exception types is raised in the test.

        skip:
            Don't execute this test as part of the regression.
            The test can still be run manually by setting :envvar:`TESTCASE`.

        stage:
            Order tests logically into stages.
            Tests from earlier stages are run before tests from later stages.
    """

    _id_count = 0  # used by the RegressionManager to sort tests in definition order

    def __init__(
        self,
        *,
        func: Callable[..., Coroutine[Any, Any, None]],
        name: Optional[str] = None,
        module: Optional[str] = None,
        doc: Optional[str] = None,
        timeout_time: Optional[float] = None,
        timeout_unit: str = "step",
        expect_fail: bool = False,
        expect_error: Union[Type[Exception], Sequence[Type[Exception]]] = (),
        skip: bool = False,
        stage: int = 0,
    ):
        self._id = self._id_count
        type(self)._id_count += 1

        if timeout_time is not None:
            co = func  # must save ref because we overwrite variable "func"

            @functools.wraps(func)
            async def func(*args, **kwargs):
                running_co = Task(co(*args, **kwargs))

                try:
                    res = await cocotb.triggers.with_timeout(
                        running_co, self.timeout_time, self.timeout_unit
                    )
                except cocotb.result.SimTimeoutError:
                    running_co.kill()
                    raise
                else:
                    return res

        self.func = func
        self.timeout_time = timeout_time
        self.timeout_unit = timeout_unit
        self.expect_fail = expect_fail
        self.expect_error = expect_error
        self.skip = skip
        self.stage = stage
        self.name = self.func.__qualname__ if name is None else name
        self.module = self.func.__module__ if module is None else module
        self.doc = self.func.__doc__ if doc is None else doc
        if self.doc is not None:
            self.doc = inspect.cleandoc(self.doc)
        self.fullname = f"{self.module}.{self.name}"


def _format_doc(docstring: Union[str, None]) -> str:
    if docstring is None:
        return ""
    else:
        brief = docstring.split("\n")[0]
        return f"\n    {brief}"


class RegressionManager:
    """Encapsulates all regression capability into a single place"""

    def __init__(self) -> None:
        self._test: Test
        self._test_task: Task[None]
        self._test_start_time: float
        self._test_start_sim_time: float
        self.log = _logger
        self._regression_start_time: float
        self._test_results: List[Dict[str, Any]] = []
        self.ntests = 0
        self.count = 0
        self.passed = 0
        self.skipped = 0
        self.failures = 0
        self._tearing_down = False
        self._test_queue: List[Test] = []
        self._filtered_tests: List[Test] = []

        # Setup XUnit
        ###################

        results_filename = os.getenv("COCOTB_RESULTS_FILE", "results.xml")
        suite_name = os.getenv("RESULT_TESTSUITE", "all")
        package_name = os.getenv("RESULT_TESTPACKAGE", "all")

        self.xunit = XUnitReporter(filename=results_filename)
        self.xunit.add_testsuite(name=suite_name, package=package_name)
        self.xunit.add_property(name="random_seed", value=str(cocotb.RANDOM_SEED))

    def discover_tests(self, *modules: str) -> None:
        """Discover tests in files automatically.

        Should be called before :meth:`start_regression` is called.

        Args:
            modules: Name of module where tests are found.

        Raises:
            RuntimeError: If no tests are found.
        """
        for module_name in modules:
            self.log.debug("Searching for tests in module %s", module_name)
            self.log.debug("Python Path: %s", ",".join(sys.path))
            self.log.debug("PWD: %s", os.getcwd())
            mod = import_module(module_name)

            if not hasattr(mod, "__cocotb_tests__"):
                raise RuntimeError(
                    f"No tests were discovered in module: {module_name!r}"
                )

            for test in mod.__cocotb_tests__:
                self.register_test(test)

        # error if no tests were discovered
        if not self._test_queue:
            modules_str = ", ".join(repr(m) for m in modules)
            raise RuntimeError(f"No tests were discovered in any module: {modules_str}")

    def filter_tests(self, *filters: str) -> None:
        """Filter discovered tests.

        Only those tests which match at least one of the given filters are included;
        the rest are excluded.

        Should be called before :meth:`start_regression` is called.

        Args:
            filters: A regex pattern for test names.
                A match *includes* the test.
        """
        included: List[Test] = []
        excluded: List[Test] = []

        # include tests that match filter
        for test in self._test_queue:
            for filter in filters:
                if re.search(filter, test.fullname):
                    included.append(test)
                    break
            else:
                self.log.debug("Filtered out test %s", test.fullname)
                excluded.append(test)

        if not included:
            self.log.warning(
                "No tests left after filtering with: %s",
                ", ".join(repr(f) for f in filters),
            )

        self._test_queue = included
        self._filtered_tests = excluded

    def register_test(self, test: Test) -> None:
        """Register a test with the RegressionManager.

        Should be called before :meth:`start_regression` is called.

        Args:
            test: The test object to register.
        """
        self.log.debug("Registered test %r", test.fullname)
        self._test_queue.append(test)

    @classmethod
    def setup_pytest_assertion_rewriting(cls, *modules: str) -> None:
        try:
            import pytest
        except ImportError:
            _logger.info(
                "pytest not found, install it to enable better AssertionError messages"
            )
            return
        try:
            # Install the assertion rewriting hook, which must be done before we
            # import the test modules.
            from _pytest.assertion import install_importhook
            from _pytest.config import Config

            pytest_conf = Config.fromdictargs(
                {}, ["--capture=no", "-o", "python_files=*.py"]
            )
            install_importhook(pytest_conf)
        except Exception:
            _logger.exception(
                "Configuring the assertion rewrite hook using pytest {} failed. "
                "Please file a bug report!".format(pytest.__version__)
            )

    def start_regression(self) -> None:
        """Start the regression.

        Should be called only once after :meth:`discover_tests` is called.
        """
        self._test_queue.sort(key=lambda test: (test.stage, test._id))
        self.ntests = len(self._test_queue)
        self.count = 1

        # record exclusions
        for test in self._filtered_tests:
            self._record_test_excluded(test)

        self._regression_start_time = time.time()
        self._execute()

    def _tear_down(self) -> None:
        # prevent re-entering the tear down procedure
        if not self._tearing_down:
            self._tearing_down = True
        else:
            return

        # fail remaining tests
        while True:
            test = self._next_test()
            if test is None:
                break
            self._record_result(
                test=test, outcome=Error(SimFailure), wall_time_s=0, sim_time_ns=0
            )

        # Write out final log messages
        self._log_test_summary()

        # Generate output reports
        self.xunit.write()

        # Setup simulator finalization
        simulator.stop_simulator()
        cocotb._stop_user_coverage()
        cocotb._stop_library_coverage()

    def _next_test(self) -> Optional[Test]:
        """Get the next test to run"""
        if not self._test_queue:
            return None
        return self._test_queue.pop(0)

    def _handle_result(self, test: Task) -> None:
        """Handle a test completing.

        Dump result to XML and schedule the next test (if any). Entered by the scheduler.

        Args:
            test: The test that completed
        """
        assert test is self._test_task

        real_time = time.time() - self._test_start_time
        sim_time_ns = get_sim_time("ns") - self._test_start_sim_time

        self._record_result(
            test=self._test,
            outcome=self._test_task._outcome,
            wall_time_s=real_time,
            sim_time_ns=sim_time_ns,
        )

        self._execute()

    def _init_test(self, test: Test) -> Optional[Task]:
        """Initialize a test.

        Record outcome if the initialization fails.
        Record skip if the test is skipped.
        Save the initialized test if it successfully initializes.
        """

        if test.skip:
            self._record_test_skipped(test)
            return None

        test_init_outcome = capture(test.func, cocotb.top)

        if isinstance(test_init_outcome, Error):
            self.log.error(
                "Failed to initialize test %s",
                test.name,
                exc_info=test_init_outcome.error,
            )
            self._record_result(test, test_init_outcome, 0, 0)
            return None

        # seed random number generator based on test module, name, and RANDOM_SEED
        hasher = hashlib.sha1()
        hasher.update(test.fullname.encode())
        seed = cocotb.RANDOM_SEED + int(hasher.hexdigest(), 16)
        random.seed(seed)

        return _RunningTest(test_init_outcome.get(), test.name)

    def _score_test(self, test: Test, outcome: Outcome) -> Tuple[bool, bool]:
        """
        Given a test and the test's outcome, determine if the test met expectations and log pertinent information
        """

        # scoring outcomes
        result_pass = True
        sim_failed = False

        try:
            outcome.get()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            result = remove_traceback_frames(e, ["_score_test", "get"])
        else:
            result = TestSuccess()

        if (
            isinstance(result, TestSuccess)
            and not test.expect_fail
            and not test.expect_error
        ):
            self._log_test_passed(test, None, None)

        elif isinstance(result, TestSuccess) and test.expect_error:
            self._log_test_failed(test, None, "passed but we expected an error")
            result_pass = False

        elif isinstance(result, TestSuccess):
            self._log_test_failed(test, None, "passed but we expected a failure")
            result_pass = False

        elif isinstance(result, SimFailure):
            if isinstance(result, test.expect_error):
                self._log_test_passed(test, result, "errored as expected")
            else:
                self.log.error("Test error has lead to simulator shutting us down")
                result_pass = False
            # whether we expected it or not, the simulation has failed unrecoverably
            sim_failed = True

        elif isinstance(result, (AssertionError, _Failed)) and test.expect_fail:
            self._log_test_passed(test, result, "failed as expected")

        elif test.expect_error:
            if isinstance(result, test.expect_error):
                self._log_test_passed(test, result, "errored as expected")
            else:
                self._log_test_failed(test, result, "errored with unexpected type ")
                result_pass = False

        else:
            self._log_test_failed(test, result, None)
            result_pass = False

            if _pdb_on_exception:
                pdb.post_mortem(result.__traceback__)

        return result_pass, sim_failed

    def _get_lineno(self, test: Test) -> None:
        try:
            return inspect.getsourcelines(test.func)[1]
        except OSError:
            return 1

    def _log_test_passed(
        self, test: Test, result: Optional[Exception] = None, msg: Optional[str] = None
    ) -> None:
        start_hilight = ANSI.COLOR_PASSED if want_color_output() else ""
        stop_hilight = ANSI.COLOR_DEFAULT if want_color_output() else ""
        if msg is None:
            rest = ""
        else:
            rest = f": {msg}"
        if result is None:
            result_was = ""
        else:
            result_was = f" (result was {type(result).__qualname__})"
        self.log.info(
            f"{test.name} {start_hilight}passed{stop_hilight}{rest}{result_was}"
        )

    def _log_test_failed(
        self, test: Test, result: Optional[Exception] = None, msg: Optional[str] = None
    ) -> None:
        start_hilight = ANSI.COLOR_FAILED if want_color_output() else ""
        stop_hilight = ANSI.COLOR_DEFAULT if want_color_output() else ""
        if msg is None:
            rest = ""
        else:
            rest = f": {msg}"
        self.log.info(
            f"{test.name} {start_hilight}failed{stop_hilight}{rest}",
            exc_info=result,
        )

    def _record_test_excluded(self, test: Test) -> None:
        lineno = self._get_lineno(test)

        self.xunit.add_testcase(
            name=test.name,
            classname=test.module,
            file=inspect.getfile(test.func),
            lineno=repr(lineno),
            time=repr(0),
            sim_time_ns=repr(0),
            ratio_time=repr(0),
        )
        self.xunit.add_skipped()

    def _record_test_skipped(self, test: Test) -> None:
        hilight_start = ANSI.COLOR_SKIPPED if want_color_output() else ""
        hilight_end = ANSI.COLOR_DEFAULT if want_color_output() else ""
        # Want this to stand out a little bit
        self.log.info(
            "%sskipping%s %s (%d/%d)%s",
            hilight_start,
            hilight_end,
            test.name,
            self.count,
            self.ntests,
            _format_doc(test.doc),
        )
        lineno = self._get_lineno(test)

        self.xunit.add_testcase(
            name=test.name,
            classname=test.module,
            file=inspect.getfile(test.func),
            lineno=repr(lineno),
            time=repr(0),
            sim_time_ns=repr(0),
            ratio_time=repr(0),
        )
        self.xunit.add_skipped()

        self._test_results.append(
            {
                "test": test.fullname,
                "pass": None,
                "sim": 0,
                "real": 0,
            }
        )

        self.skipped += 1
        self.count += 1

    def _record_result(
        self,
        test: Test,
        outcome: Outcome,
        wall_time_s: float,
        sim_time_ns: float,
    ) -> None:
        ratio_time = self._safe_divide(sim_time_ns, wall_time_s)
        lineno = self._get_lineno(test)

        self.xunit.add_testcase(
            name=test.name,
            classname=test.module,
            file=inspect.getfile(test.func),
            lineno=repr(lineno),
            time=repr(wall_time_s),
            sim_time_ns=repr(sim_time_ns),
            ratio_time=repr(ratio_time),
        )

        test_pass, sim_failed = self._score_test(test, outcome)
        if not test_pass:
            self.xunit.add_failure(
                message=f"Test failed with RANDOM_SEED={cocotb.RANDOM_SEED}"
            )
            self.failures += 1
        else:
            self.passed += 1
        self.count += 1

        self._test_results.append(
            {
                "test": test.fullname,
                "pass": test_pass,
                "sim": sim_time_ns,
                "real": wall_time_s,
                "ratio": ratio_time,
            }
        )

        if sim_failed:
            self._tear_down()
            return

    def _execute(self) -> None:
        while True:
            self._test = self._next_test()
            if self._test is None:
                return self._tear_down()

            self._test_task = self._init_test(self._test)
            if self._test_task is not None:
                return self._start_test()

    def _start_test(self) -> None:
        # Want this to stand out a little bit
        start = ""
        end = ""
        if want_color_output():
            start = ANSI.COLOR_TEST
            end = ANSI.COLOR_DEFAULT

        self.log.info(
            "%srunning%s %s (%d/%d)%s",
            start,
            end,
            self._test.name,
            self.count,
            self.ntests,
            _format_doc(self._test.doc),
        )

        self._test_start_time = time.time()
        self._test_start_sim_time = get_sim_time("ns")
        cocotb.scheduler._add_test(self._test_task)

    def _log_test_summary(self) -> None:
        real_time = time.time() - self._regression_start_time
        sim_time_ns = get_sim_time("ns")
        ratio_time = self._safe_divide(sim_time_ns, real_time)

        if len(self._test_results) == 0:
            return

        TEST_FIELD = "TEST"
        RESULT_FIELD = "STATUS"
        SIM_FIELD = "SIM TIME (ns)"
        REAL_FIELD = "REAL TIME (s)"
        RATIO_FIELD = "RATIO (ns/s)"
        TOTAL_NAME = f"TESTS={self.ntests} PASS={self.passed} FAIL={self.failures} SKIP={self.skipped}"

        TEST_FIELD_LEN = max(
            len(TEST_FIELD),
            len(TOTAL_NAME),
            len(max([x["test"] for x in self._test_results], key=len)),
        )
        RESULT_FIELD_LEN = len(RESULT_FIELD)
        SIM_FIELD_LEN = len(SIM_FIELD)
        REAL_FIELD_LEN = len(REAL_FIELD)
        RATIO_FIELD_LEN = len(RATIO_FIELD)

        header_dict = dict(
            a=TEST_FIELD,
            b=RESULT_FIELD,
            c=SIM_FIELD,
            d=REAL_FIELD,
            e=RATIO_FIELD,
            a_len=TEST_FIELD_LEN,
            b_len=RESULT_FIELD_LEN,
            c_len=SIM_FIELD_LEN,
            d_len=REAL_FIELD_LEN,
            e_len=RATIO_FIELD_LEN,
        )

        LINE_LEN = (
            3
            + TEST_FIELD_LEN
            + 2
            + RESULT_FIELD_LEN
            + 2
            + SIM_FIELD_LEN
            + 2
            + REAL_FIELD_LEN
            + 2
            + RATIO_FIELD_LEN
            + 3
        )

        LINE_SEP = "*" * LINE_LEN + "\n"

        summary = ""
        summary += LINE_SEP
        summary += "** {a:<{a_len}}  {b:^{b_len}}  {c:>{c_len}}  {d:>{d_len}}  {e:>{e_len}} **\n".format(
            **header_dict
        )
        summary += LINE_SEP

        test_line = "** {a:<{a_len}}  {start}{b:^{b_len}}{end}  {c:>{c_len}.2f}   {d:>{d_len}.2f}   {e:>{e_len}}  **\n"
        for result in self._test_results:
            hilite = ""
            lolite = ""

            if result["pass"] is None:
                ratio = "-.--"
                pass_fail_str = "SKIP"
                if want_color_output():
                    hilite = ANSI.COLOR_SKIPPED
                    lolite = ANSI.COLOR_DEFAULT
            elif result["pass"]:
                ratio = format(result["ratio"], "0.2f")
                pass_fail_str = "PASS"
                if want_color_output():
                    hilite = ANSI.COLOR_PASSED
                    lolite = ANSI.COLOR_DEFAULT
            else:
                ratio = format(result["ratio"], "0.2f")
                pass_fail_str = "FAIL"
                if want_color_output():
                    hilite = ANSI.COLOR_FAILED
                    lolite = ANSI.COLOR_DEFAULT

            test_dict = dict(
                a=result["test"],
                b=pass_fail_str,
                c=result["sim"],
                d=result["real"],
                e=ratio,
                a_len=TEST_FIELD_LEN,
                b_len=RESULT_FIELD_LEN,
                c_len=SIM_FIELD_LEN - 1,
                d_len=REAL_FIELD_LEN - 1,
                e_len=RATIO_FIELD_LEN - 1,
                start=hilite,
                end=lolite,
            )

            summary += test_line.format(**test_dict)

        summary += LINE_SEP

        summary += test_line.format(
            a=TOTAL_NAME,
            b="",
            c=sim_time_ns,
            d=real_time,
            e=format(ratio_time, "0.2f"),
            a_len=TEST_FIELD_LEN,
            b_len=RESULT_FIELD_LEN,
            c_len=SIM_FIELD_LEN - 1,
            d_len=REAL_FIELD_LEN - 1,
            e_len=RATIO_FIELD_LEN - 1,
            start="",
            end="",
        )

        summary += LINE_SEP

        self.log.info(summary)

    @staticmethod
    def _safe_divide(a: float, b: float) -> float:
        try:
            return a / b
        except ZeroDivisionError:
            if a == 0:
                return float("nan")
            else:
                return float("inf")


F = TypeVar("F", bound=Callable[..., Coroutine[Any, Any, None]])


class TestFactory(Generic[F]):
    """Factory to automatically generate tests.

    Args:
        test_function: A Callable that returns the test Coroutine.
            Must take *dut* as the first argument.
        *args: Remaining arguments are passed directly to the test function.
            Note that these arguments are not varied. An argument that
            varies with each test must be a keyword argument to the
            test function.
        **kwargs: Remaining keyword arguments are passed directly to the test function.
            Note that these arguments are not varied. An argument that
            varies with each test must be a keyword argument to the
            test function.

    Assuming we have a common test function that will run a test. This test
    function will take keyword arguments (for example generators for each of
    the input interfaces) and generate tests that call the supplied function.

    This Factory allows us to generate sets of tests based on the different
    permutations of the possible arguments to the test function.

    For example, if we have a module that takes backpressure, has two configurable
    features where enabling ``feature_b`` requires ``feature_a`` to be active, and
    need to test against data generation routines ``gen_a`` and ``gen_b``:

    >>> tf = TestFactory(test_function=run_test)
    >>> tf.add_option(name='data_in', optionlist=[gen_a, gen_b])
    >>> tf.add_option('backpressure', [None, random_backpressure])
    >>> tf.add_option(('feature_a', 'feature_b'), [(False, False), (True, False), (True, True)])
    >>> tf.generate_tests()

    We would get the following tests:

        * ``gen_a`` with no backpressure and both features disabled
        * ``gen_a`` with no backpressure and only ``feature_a`` enabled
        * ``gen_a`` with no backpressure and both features enabled
        * ``gen_a`` with ``random_backpressure`` and both features disabled
        * ``gen_a`` with ``random_backpressure`` and only ``feature_a`` enabled
        * ``gen_a`` with ``random_backpressure`` and both features enabled
        * ``gen_b`` with no backpressure and both features disabled
        * ``gen_b`` with no backpressure and only ``feature_a`` enabled
        * ``gen_b`` with no backpressure and both features enabled
        * ``gen_b`` with ``random_backpressure`` and both features disabled
        * ``gen_b`` with ``random_backpressure`` and only ``feature_a`` enabled
        * ``gen_b`` with ``random_backpressure`` and both features enabled

    The tests are appended to the calling module for auto-discovery.

    Tests are simply named ``test_function_N``. The docstring for the test (hence
    the test description) includes the name and description of each generator.

    .. versionchanged:: 1.5
        Groups of options are now supported

    .. versionchanged:: 2.0
        You can now pass :func:`cocotb.test` decorator arguments when generating tests.

    .. deprecated:: 2.0
        Use :func:`cocotb.parameterize` instead.
    """

    def __init__(self, test_function: F, *args: Any, **kwargs: Any) -> None:
        self.test_function = test_function
        self.args = args
        self.kwargs_constant = kwargs
        self.kwargs: Dict[
            Union[str, Sequence[str]], Union[Sequence[Any], Sequence[Sequence[Any]]]
        ] = {}

    @overload
    def add_option(self, name: str, optionlist: Sequence[Any]) -> None:
        ...

    @overload
    def add_option(
        self, name: Sequence[str], optionlist: Sequence[Sequence[Any]]
    ) -> None:
        ...

    def add_option(
        self,
        name: Union[str, Sequence[str]],
        optionlist: Union[Sequence[str], Sequence[Sequence[str]]],
    ) -> None:
        """Add a named option to the test.

        Args:
            name:
                An option name, or an iterable of several option names. Passed to test as keyword arguments.

            optionlist:
                A list of possible options for this test knob.
                If N names were specified, this must be a list of N-tuples or
                lists, where each element specifies a value for its respective
                option.

        .. versionchanged:: 1.5
            Groups of options are now supported
        """
        if not isinstance(name, str):
            for opt in optionlist:
                if len(name) != len(opt):
                    raise ValueError(
                        "Mismatch between number of options and number of option values in group"
                    )
        self.kwargs[name] = optionlist

    def generate_tests(
        self,
        *,
        prefix: Optional[str] = None,
        postfix: Optional[str] = None,
        name: Optional[str] = None,
        timeout_time: Optional[float] = None,
        timeout_unit: str = "steps",
        expect_fail: bool = False,
        expect_error: Union[Type[Exception], Sequence[Type[Exception]]] = (),
        skip: bool = False,
        stage: int = 0,
    ):
        """
        Generate an exhaustive set of tests using the cartesian product of the
        possible keyword arguments.

        The generated tests are appended to the namespace of the calling
        module.

        Args:
            prefix:
                Text string to append to start of ``test_function`` name when naming generated test cases.
                This allows reuse of a single ``test_function`` with multiple :class:`TestFactories <.TestFactory>` without name clashes.

                .. deprecated:: 2.0
                    Use the more flexible ``name`` field instead.

            postfix:
                Text string to append to end of ``test_function`` name when naming generated test cases.
                This allows reuse of a single ``test_function`` with multiple :class:`TestFactories <.TestFactory>` without name clashes.

                .. deprecated:: 2.0
                    Use the more flexible ``name`` field instead.

            name:
                Passed as ``name`` argument to :func:`cocotb.test`.

                .. versionadded:: 2.0

            timeout_time:
                Passed as ``timeout_time`` argument to :func:`cocotb.test`.

                .. versionadded:: 2.0

            timeout_unit:
                Passed as ``timeout_unit`` argument to :func:`cocotb.test`.

                .. versionadded:: 2.0

            expect_fail:
                Passed as ``expect_fail`` argument to :func:`cocotb.test`.

                .. versionadded:: 2.0

            expect_error:
                Passed as ``expect_error`` argument to :func:`cocotb.test`.

                .. versionadded:: 2.0

            skip:
                Passed as ``skip`` argument to :func:`cocotb.test`.

                .. versionadded:: 2.0

            stage:
                Passed as ``stage`` argument to :func:`cocotb.test`.

                .. versionadded:: 2.0
        """
        warnings.warn(
            "TestFactory is deprecated, use `@cocotb.parameterize` instead",
            DeprecationWarning,
            stacklevel=2,
        )

        glbs = inspect.currentframe().f_back.f_globals

        if "__cocotb_tests__" not in glbs:
            glbs["__cocotb_tests__"] = []

        for test in self._generate_tests(
            prefix=prefix,
            postfix=postfix,
            name=name,
            module=glbs["__name__"],
            timeout_time=timeout_time,
            timeout_unit=timeout_unit,
            expect_fail=expect_fail,
            expect_error=expect_error,
            skip=skip,
            stage=stage,
        ):
            if test.name in glbs:
                _logger.error(
                    "Overwriting %s in module %s. "
                    "This causes a previously defined testcase not to be run. "
                    "Consider using the `name`, `prefix`, or `postfix` arguments to augment the name.",
                    name,
                    glbs["__name__"],
                )
            glbs["__cocotb_tests__"].append(test)
            glbs[test.name] = test

    def _generate_tests(
        self,
        *,
        prefix: Optional[str] = None,
        postfix: Optional[str] = None,
        name: Optional[str] = None,
        module: Optional[str] = None,
        timeout_time: Optional[float] = None,
        timeout_unit: str = "steps",
        expect_fail: bool = False,
        expect_error: Union[Type[Exception], Sequence[Type[Exception]]] = (),
        skip: bool = False,
        stage: int = 0,
    ) -> Iterable[Test]:
        if prefix is not None:
            warnings.warn(
                "``prefix`` argument is deprecated. Use the more flexible ``name`` field instead.",
                DeprecationWarning,
            )
        else:
            prefix = ""

        if postfix is not None:
            warnings.warn(
                "``postfix`` argument is deprecated. Use the more flexible ``name`` field instead.",
                DeprecationWarning,
            )
        else:
            postfix = ""

        test_func_name = self.test_function.__qualname__ if name is None else name

        for index, testoptions in enumerate(
            dict(zip(self.kwargs, v)) for v in product(*self.kwargs.values())
        ):
            name = "%s%s%s_%03d" % (
                prefix,
                test_func_name,
                postfix,
                index + 1,
            )
            doc: str = "Automatically generated test\n\n"

            # preprocess testoptions to split tuples
            testoptions_split: Dict[str, Sequence[Any]] = {}
            for optname, optvalue in testoptions.items():
                if isinstance(optname, str):
                    optvalue = cast(Sequence[Any], optvalue)
                    testoptions_split[optname] = optvalue
                else:
                    # previously checked in add_option; ensure nothing has changed
                    optvalue = cast(Sequence[Sequence[Any]], optvalue)
                    assert len(optname) == len(optvalue)
                    for n, v in zip(optname, optvalue):
                        testoptions_split[n] = v

            for optname, optvalue in testoptions_split.items():
                if callable(optvalue):
                    if not optvalue.__doc__:
                        desc = "No docstring supplied"
                    else:
                        desc = optvalue.__doc__.split("\n")[0]
                    doc += f"\t{optname}: {optvalue.__qualname__} ({desc})\n"
                else:
                    doc += f"\t{optname}: {repr(optvalue)}\n"

            kwargs: Dict[str, Any] = {}
            kwargs.update(self.kwargs_constant)
            kwargs.update(testoptions_split)

            @functools.wraps(self.test_function)
            async def _my_test(dut, kwargs: Dict[str, Any] = kwargs) -> None:
                await self.test_function(dut, *self.args, **kwargs)

            _my_test.__doc__ = doc
            _my_test.__name__ = name
            _my_test.__qualname__ = name

            yield Test(
                func=_my_test,
                name=name,
                module=module,
                timeout_time=timeout_time,
                timeout_unit=timeout_unit,
                expect_fail=expect_fail,
                expect_error=expect_error,
                skip=skip,
                stage=stage,
            )
