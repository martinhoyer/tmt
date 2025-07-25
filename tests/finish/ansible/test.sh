#!/bin/bash
. /usr/share/beakerlib/beakerlib.sh || exit 1
. ../../images.sh || exit 1

rlJournalStart
    rlPhaseStartSetup
        rlRun "PROVISION_HOW=${PROVISION_HOW:-local}"

        if [ "$PROVISION_HOW" = "container" ]; then
            rlRun "IMAGES='$TEST_CONTAINER_IMAGES'"

            build_container_images

        elif [ "$PROVISION_HOW" = "virtual" ]; then
            rlRun "IMAGES='$TEST_VIRTUAL_IMAGES'"

        else
            rlRun "IMAGES="
        fi

        rlRun "pushd data"
        rlRun "run=\$(mktemp -d)" 0 "Create run directory"
    rlPhaseEnd

    while IFS= read -r image; do
        phase_prefix="$(test_phase_prefix $image)"

        rlPhaseStartTest "$phase_prefix Test Ansible playbook"
            if is_fedora_coreos "$image"; then
                    rlLogInfo "Skipping because of https://github.com/teemtee/tmt/issues/2884: tmt cannot run tests on Fedora CoreOS containers"
                rlPhaseEnd

                continue
            fi

            if rlIsFedora ">=42" && (is_centos_7 "$image" || is_ubi_8 "$image"); then
                    rlLogInfo "Skipping because Ansible shipped with Fedora does not support Python 3.6"
                rlPhaseEnd

                continue
            fi

            [ "$PROVISION_HOW" = "container" ] && rlRun "podman images $image"

            # Run given method
            if [ "$PROVISION_HOW" = "local" ]; then
                rlRun "tmt run -i $run --scratch -av provision -h $PROVISION_HOW"
            else
                rlRun "tmt run -i $run --scratch -av provision -h $PROVISION_HOW -i $image"
            fi

            # Check that created file is synced back
            rlRun "ls -l $run/plan/data"
            rlAssertExists "$run/plan/data/my_file.txt"

            rlRun "results_file=$run/plan/finish/results.yaml"
            rlAssertExists "$results_file"
            rlAssertEquals "finish produced expected result" \
                           "$(yq -r '.[] | "\(.name):\(.result):\(.log[0])"' $results_file)" \
                           "Ansible we want to test / playbook.yml:pass:Ansible-we-want-to-test/playbook-0/default-0/output.txt"

            # After the local provision remove the test file
            if [[ $PROVISION_HOW == local ]]; then
                rlRun "sudo rm -f /tmp/finished"
            fi
        rlPhaseEnd
    done <<< "$IMAGES"

    rlPhaseStartCleanup
        rlRun "rm -r $run" 0 "Removing run directory"
        rlRun "popd"
    rlPhaseEnd
rlJournalEnd
