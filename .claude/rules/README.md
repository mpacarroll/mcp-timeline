# Repo rules

Durable, repo-specific knowledge for AI sessions. Every .md file here loads
automatically at session start; add `paths:` glob frontmatter to scope a rule
to part of the tree so it loads only when those files are touched.

Good candidates: architecture notes, data model quirks, deploy gotchas,
decisions with reasons. Shared rules live in the carroll-core plugin and in
https://github.com/mpacarroll/ai-instructions, not here.
