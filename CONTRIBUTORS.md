# Contributors

Please follow the below guidelines for commit messages. Thank you to all the contributors who have helped improve this project!

## Commit Message Guidelines

Your commit messages should follow the conventional commit format:

```
<type>(<optional scope>)<place-!-for-breaking-changes>: <description>

[optional body]

[optional footer]
```

### Type

The `<type>` should be one of the following:

- `feat`: A new feature.
- `fix`: A bug fix.
- `docs`: Documentation updates.
- `style`: Changes that do not affect code (e.g., formatting, white-space, etc.).
- `refactor`: Code changes that neither fix a bug nor add a feature.
- `perf`: A change that improves performance.
- `test`: Adding or modifying tests.
- `build`: Changes to the build system or dependencies.
- `ci`: Changes to CI configuration or workflows.
- `chore`: Other changes that do not modify source or tests.
- `revert`: Reverts a previous commit.

**This list is enforced on pull request titles** by
`.github/workflows/enforce-pr-title.yml`, which must be kept in step with it.
The two drifted for a long time: CI allowed `jira`, `proj-` and `hotfix` (never
used here, inherited from the action's example config) while rejecting `perf`,
`build`, `chore`, `style` and `refactor` — all of which this file documented as
valid and the history actually uses.

PR titles are also capped at **50 characters**, which a Conventional Commit with
a scope reaches quickly: `feat(camera): ` alone is 14. Put the detail in the
body rather than the title.

### Scope (optional)

The `<scope>` should indicate the scope of the commit (e.g., component, module, etc.). It is optional if the commit applies globally.

### Description

The `<description>` should provide a brief summary of the change.

### Breaking Changes

For breaking changes include an '!' after the scope and elaborate a bit in the body.

```
feat!: some breaking change description

<body description>

BREAKING CHANGE: <what-broke-description>
```

### Body (optional)

The `<body>` should provide more detailed information about the change. It can span multiple lines.

### Footer (optional)

The `<footer>` should contain any additional information related to the commit, such as references to issues or breaking changes.

```
Refs: #100,#101,etc

or 

BREAKING CHANGE: breaks some interface
```

## Examples

### Good Examples

- ✅ `feat: Add user authentication`
- ✅ `fix(auth): Resolve issue with login logic`
- ✅ `fix(scope/another-scope/some-scope): commit subject with multiple scopes`
- ✅ `docs: Update README with new examples`
- ✅ `style: Format code according to style guide`
- ✅ `refactor: Simplify data processing method`
- ✅ `test: Add unit tests for API endpoints`
- ✅ `chore: Upgrade dependencies to latest versions`

### Bad Examples

- ❌ `added new feature`
- ❌ `Fixed the login bug`
- ❌ `Documentation updates`
- ❌ `Fix formatting`
- ❌ `Changed some code`
- ❌ `Added tests`
- ❌ `Updated dependencies`

Remember to follow these guidelines when making contributions. Consistent and clear commit messages help maintain a healthy and easily understandable codebase.

Thank you again for your contributions! 🙌

## Community and Volunteering

Please have a look at the [Volunteer](https://github.com/iot-root/gardyn-of-eden/wiki/Volunteer) page for instructions on where to start and more.
