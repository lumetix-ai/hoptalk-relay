# CLAUDE.md

## Language

- Reply in the language of the user's latest message (Russian or English).
- Always write in English, regardless of the conversation language: source code,
  comments and docstrings, shell-script comments, program output and log
  messages, commit messages, PR descriptions, branch names, README and other
  documentation, technical instructions and guides.
- If the user explicitly asks for a different language for one response or
  artifact, follow that request for that case only.

## Naming and readability

Optimize for a reader who is new to the project and debugging a production issue
at 3 AM: boring, explicit code over clever, compact code.

- Use full, descriptive names for variables, functions, methods, classes,
  parameters and files. No abbreviations, contractions or single-letter names,
  except universally understood ones (`id`, `url`, `api`, `x`/`y` in math formulas).
- Follow the standard casing convention of the language in use (for example,
  `snake_case` in Python, `camelCase` in JavaScript). The examples below use camelCase.
- Function names say what the function does: `handleUserAuthentication()`, never
  a bare `handle()` or `process()`.
- Split complex logic into small, clearly named functions. Avoid hidden side
  effects, long chained expressions and dense one-liners.

| Avoid       | Prefer                        |
|-------------|-------------------------------|
| `tmp`       | `temporaryDirectoryPath`      |
| `cfg`       | `applicationConfiguration`    |
| `resp`      | `httpResponse`                |
| `d`         | `customerInvoiceData`         |
| `handle()`  | `handleUserAuthentication()`  |
| `process()` | `processUploadedImageFiles()` |

Improve poor names in the code you are already changing. Propose renames of
identifiers used across the codebase as a separate change instead of mixing them
into an unrelated diff.

## Comments and docstrings

Code should explain itself. When something is unclear, first rename a variable
or extract a well-named function; add a comment only if that still is not enough.

- Write a comment only when the code cannot be understood without it: an
  external constraint, a workaround for a third-party bug, a non-obvious reason
  behind a decision, or behavior that would surprise the reader.
- Never restate what the code already says (`# increment the counter`,
  `# loop over users`, `# return the result`); instead, rename the symbol or
  extract a well-named variable or function until the code says it.
- The same applies to docstrings: no docstring that only repeats the function
  name, parameters or types. Write one only when the contract is not obvious
  from the name and signature.
- Describe the code as it stands instead of the agent's work on it: leave out
  tasks, steps, iterations, requests, reviews and previous versions
  (`# Step 2: ...`, `# Added for the retry task`, `# Fixed as requested`,
  `# New implementation`). Change history belongs in the commit message.
- Delete code that is no longer needed instead of commenting it out; version
  control keeps the old version.
- When changing code, update or delete the comments it makes wrong.
