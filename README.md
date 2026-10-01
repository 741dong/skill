# Skill

WorkBuddy / CodeBuddy agent skills collection.

## Structure

```
<skill-name>/
  SKILL.md        # entry point: frontmatter + instructions
  scripts/        # optional helper scripts
  references/     # optional reference docs
```

## Usage

Clone this repo, then symlink or copy a skill directory into your agent's skills path:

- user-level: `~/.workbuddy/skills/`
- project-level: `{workspace}/.workbuddy/skills/`
