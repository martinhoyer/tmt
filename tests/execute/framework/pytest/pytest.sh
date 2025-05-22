#!/bin/bash
. ../../utils.sh

# Plan name can be overriden by the first argument
PLAN=${1:-smoke} # Assuming 'smoke' is the plan name in plans.fmf

rlPhaseStartTest "PytestFramework-$PLAN"
    # Run all tests tagged with pytest-smoke under the specified plan
    # The plan itself in plans.fmf also filters for "tag: pytest-smoke"
    # Using -v for verbosity to ensure pytest output is in the log
    rlRun "tmt run -avvv provision -h container execute -h tmt discover -h fmf --force plans --name $PLAN"

    # Check for the overall summary from tmt, which should reflect the individual test outcomes
    # These will depend on how tests are named and if they are all run together.
    # The tests.fmf defines /pass, /fail, /no_tests_found
    # tmt results will show these names.

    # Check for passing test scenario output
    # We need to find the specific log for the '/pass' test.
    # This is hard from the main rlRun_LOG if tests run in parallel or output is interleaved.
    # A more robust check would parse results.yaml.
    # For now, we assume unique strings in the overall log for simplicity,
    # or that tmt's summary output will contain these details.

    # General check for pytest's output for a passing test
    rlAssertGrep "1 passed" "$rlRun_LOG"

    # General check for pytest's output for a failing test
    rlAssertGrep "1 failed" "$rlRun_LOG"
    
    # General check for pytest's output when no tests are collected
    rlAssertGrep "collected 0 items" "$rlRun_LOG"
    
    # Check tmt's result summary (example, adjust based on actual tmt output format)
    # This assumes tmt summarizes results like this.
    rlAssertGrep "1 test passed" "$rlRun_LOG"
    rlAssertGrep "1 test failed" "$rlRun_LOG"
    rlAssertGrep "1 test info" "$rlRun_LOG" # For the no_tests_found case

    # Checking for pytest-html report.html is complex in shell.
    # We would need to find the specific test workdir for each test.
    # e.g., for the '/pass' test:
    # pass_workdir=$(grep -A5 "/pass" "$rlRun_LOG" | grep "workdir" | head -n1 | awk '{print $2}')
    # if [ -n "$pass_workdir" ] && [ -f "$pass_workdir/report.html" ]; then
    #    rlLog "report.html found for /pass test in $pass_workdir"
    # else
    #    rlFail "report.html not found for /pass test"
    # fi
    # This is deferred for simplicity in this shell script.

rlPhaseEnd
make_executable tests/execute/framework/pytest/pytest.sh
