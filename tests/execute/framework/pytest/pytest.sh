#!/bin/bash
. /usr/share/beakerlib/beakerlib.sh || exit 1

# Plan name can be overriden by the first argument
PLAN=${1:-/tests/execute/framework/pytest} # Default to the path-derived plan name

rlPhaseStartTest "PytestFramework-$PLAN"
    # Run all tests tagged with pytest-smoke under the specified plan
    # The plan itself in plans.fmf also filters for "tag: pytest-smoke"
    # Using -v for verbosity to ensure pytest output is in the log
    # The -dddvvv should be enough to get workdir info.
    rlRun "tmt run -adddvvv provision -h container execute -h tmt discover -h fmf --force plans --name \"$PLAN\""
    RUNDIR=$(grep 'workdir' $rlRun_LOG | awk '{print $2}' | tail -1)
    rlLog "RUNDIR is $RUNDIR"

    # The plan name is $PLAN. Remove leading '/' for path construction.
    PLAN_PATH_COMP=$(echo $PLAN | sed 's|^/||')
    #rlLog "PLAN_PATH_COMP is $PLAN_PATH_COMP"

    # Expected number of JUnit files for the three tests (pass, fail, no_tests_found)
    EXPECTED_JUNIT_FILES=3
    # Search for junit-report.xml files within the specific plan's execution path.
    # The structure can be $RUNDIR/plan_path_comp/test_name_and_serial/execute/data/junit-report.xml
    # or slightly different depending on tmt version (e.g. without execute/data).
    # Using a find that is somewhat robust.
    JUNIT_FILES_FOUND=$(find "$RUNDIR/$PLAN_PATH_COMP" -path '*/data/junit-report.xml' -type f | wc -l)
    if [ "$JUNIT_FILES_FOUND" -eq 0 ]; then
        # Fallback for older tmt versions or different structures
        JUNIT_FILES_FOUND=$(find "$RUNDIR/$PLAN_PATH_COMP" -name 'junit-report.xml' -type f | wc -l)
    fi
    rlAssertEquals "Number of JUnit XML files found" "$EXPECTED_JUNIT_FILES" "$JUNIT_FILES_FOUND"

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
