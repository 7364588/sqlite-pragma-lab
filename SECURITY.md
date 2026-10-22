# Security

The initial 0.1.x line is supported. Report a suspected security defect through
the repository's private vulnerability-reporting channel when available. If it
is unavailable, open an issue requesting a private contact without including
exploit details, credentials, or sensitive SQL.

SQL is executed in memory with a restrictive authorizer and resource budgets.
This is not an operating-system sandbox or a complete process-memory limit. Use
developer-controlled fixtures; isolate the process when accepting submissions
from others. There is no live-database mode or automatic source discovery.

Reports exclude row values, raw SQL, source paths, and raw SQLite errors. Table
and column names remain visible and may themselves be sensitive. Review reports
before sharing. File checks do not defend against an adversary concurrently
replacing filesystem paths or parent directories.

HTML embeds escaped JSON, renders identifiers as text, and loads no remote assets.
Existing output files are not overwritten. A failed stdout write can occur after
an HTML file has been created; inspect the destination before retrying.
