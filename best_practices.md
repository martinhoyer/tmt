# TMT Project Best Practices

## Code Style
- Maintain Python 3.9+ compatibility
- Keep line length to 99 characters maximum
- Use strict typing with proper type annotations
- Follow ruff formatting rules
- Use project-specific imports from `tmt._compat` instead of standard libraries
- Name variables, functions, and classes following PEP8 conventions
- Write full sentences for comments with capitalized first word
- Enclose names in single quotes for user messages

## Project-Specific Patterns
- Use `tmt.container.container` instead of `dataclasses.dataclass`
- Use `tmt.container.field` instead of `dataclasses.field`
- Use Path methods from pathlib for file operations instead of built-in `open()`
- Follow the project's plugin architecture for extensions

## Error Handling
- Use specific error types rather than generic exceptions
- Include context in error messages to help with debugging
- Preserve tracebacks with `raise ... from err` pattern
- Provide meaningful and actionable error messages to the user

## Testing
- Add comprehensive test coverage for new functionality
- Place tests in the appropriate subdirectory under `/tests`
- Follow the existing test patterns in similar components
- Document test cases clearly
- Include both positive and negative test cases

## Documentation
- Document new functionality with docstrings
- Update relevant specification in the `/spec` directory
- Keep docstrings up to date with implementation
- For user-facing features, add or update documentation in `/docs`
- Document plugin options in both code and schema files

## Pull Requests
- Keep PRs focused on a single feature or bug fix
- Add tests for all new functionality
- Update documentation alongside code changes
- Respect the project's commit message format
- Respond to review comments promptly with fixes
