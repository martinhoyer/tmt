# Metadata Specification

This specification defines a way how to store all metadata needed
for test execution in plain text files close to the test code or
application source code. Files are stored under version control
directly in the git repository.

[Flexible Metadata Format](https://fmf.readthedocs.io/) is used to store data in a concise
human and machine readable way plus adds a couple of nice features
like virtual hierarchy, inheritance and elasticity to minimize
data duplication and maintenance.

The following metadata levels are defined:

Level 0: Core
    [Core attributes](./spec/core.md) such as [summary](./spec/core.md#summary)
    for short overview, [description](./spec/core.md#description) for detailed
    texts or the [order](./spec/core.md#order) which are common and can
    be used across all metadata levels.

Level 1: Tests
    Metadata closely related to individual [tests](./spec/tests.md) such
    as the [test script](./spec/tests.md#test), directory
    [path](./spec/tests.md#path) or maximum [duration](./spec/tests.md#duration)
    which are stored directly with the test code.

Level 2: Plans
    [Plans](./spec/plans.md) are used to group relevant tests and enable
    them in the CI. They describe how to
    [discover tests](./spec/plans.md#discover) for execution, how to
    [provision the environment](./spec/plans.md#provision) and
    [prepare it for testing](./spec/plans.md#prepare), how to
    [execute tests](./spec/plans.md#execute) and [report test results](./spec/plans.md#report).

Level 3: Stories
    User [stories](./stories.md) can be used to describe expected
    features of the application by defining the user
    [story](./stories.md#story) and to easily track which
    functionality has been already implemented, verified and
    documented.
