# Copyright cocotb contributors
# Licensed under the Revised BSD License, see LICENSE for details.
# SPDX-License-Identifier: BSD-3-Clause
"""
Tests that sepcifically test generator-based coroutines
"""
import cocotb
from cocotb.triggers import Timer
from cocotb.result import TestFailure
from common import clock_gen, _check_traceback
import textwrap


# Tests relating to providing meaningful errors if we forget to use the
# yield keyword correctly to turn a function into a coroutine

@cocotb.test(expect_error=TypeError)
def test_not_a_coroutine(dut):
    """Example of a failing to use the yield keyword in a test"""
    dut._log.warning("This test will fail because we don't yield anything")


@cocotb.coroutine
def function_not_a_coroutine():
    """If we don't yield, this isn't a coroutine"""
    return "This should fail"


@cocotb.test()
def test_function_not_a_coroutine(dut):
    """Example of trying to yield a coroutine that isn't a coroutine"""
    yield Timer(500)
    try:
        # failure should occur before we even try to yield or fork the coroutine
        coro = function_not_a_coroutine()
    except TypeError as exc:
        assert "isn't a valid coroutine" in str(exc)
    else:
        raise TestFailure


def normal_function(dut):
    return True


@cocotb.test()
def test_function_not_decorated(dut):
    try:
        yield normal_function(dut)
    except TypeError as exc:
        assert "yielded" in str(exc)
        assert "scheduler can't handle" in str(exc)
    except:
        raise TestFailure


@cocotb.test()
def test_function_not_decorated_fork(dut):
    """Example of trying to fork a coroutine that isn't a coroutine"""
    yield Timer(500)
    try:
        cocotb.fork(normal_function(dut))
    except TypeError as exc:
        assert "isn't a coroutine" in str(exc)
    else:
        raise TestFailure()

    yield Timer(500)


@cocotb.test()
def test_adding_a_coroutine_without_starting(dut):
    """Catch (and provide useful error) for attempts to fork coroutines
    incorrectly"""
    yield Timer(100)
    try:
        forked = cocotb.fork(clock_gen)
    except TypeError as exc:
        assert "a coroutine that hasn't started" in str(exc)
    else:
        raise TestFailure


@cocotb.test(expect_fail=False)
def test_yield_list(dut):
    """Example of yielding on a list of triggers"""
    clock = dut.clk
    cocotb.scheduler.add(clock_gen(clock))
    yield [Timer(1000), Timer(2000)]

    yield Timer(10000)


@cocotb.coroutine
def syntax_error():
    yield Timer(100)
    fail


@cocotb.test(expect_error=True)
def test_coroutine_syntax_error(dut):
    """Syntax error in a coroutine that we yield"""
    yield clock_gen(dut.clk)
    yield syntax_error()


@cocotb.test(expect_error=True)
def test_fork_syntax_error(dut):
    """Syntax error in a coroutine that we fork"""
    yield clock_gen(dut.clk)
    cocotb.fork(syntax_error())
    yield clock_gen(dut.clk)


@cocotb.test()
def test_coroutine_return(dut):
    """ Test that the Python 3.3 syntax for returning from generators works """
    @cocotb.coroutine
    def return_it(x):
        return x

        # this makes `return_it` a coroutine
        yield

    ret = yield return_it(42)
    if ret != 42:
        raise TestFailure("Return statement did not work")


@cocotb.test()
def test_immediate_coro(dut):
    """
    Test that coroutines can return immediately
    """
    @cocotb.coroutine
    def immediate_value():
        return 42
        yield

    @cocotb.coroutine
    def immediate_exception():
        raise ValueError
        yield

    assert (yield immediate_value()) == 42

    try:
        yield immediate_exception()
    except ValueError:
        pass
    else:
        raise TestFailure("Exception was not raised")


@cocotb.test()
def test_exceptions_direct(dut):
    """ Test exception propagation via a direct yield statement """
    @cocotb.coroutine
    def raise_inner():
        yield Timer(10)
        raise ValueError('It is soon now')

    @cocotb.coroutine
    def raise_soon():
        yield Timer(1)
        yield raise_inner()

    # it's ok to change this value if the traceback changes - just make sure
    # that when changed, it doesn't become harder to read.
    expected = textwrap.dedent(r"""
    Traceback \(most recent call last\):
      File ".*common\.py", line \d+, in _check_traceback
        yield running_coro
      File ".*test_generator_coroutines\.py", line \d+, in raise_soon
        yield raise_inner\(\)
      File ".*test_generator_coroutines\.py", line \d+, in raise_inner
        raise ValueError\('It is soon now'\)
    ValueError: It is soon now""").strip()

    yield _check_traceback(raise_soon(), ValueError, expected)


@cocotb.test()
def test_exceptions_forked(dut):
    """ Test exception propagation via cocotb.fork """
    @cocotb.coroutine
    def raise_inner():
        yield Timer(10)
        raise ValueError('It is soon now')

    @cocotb.coroutine
    def raise_soon():
        yield Timer(1)
        coro = cocotb.fork(raise_inner())
        yield coro.join()

    # it's ok to change this value if the traceback changes - just make sure
    # that when changed, it doesn't become harder to read.
    expected = textwrap.dedent(r"""
    Traceback \(most recent call last\):
      File ".*common\.py", line \d+, in _check_traceback
        yield running_coro
      File ".*test_generator_coroutines\.py", line \d+, in raise_soon
        yield coro\.join\(\)
      File ".*test_generator_coroutines\.py", line \d+, in raise_inner
        raise ValueError\('It is soon now'\)
    ValueError: It is soon now""").strip()

    yield _check_traceback(raise_soon(), ValueError, expected)
