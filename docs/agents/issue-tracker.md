# Issue Tracker: Local Markdown

Issues and PRDs for this repo live as markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The PRD is `.scratch/<feature-slug>/PRD.md`
- Implementation issues are `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`
- Triage state is recorded as a `Status:` line near the top of each issue file
- Comments and conversation history append to the bottom of the file under a `## Comments` heading

## Publishing Work

When a skill says "publish to the issue tracker", create a new file under `.scratch/<feature-slug>/`, creating the directory if needed.

## Fetching Work

When a skill says "fetch the relevant ticket", read the referenced markdown file. The user will normally pass the path or issue number directly.
